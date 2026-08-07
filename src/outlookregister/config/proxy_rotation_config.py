"""Validation helpers for the HX-ProxyGroup public rotation endpoint."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,128}$")
_CONTROL_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_CONTROL_PLANE_SCHEMES = {"http", "https"}
_PROXY_SCHEMES = {"http", "https", "socks5"}


@dataclass(frozen=True)
class ControlPlaneURL:
    """An origin plus an optional token from a pasted /rot/<token> URL."""

    origin: str
    embedded_token: str = ""


def parse_residential_control_url(value: str) -> ControlPlaneURL:
    """Validate an exact public ``/ctl/<token>`` residential control URL."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("proxy_rotation.control_url 不能为空")
    if any(character.isspace() for character in raw):
        raise ValueError("proxy_rotation.control_url 不能包含空白字符")
    try:
        parsed = urlsplit(raw)
        parsed.port
    except ValueError as exc:
        raise ValueError("proxy_rotation.control_url 的端口无效") from exc

    scheme = parsed.scheme.casefold()
    host = parsed.hostname or ""
    if scheme not in _CONTROL_PLANE_SCHEMES or not host:
        raise ValueError("proxy_rotation.control_url 必须是 http 或 https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("proxy_rotation.control_url 不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("proxy_rotation.control_url 不能包含查询参数或片段")
    if scheme != "https" and not _is_loopback_host(host):
        raise ValueError("远程 HX-ProxyGroup 必须使用 https；http 仅允许回环地址")

    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or parts[0] != "ctl":
        raise ValueError(
            "proxy_rotation.control_url 必须是完整 /ctl/<token> URL"
        )
    token = str(parts[1] or "").strip()
    if not _CONTROL_TOKEN_PATTERN.fullmatch(token):
        raise ValueError(
            "HX-ProxyGroup control token 必须是 16 到 128 位字母、数字、'-' 或 '_'"
        )
    origin = urlunsplit((scheme, parsed.netloc, "", "", ""))
    return ControlPlaneURL(origin=origin, embedded_token=token)


def parse_remote_residential_control_url(value: str) -> ControlPlaneURL:
    """Parse a residential control URL reachable from this host."""
    endpoint = parse_residential_control_url(value)
    host = urlsplit(endpoint.origin).hostname or ""
    if _is_loopback_host(host):
        raise ValueError("HX-ProxyGroup 远程控制面不能使用回环地址")
    return endpoint


def parse_control_plane_url(value: str) -> ControlPlaneURL:
    """Validate and normalize a control-plane origin or rotation URL.

    The canonical configuration is an origin plus a separate token. For
    compatibility, an exact ``/rot/<token>`` URL is also accepted and reduced
    to the origin; the caller must verify that the embedded token matches its
    token entry.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("proxy_rotation.base_url 不能为空")
    if any(character.isspace() for character in raw):
        raise ValueError("proxy_rotation.base_url 不能包含空白字符")
    try:
        parsed = urlsplit(raw)
        parsed.port
    except ValueError as exc:
        raise ValueError("proxy_rotation.base_url 的端口无效") from exc

    scheme = parsed.scheme.casefold()
    host = parsed.hostname or ""
    if scheme not in _CONTROL_PLANE_SCHEMES or not host:
        raise ValueError("proxy_rotation.base_url 必须是 http 或 https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("proxy_rotation.base_url 不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("proxy_rotation.base_url 不能包含查询参数或片段")
    if scheme != "https" and not _is_loopback_host(host):
        raise ValueError("远程 HX-ProxyGroup 必须使用 https；http 仅允许回环地址")

    path = parsed.path.rstrip("/")
    embedded_token = ""
    if path:
        parts = path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "rot":
            raise ValueError(
                "proxy_rotation.base_url 只能是控制面根地址或完整 /rot/<token> URL"
            )
        embedded_token = validate_rotation_token(parts[1])

    origin = urlunsplit((scheme, parsed.netloc, "", "", ""))
    return ControlPlaneURL(origin=origin, embedded_token=embedded_token)


def parse_remote_control_plane_url(value: str) -> ControlPlaneURL:
    """Parse a control-plane URL that must be reachable outside this host."""
    endpoint = parse_control_plane_url(value)
    host = urlsplit(endpoint.origin).hostname or ""
    if _is_loopback_host(host):
        raise ValueError("HX-ProxyGroup 远程控制面不能使用回环地址")
    return endpoint


def validate_rotation_token(value: str) -> str:
    """Return a safe token string or raise without echoing its value."""
    token = str(value or "").strip()
    if not _TOKEN_PATTERN.fullmatch(token):
        raise ValueError(
            "HX-ProxyGroup token 必须是 4 到 128 位字母、数字、'-' 或 '_'"
        )
    return token


def validate_proxy_endpoint(value: str) -> str:
    """Validate the local HTTP/SOCKS endpoint used for browser traffic."""
    proxy = str(value or "").strip()
    if not proxy:
        raise ValueError("代理入口不能为空")
    if any(character.isspace() for character in proxy):
        raise ValueError("代理入口不能包含空白字符")
    try:
        parsed = urlsplit(proxy)
        parsed.port
    except ValueError as exc:
        raise ValueError("代理入口的端口无效") from exc
    if parsed.scheme.casefold() not in _PROXY_SCHEMES or not parsed.hostname:
        raise ValueError("代理入口必须是 http、https 或 socks5 URL")
    if parsed.query or parsed.fragment:
        raise ValueError("代理入口不能包含查询参数或片段")
    if parsed.path not in ("", "/"):
        raise ValueError("代理入口不能包含路径")
    return proxy


def validate_remote_proxy_endpoint(value: str) -> str:
    """Validate a browser proxy endpoint that must not be local to this host."""
    proxy = validate_proxy_endpoint(value)
    if _is_loopback_host(urlsplit(proxy).hostname or ""):
        raise ValueError("HX-ProxyGroup 远程数据面不能使用回环地址")
    return proxy


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().strip("[]").rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
