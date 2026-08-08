"""Credential-safe HTTP helpers shared by every proxy pool implementation.

Extracted from ``proxy_pool_sessions`` so the manual list pool can reuse the
same response/error redaction rules as the residential pool without pulling in
the HX-ProxyGroup control-plane logic.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

from outlookregister.config.proxy_rotation_config import validate_proxy_endpoint
from outlookregister.proxy.proxy_pool_types import ProxyRotationError


class _ProxyPoolHTTPHelpers:
    @staticmethod
    def _json(response, action):
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProxyRotationError(f"{action}响应不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ProxyRotationError(f"{action}响应格式错误")
        return payload

    @staticmethod
    def _response_detail(response):
        """Return a non-sensitive error hint without echoing response bodies."""
        if 300 <= response.status_code < 400:
            return "redirect_blocked"
        try:
            payload = response.json()
        except (AttributeError, ValueError):
            return "unstructured_error"
        if not isinstance(payload, dict):
            return "unstructured_error"
        error = payload.get("error")
        if isinstance(error, dict) and error.get("code"):
            return str(error["code"])
        if payload.get("code"):
            return str(payload["code"])
        return "unstructured_error"

    @staticmethod
    def _listener_error(exc, listener=""):
        """Classify listener failures without exposing proxy credentials."""
        detail = str(exc).casefold()
        if "407" in detail or "proxy authentication required" in detail:
            message = "HX-ProxyGroup Listener 认证失败"
        elif any(
            marker in detail
            for marker in (
                "connection refused",
                "failed to establish a new connection",
                "newconnectionerror",
            )
        ):
            message = "HX-ProxyGroup Listener 未监听或连接被拒绝"
        else:
            message = "HX-ProxyGroup Listener 请求失败"
        return f"{message}: {listener}" if listener else message

    @staticmethod
    def _proxy_with_credentials(proxy, username, password):
        try:
            proxy = validate_proxy_endpoint(proxy)
        except ValueError as exc:
            raise ProxyRotationError(str(exc)) from exc
        parsed = urlsplit(proxy)
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        credentials = f"{quote(username, safe='')}:{quote(password, safe='')}@"
        return urlunsplit((
            parsed.scheme,
            credentials + host,
            parsed.path,
            parsed.query,
            parsed.fragment,
        ))
