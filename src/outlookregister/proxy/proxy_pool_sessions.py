from __future__ import annotations

import uuid
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from outlookregister.config.proxy_rotation_config import (
    validate_proxy_endpoint,
    validate_remote_proxy_endpoint,
)
from outlookregister.proxy.proxy_pool_types import (
    ProxyLease,
    ProxyRotationError,
)


class _ProxyPoolSessions:
    def _create_session(self, entry, country_code=""):
        session_id = uuid.uuid4().hex
        entry_country = str(entry.get("country_code") or "").strip()
        if (
            country_code
            and entry_country
            and country_code.casefold() != entry_country.casefold()
        ):
            raise ProxyRotationError(
                f"渠道只支持国家 {entry_country}，不能分配 {country_code}"
            )
        requested_country = str(
            country_code or entry_country or ""
        ).strip()
        request_body = {"country_code": requested_country} if requested_country else None
        response = self._request(
            "PUT",
            f"/rot/{entry['token']}/sessions/{session_id}",
            json=request_body,
        )
        if response.status_code != 200:
            if response.status_code == 404:
                raise ProxyRotationError(
                    "HX-ProxyGroup 住宅 URL 的 Token 无效或已失效: HTTP 404"
                )
            raise ProxyRotationError(
                f"创建窗口会话失败: HTTP {response.status_code} ({self._response_detail(response)})"
            )
        try:
            payload = self._json(response, "创建窗口会话")
            username = str(payload.get("proxy_username") or "")
            password = str(payload.get("proxy_password") or "")
            if not username or not password:
                raise ProxyRotationError("创建窗口会话响应缺少代理账号或密码")
            if payload.get("session_id") != session_id:
                raise ProxyRotationError("创建窗口会话响应的 session_id 不一致")
            returned_country = str(payload.get("country_code") or "").strip()
            if requested_country and returned_country and returned_country.casefold() != requested_country.casefold():
                raise ProxyRotationError(
                    f"HX-ProxyGroup 返回的国家与请求不一致: expected={requested_country}, actual={returned_country}"
                )
            if requested_country and not returned_country and self.require_country_echo:
                raise ProxyRotationError("HX-ProxyGroup 未回显 country_code，无法确认国家约束")
            # The current sticky-session API allocates nodes on demand and
            # omits the legacy pool_size field. Keep the compatibility check
            # only when the server explicitly sends that field.
            if self.required_pool_size and "pool_size" in payload:
                try:
                    pool_size = int(payload["pool_size"])
                except (TypeError, ValueError) as exc:
                    raise ProxyRotationError("代理池容量字段无效") from exc
                if pool_size < self.required_pool_size:
                    raise ProxyRotationError(
                        f"代理池容量不足: pool_size={pool_size}, "
                        f"required={self.required_pool_size}"
                    )
            proxy = self._proxy_from_session_payload(
                payload,
                entry,
                username,
                password,
            )
        except ProxyRotationError:
            # PUT may already have allocated a pool slot even if its response is
            # malformed or cannot be converted into browser proxy settings.
            self.release(ProxyLease(
                proxy="",
                token=entry["token"],
                session_id=session_id,
                session_scoped=True,
            ))
            raise
        if "pool_size" in payload:
            allocation_detail = (
                f"slot={payload.get('session_index')}/{payload.get('pool_size')}"
            )
        else:
            allocation_detail = f"session_index={payload.get('session_index')}"
        print(
            "[ProxyRotate] 已创建独立窗口会话 - "
            f"session_id={session_id}, {allocation_detail}"
        )
        return ProxyLease(
            proxy=proxy,
            token=entry["token"],
            session_id=session_id,
            session_scoped=True,
            country_code=returned_country or requested_country,
        )

    def _proxy_from_session_payload(self, payload, entry, username, password):
        """Resolve the business-data endpoint returned by HX for this session.

        Automatic mode must never infer a local Listener. Older deployments
        remain supported only through the explicit token-list configuration.
        """
        endpoint = (
            payload.get("browser_proxy")
            or payload.get("proxy_endpoint")
            or payload.get("client_proxy")
        )
        if self.auto_identity:
            if isinstance(endpoint, str):
                endpoint = {"url": endpoint}
            if not isinstance(endpoint, dict):
                raise ProxyRotationError(
                    "HX-ProxyGroup 未返回远程数据面入口；不能回退到本地代理"
                )
            endpoint_type = str(endpoint.get("type") or "").strip().casefold()
            if endpoint_type in {"vless-ws", "vmess-ws", "trojan-ws"}:
                raise ProxyRotationError(
                    "HX-ProxyGroup 当前仅返回 WebSocket 线路；"
                    "OutlookRegister 需要远程 http-connect 或 socks5 入口"
                )
            server = str(endpoint.get("server") or endpoint.get("url") or "").strip()
            if not server:
                raise ProxyRotationError("HX-ProxyGroup 远程数据面缺少 server")
            if endpoint_type == "socks":
                endpoint_type = "socks5"
            if not endpoint_type:
                inferred_scheme = urlsplit(server).scheme.casefold()
                endpoint_type = (
                    "socks5" if inferred_scheme == "socks5" else "http-connect"
                )
            if endpoint_type not in {"http", "http-connect", "socks5"}:
                raise ProxyRotationError(
                    "HX-ProxyGroup 返回了不支持的远程数据面协议"
                )
            if "://" not in server:
                scheme = "https" if bool(endpoint.get("tls")) else (
                    "socks5" if endpoint_type == "socks5" else "http"
                )
                port = endpoint.get("port")
                try:
                    port = int(port)
                except (TypeError, ValueError) as exc:
                    raise ProxyRotationError(
                        "HX-ProxyGroup 远程数据面端口无效"
                    ) from exc
                if port < 1 or port > 65535:
                    raise ProxyRotationError("HX-ProxyGroup 远程数据面端口无效")
                authority = server
                if ":" in authority and not authority.startswith("["):
                    authority = f"[{authority}]"
                server = f"{scheme}://{authority}:{port}"
            try:
                if endpoint_type == "socks5" and urlsplit(server).scheme.casefold() != "socks5":
                    raise ValueError("socks5 数据面必须使用 socks5 URL")
                if endpoint_type in {"http", "http-connect"} and urlsplit(server).scheme.casefold() not in {"http", "https"}:
                    raise ValueError("HTTP CONNECT 数据面必须使用 http 或 https URL")
                remote_server = validate_remote_proxy_endpoint(server)
                return self._proxy_with_credentials(remote_server, username, password)
            except (TypeError, ValueError) as exc:
                raise ProxyRotationError(
                    f"HX-ProxyGroup 远程数据面地址无效: {exc}"
                ) from exc

        if not entry.get("proxy"):
            raise ProxyRotationError("代理渠道缺少数据面入口")
        return self._proxy_with_credentials(entry["proxy"], username, password)

    def _request(self, method, path, **kwargs):
        try:
            with self._request_lock:
                kwargs.setdefault("allow_redirects", False)
                return self._session.request(
                    method,
                    f"{self.base_url}{path}",
                    timeout=self.timeout,
                    **kwargs,
                )
        except requests.RequestException as exc:
            raise ProxyRotationError("HX-ProxyGroup 控制面请求失败") from exc

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
        return urlunsplit((parsed.scheme, credentials + host, parsed.path, parsed.query, parsed.fragment))
