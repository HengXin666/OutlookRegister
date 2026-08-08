"""本地 Mihomo 配置与协议解析参数构造。

这些纯函数从 managed_mihomo 中抽出，保持文件 ≤300 行；可被 managed_mihomo 复用。
"""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, unquote, urlsplit


class ManagedMihomoError(Exception):
    """A local data-plane endpoint could not be validated or started."""


SUPPORTED_PROTOCOLS = {"vless", "vmess", "trojan"}
SUPPORTED_RESIDENTIAL_PROTOCOLS = {"http", "socks5"}

def build_mihomo_config(endpoint: dict, local_port: int) -> dict:
    """Convert one HX endpoint into a loopback-only Mihomo configuration."""
    if not isinstance(endpoint, dict):
        raise ManagedMihomoError("HX-ProxyGroup 数据端点格式错误")
    protocol = str(endpoint.get("protocol") or "").strip().casefold()
    transport = str(endpoint.get("transport") or "").strip().casefold()
    uri = str(endpoint.get("uri") or "").strip()
    if not isinstance(local_port, int) or local_port < 1 or local_port > 65535:
        raise ManagedMihomoError("本机 Mihomo 监听端口无效")

    if protocol in SUPPORTED_RESIDENTIAL_PROTOCOLS and transport == "tcp":
        proxy = _parse_residential_endpoint(protocol, endpoint)
    elif protocol in SUPPORTED_PROTOCOLS and transport == "ws":
        if not uri or len(uri) > 8192 or any(character.isspace() for character in uri):
            raise ManagedMihomoError("HX-ProxyGroup 数据端点 URI 无效")
        proxy = _parse_vmess(uri) if protocol == "vmess" else _parse_standard_uri(protocol, uri)
    else:
        raise ManagedMihomoError(
            "本机 Mihomo 只接受 HTTP/SOCKS5 住宅端点或 VLESS/VMess/Trojan WebSocket 端点"
        )
    proxy["name"] = "hx-residential"
    return {
        "mode": "rule",
        "log-level": "warning",
        "allow-lan": False,
        "dns": {
            "enable": True,
            "ipv6": False,
            "enhanced-mode": "redir-host",
            # 住宅数据面域名解析必须优先走系统 DNS：1.1.1.1/8.8.8.8 的 DoH
            # 在部分网络（如 Clash TUN + 国内出口）不可达，会导致
            # "dns resolve failed: couldn't find ip" 而无法拨号住宅节点。
            # system 解析失败时再回退到 DoH。
            "nameserver": [
                "system",
                "https://1.1.1.1/dns-query",
                "https://8.8.8.8/dns-query",
            ],
            "proxy-server-nameserver": [
                "system",
                "https://1.1.1.1/dns-query",
                "https://8.8.8.8/dns-query",
            ],
        },
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


def _parse_residential_endpoint(protocol: str, endpoint: dict) -> dict:
    server = str(endpoint.get("server") or "").strip()
    username = str(endpoint.get("username") or "")
    password = str(endpoint.get("password") or "")
    try:
        port = int(endpoint.get("port"))
    except (TypeError, ValueError) as exc:
        raise ManagedMihomoError("HX-ProxyGroup 住宅端点端口无效") from exc
    if (
        not server
        or len(server) > 253
        or any(character.isspace() for character in server)
        or port < 1
        or port > 65535
    ):
        raise ManagedMihomoError("HX-ProxyGroup 住宅端点主机或端口无效")
    if len(username) > 512 or len(password) > 1024 or (password and not username):
        raise ManagedMihomoError("HX-ProxyGroup 住宅端点鉴权字段无效")
    tls = endpoint.get("tls") is True
    if protocol == "socks5" and tls:
        raise ManagedMihomoError("SOCKS5 住宅端点不能启用 HTTP TLS")
    proxy = {
        "type": protocol,
        "server": server,
        "port": port,
    }
    if username:
        proxy["username"] = username
        proxy["password"] = password
    if tls:
        proxy["tls"] = True
    if protocol == "socks5":
        proxy["udp"] = True
    return proxy


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
