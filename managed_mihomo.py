"""Bounded local Mihomo instances for HX WebSocket residential endpoints."""

from __future__ import annotations

import atexit
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit


SUPPORTED_PROTOCOLS = {"vless", "vmess", "trojan"}


class ManagedMihomoError(Exception):
    """A local data-plane endpoint could not be validated or started."""


def build_mihomo_config(endpoint: dict, local_port: int) -> dict:
    """Convert one HX endpoint into a loopback-only Mihomo configuration."""
    if not isinstance(endpoint, dict):
        raise ManagedMihomoError("HX-ProxyGroup 数据端点格式错误")
    protocol = str(endpoint.get("protocol") or "").strip().casefold()
    transport = str(endpoint.get("transport") or "").strip().casefold()
    uri = str(endpoint.get("uri") or "").strip()
    if protocol not in SUPPORTED_PROTOCOLS or transport != "ws":
        raise ManagedMihomoError("本机 Mihomo 只接受 VLESS/VMess/Trojan WebSocket 端点")
    if not uri or len(uri) > 8192 or any(character.isspace() for character in uri):
        raise ManagedMihomoError("HX-ProxyGroup 数据端点 URI 无效")
    if not isinstance(local_port, int) or local_port < 1 or local_port > 65535:
        raise ManagedMihomoError("本机 Mihomo 监听端口无效")

    proxy = _parse_vmess(uri) if protocol == "vmess" else _parse_standard_uri(protocol, uri)
    proxy["name"] = "hx-residential"
    return {
        "mode": "rule",
        "log-level": "silent",
        "allow-lan": False,
        "proxies": [proxy],
        "proxy-groups": [{
            "name": "HX-RESIDENTIAL",
            "type": "select",
            "proxies": ["hx-residential"],
        }],
        "listeners": [{
            "name": "hx-browser-loopback",
            "type": "mixed",
            "listen": "127.0.0.1",
            "port": local_port,
            "proxy": "HX-RESIDENTIAL",
            "udp": True,
        }],
        "rules": ["MATCH,HX-RESIDENTIAL"],
    }


def _parse_standard_uri(protocol: str, uri: str) -> dict:
    parsed = urlsplit(uri)
    if parsed.scheme.casefold() != protocol or not parsed.hostname:
        raise ManagedMihomoError("HX-ProxyGroup 数据端点协议或主机无效")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ManagedMihomoError("HX-ProxyGroup 数据端点端口无效") from exc
    credential = unquote(parsed.username or "")
    if not port or not credential:
        raise ManagedMihomoError("HX-ProxyGroup 数据端点缺少端口或凭据")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if _query_value(query, "type").casefold() != "ws":
        raise ManagedMihomoError("HX-ProxyGroup 数据端点不是 WebSocket")
    if _query_value(query, "security").casefold() != "tls":
        raise ManagedMihomoError("HX-ProxyGroup WebSocket 数据端点必须使用 TLS")
    host = _query_value(query, "host") or parsed.hostname
    servername = _query_value(query, "sni") or host
    path = _query_value(query, "path") or "/"
    proxy = {
        "type": protocol,
        "server": parsed.hostname,
        "port": port,
        "network": "ws",
        "tls": True,
        "servername": servername,
        "ws-opts": {"path": path, "headers": {"Host": host}},
    }
    if protocol == "vless":
        proxy["uuid"] = credential
        proxy["udp"] = True
    else:
        proxy["password"] = credential
    return proxy


def _parse_vmess(uri: str) -> dict:
    if not uri.casefold().startswith("vmess://"):
        raise ManagedMihomoError("HX-ProxyGroup VMess 数据端点 URI 无效")
    encoded = uri[len("vmess://"):]
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        payload = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedMihomoError("HX-ProxyGroup VMess 数据端点无法解析") from exc
    if not isinstance(payload, dict):
        raise ManagedMihomoError("HX-ProxyGroup VMess 数据端点格式错误")
    server = str(payload.get("add") or "").strip()
    credential = str(payload.get("id") or "").strip()
    try:
        port = int(payload.get("port"))
        alter_id = int(payload.get("aid") or 0)
    except (TypeError, ValueError) as exc:
        raise ManagedMihomoError("HX-ProxyGroup VMess 数据端点端口无效") from exc
    if not server or not credential or port < 1 or port > 65535:
        raise ManagedMihomoError("HX-ProxyGroup VMess 数据端点缺少主机、端口或凭据")
    if str(payload.get("net") or "").casefold() != "ws":
        raise ManagedMihomoError("HX-ProxyGroup VMess 数据端点不是 WebSocket")
    if str(payload.get("tls") or "").casefold() != "tls":
        raise ManagedMihomoError("HX-ProxyGroup VMess WebSocket 数据端点必须使用 TLS")
    host = str(payload.get("host") or server).strip()
    servername = str(payload.get("sni") or host).strip()
    path = str(payload.get("path") or "/").strip()
    return {
        "type": "vmess",
        "server": server,
        "port": port,
        "uuid": credential,
        "alterId": alter_id,
        "cipher": str(payload.get("scy") or "auto"),
        "network": "ws",
        "tls": True,
        "servername": servername,
        "ws-opts": {"path": path, "headers": {"Host": host}},
    }


def _query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[0] if values else "").strip()


class _Instance:
    def __init__(self, process, directory, log_handle):
        self.process = process
        self.directory = directory
        self.log_handle = log_handle


class ManagedMihomo:
    """Own one short-lived loopback Mihomo process per active declared node."""

    def __init__(self, binary: str | None = None, start_timeout: float = 5.0):
        self._binary = str(binary or os.environ.get("HX_MIHOMO_BIN") or "mihomo").strip()
        self._start_timeout = max(float(start_timeout), 0.5)
        self._instances: dict[int, _Instance] = {}
        self._lock = threading.RLock()
        atexit.register(self.close)

    def start(self, node_index: int, endpoint: dict) -> str:
        with self._lock:
            self._stop_locked(node_index)
            executable = self._resolve_binary()
            local_port = _available_loopback_port()
            config = build_mihomo_config(endpoint, local_port)
            directory = tempfile.TemporaryDirectory(prefix=f"outlook-hx-{node_index}-")
            config_path = Path(directory.name) / "config.json"
            log_path = Path(directory.name) / "mihomo.log"
            try:
                config_path.write_text(json.dumps(config, ensure_ascii=True), encoding="utf-8")
                config_path.chmod(0o600)
                subprocess.run(
                    [executable, "-t", "-d", directory.name, "-f", str(config_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self._start_timeout,
                    check=True,
                )
                log_handle = log_path.open("wb")
                log_path.chmod(0o600)
                process = subprocess.Popen(
                    [executable, "-d", directory.name, "-f", str(config_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                )
                instance = _Instance(process, directory, log_handle)
                self._instances[node_index] = instance
                self._wait_ready(instance, local_port)
            except (OSError, subprocess.SubprocessError) as exc:
                self._stop_locked(node_index)
                directory.cleanup()
                raise ManagedMihomoError("本机 Mihomo 启动或配置校验失败") from exc
            except ManagedMihomoError:
                self._stop_locked(node_index)
                directory.cleanup()
                raise
            return f"http://127.0.0.1:{local_port}"

    def stop(self, node_index: int) -> None:
        with self._lock:
            self._stop_locked(node_index)

    def close(self) -> None:
        with self._lock:
            for node_index in list(self._instances):
                self._stop_locked(node_index)

    def _resolve_binary(self) -> str:
        executable = shutil.which(self._binary)
        if not executable:
            raise ManagedMihomoError(
                "未找到 mihomo；请安装 Mihomo 或通过 HX_MIHOMO_BIN 指定可执行文件"
            )
        return executable

    def _wait_ready(self, instance: _Instance, port: int) -> None:
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            if instance.process.poll() is not None:
                raise ManagedMihomoError("本机 Mihomo 在监听就绪前退出")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        raise ManagedMihomoError("等待本机 Mihomo 监听就绪超时")

    def _stop_locked(self, node_index: int) -> None:
        instance = self._instances.pop(node_index, None)
        if instance is None:
            return
        process = instance.process
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        instance.log_handle.close()
        instance.directory.cleanup()


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
