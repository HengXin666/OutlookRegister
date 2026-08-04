"""Proxy endpoint selection and managed local Mihomo runtimes."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import yaml


ADVANCED_PROTOCOLS = frozenset({"vless", "vmess", "trojan"})
BROWSER_PROTOCOLS = frozenset({"http", "https", "socks5"})
DEFAULT_PROTOCOL_PREFERENCE = ("vless",)


class ProxyRuntimeError(RuntimeError):
    """Raised when a configured proxy endpoint cannot be made usable."""


@dataclass(frozen=True)
class ProxyEndpoint:
    protocol: str
    transport: str
    uri: str
    browser_compatible: bool


@dataclass(frozen=True)
class ControlNode:
    index: int
    name: str
    endpoints: tuple[ProxyEndpoint, ...]


def _decode_base64_json(value: str) -> dict[str, Any]:
    payload = value.strip()
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        result = json.loads(decoded)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProxyRuntimeError("invalid VMess endpoint") from exc
    if not isinstance(result, dict):
        raise ProxyRuntimeError("invalid VMess endpoint")
    return result


def advanced_proxy_from_uri(uri: str, name: str = "HX-UPSTREAM") -> dict[str, Any]:
    """Convert an HX VLESS/VMess/Trojan share URI into a Mihomo proxy."""
    protocol = uri.split(":", 1)[0].lower()
    if protocol == "vmess":
        payload = _decode_base64_json(uri.removeprefix("vmess://"))
        try:
            proxy: dict[str, Any] = {
                "name": name,
                "type": "vmess",
                "server": str(payload["add"]),
                "port": int(payload["port"]),
                "uuid": str(payload["id"]),
                "alterId": int(payload.get("aid", 0)),
                "cipher": str(payload.get("scy", "auto")),
                "network": str(payload.get("net", "ws")),
                "tls": str(payload.get("tls", "")).lower() == "tls",
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise ProxyRuntimeError("invalid VMess endpoint") from exc
        if proxy["network"] == "ws":
            proxy["ws-opts"] = {
                "path": str(payload.get("path", "/")),
                "headers": {"Host": str(payload.get("host", payload["add"]))},
            }
        server_name = str(payload.get("sni", "")).strip()
        if server_name:
            proxy["servername"] = server_name
        return proxy

    if protocol not in {"vless", "trojan"}:
        raise ProxyRuntimeError(f"unsupported advanced proxy protocol: {protocol or 'unknown'}")
    try:
        parsed = urlsplit(uri)
        port = parsed.port
    except ValueError as exc:
        raise ProxyRuntimeError(f"invalid {protocol.upper()} endpoint") from exc
    if not parsed.hostname or not port or not parsed.username:
        raise ProxyRuntimeError(f"invalid {protocol.upper()} endpoint")
    query = parse_qs(parsed.query)
    transport = query.get("type", ["tcp"])[0].lower()
    proxy = {
        "name": name,
        "type": protocol,
        "server": parsed.hostname,
        "port": port,
        "network": transport,
        "tls": query.get("security", [""])[0].lower() == "tls",
    }
    credential = unquote(parsed.username)
    if protocol == "vless":
        proxy["uuid"] = credential
        proxy["udp"] = True
    else:
        proxy["password"] = credential
    server_name = query.get("sni", [""])[0].strip()
    if server_name:
        proxy["servername"] = server_name
    if transport == "ws":
        host = query.get("host", [server_name or parsed.hostname])[0]
        proxy["ws-opts"] = {
            "path": query.get("path", ["/"])[0],
            "headers": {"Host": host},
        }
    return proxy


def mihomo_config(uri: str, port: int) -> dict[str, Any]:
    return {
        "mixed-port": port,
        "bind-address": "127.0.0.1",
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "proxies": [advanced_proxy_from_uri(uri)],
        "rules": ["MATCH,HX-UPSTREAM"],
    }


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ManagedMihomo:
    """Own one bounded Mihomo child process and its temporary configuration."""

    def __init__(self, binary: str, uri: str, startup_timeout: float = 10.0):
        resolved = shutil.which(binary) if os.path.sep not in binary else binary
        if not resolved or not Path(resolved).is_file():
            raise ProxyRuntimeError("Mihomo executable was not found")
        self.binary = str(Path(resolved).resolve())
        self.uri = uri
        self.startup_timeout = startup_timeout
        self.port = _reserve_loopback_port()
        self._directory: tempfile.TemporaryDirectory[str] | None = None
        self._log = None
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def proxy_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        if self._process is not None:
            return
        self._directory = tempfile.TemporaryDirectory(prefix="outlook-mihomo-")
        directory = Path(self._directory.name)
        config_path = directory / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(mihomo_config(self.uri, self.port), sort_keys=False),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        try:
            validation = subprocess.run(
                [self.binary, "-t", "-d", str(directory), "-f", str(config_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.startup_timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.close()
            raise ProxyRuntimeError("Mihomo configuration validation failed") from exc
        if validation.returncode != 0:
            self.close()
            raise ProxyRuntimeError("Mihomo rejected the generated endpoint configuration")
        self._log = (directory / "mihomo.log").open("ab")
        try:
            self._process = subprocess.Popen(
                [self.binary, "-d", str(directory), "-f", str(config_path)],
                stdin=subprocess.DEVNULL,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self.close()
            raise ProxyRuntimeError("Mihomo local proxy could not be started") from exc
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self.close()
                raise ProxyRuntimeError("Mihomo exited before its local proxy became ready")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        self.close()
        raise ProxyRuntimeError("Mihomo local proxy startup timed out")

    def close(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self._log is not None:
            self._log.close()
            self._log = None
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None


def parse_control_node(payload: Any) -> ControlNode:
    """Validate and normalize one node returned by the HX control API."""
    if not isinstance(payload, dict):
        raise ProxyRuntimeError("HX proxy control returned an invalid node")
    endpoints: list[ProxyEndpoint] = []
    for item in payload.get("endpoints", []):
        if not isinstance(item, dict):
            continue
        protocol = str(item.get("protocol", "")).lower()
        uri = str(item.get("uri", ""))
        if protocol in ADVANCED_PROTOCOLS | BROWSER_PROTOCOLS and uri:
            endpoints.append(
                ProxyEndpoint(
                    protocol=protocol,
                    transport=str(item.get("transport", "tcp")).lower(),
                    uri=uri,
                    browser_compatible=bool(
                        item.get("browser_compatible", protocol in BROWSER_PROTOCOLS)
                    ),
                )
            )
    legacy_url = payload.get("proxy_url")
    if not endpoints and isinstance(legacy_url, str) and legacy_url:
        protocol = legacy_url.split(":", 1)[0].lower()
        endpoints.append(ProxyEndpoint(protocol, "tcp", legacy_url, True))
    try:
        index = int(payload["index"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProxyRuntimeError("HX proxy control returned an invalid node index") from exc
    if index < 1 or not endpoints:
        raise ProxyRuntimeError("HX proxy node has no supported endpoint")
    return ControlNode(
        index=index,
        name=str(payload.get("node_name", index)),
        endpoints=tuple(endpoints),
    )


def select_endpoint(
    node: ControlNode,
    protocol_preference: tuple[str, ...] = DEFAULT_PROTOCOL_PREFERENCE,
) -> ProxyEndpoint:
    """Select the first usable endpoint according to client preference."""
    for protocol in protocol_preference:
        for endpoint in node.endpoints:
            if endpoint.protocol != protocol:
                continue
            if endpoint.protocol in ADVANCED_PROTOCOLS and endpoint.transport != "ws":
                continue
            if endpoint.protocol == "vless":
                try:
                    parsed = urlsplit(endpoint.uri)
                    port = parsed.port
                except ValueError:
                    continue
                query = parse_qs(parsed.query)
                if (
                    port != 443
                    or query.get("security", [""])[0].lower() != "tls"
                ):
                    continue
            return endpoint
    raise ProxyRuntimeError(f"HX proxy node {node.index} has no preferred endpoint")


__all__ = [
    "ControlNode",
    "ManagedMihomo",
    "ProxyEndpoint",
    "ProxyRuntimeError",
    "advanced_proxy_from_uri",
    "mihomo_config",
    "parse_control_node",
    "select_endpoint",
]
