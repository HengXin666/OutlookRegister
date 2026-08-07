from __future__ import annotations

import ipaddress
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from outlookregister.config.identity_profiles import (
    is_valid_country_code,
    is_valid_timezone,
    select_identity_profile,
)
from outlookregister.proxy.proxy_pool_types import (
    ProxyRotationError,
)


class _ProxyPoolVerify:
    def _rotate(self, entry):
        last_error = None

        for attempt in range(self.max_rotate_retries + 1):
            try:
                response = self._request(
                    "POST",
                    f"/rot/{entry['token']}/next",
                )
            except ProxyRotationError as exc:
                last_error = str(exc)
            else:
                if response.status_code == 200:
                    try:
                        payload = response.json()
                        detail = f"session {payload.get('session_index')}/{payload.get('pool_size')}"
                        if payload.get("exit_ip"):
                            detail += f" exit_ip={payload['exit_ip']}"
                        print(f"[ProxyRotate] 服务端换 IP 成功 - {detail}")
                    except (ValueError, KeyError):
                        print("[ProxyRotate] 服务端换 IP 成功")
                    return
                if response.status_code == 429:
                    last_error = "服务端限流(rotate_rate_limited)"
                    time.sleep(2 * (attempt + 1))
                    continue
                last_error = (
                    f"HTTP {response.status_code} "
                    f"({self._response_detail(response)})"
                )
            time.sleep(0.5 * (attempt + 1))

        raise ProxyRotationError(last_error or "未知错误")

    def _verify(self, proxy):
        """通过候选代理请求出口 IP 回显接口，确认代理真实可用。"""
        endpoint_label = self._proxy_label(proxy)
        try:
            with self._request_lock:
                response = self._session.get(
                    self.exit_ip_endpoint,
                    proxies={"http": proxy, "https": proxy},
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            if response.status_code == 407:
                raise ProxyRotationError(
                    self._listener_error(
                        requests.HTTPError("407 Proxy Authentication Required"),
                        endpoint_label,
                    )
                )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                payload = None
            return self._parse_exit_ip(payload, response.text)
        except ProxyRotationError:
            raise
        except requests.exceptions.ProxyError as exc:
            raise ProxyRotationError(
                self._listener_error(exc, endpoint_label)
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise ProxyRotationError(
                f"HX-ProxyGroup Listener 请求超时: {endpoint_label}"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise ProxyRotationError(
                self._listener_error(exc, endpoint_label)
            ) from exc
        except (requests.RequestException, OSError) as exc:
            raise ProxyRotationError("代理可用性检查失败") from exc
        except ValueError as exc:
            raise ProxyRotationError(f"出口 IP 接口返回异常: {exc}") from exc

    def _verify_exit_identity(
        self,
        proxy,
        expected_country="",
        *,
        verify_listener_credentials=True,
    ):
        """Verify the residential exit and derive a matching browser identity."""
        endpoint_label = self._proxy_label(proxy)
        try:
            with self._request_lock:
                response = self._session.get(
                    self.identity_endpoint,
                    proxies={"http": proxy, "https": proxy},
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            if response.status_code == 407:
                raise ProxyRotationError(
                    self._listener_error(
                        requests.HTTPError("407 Proxy Authentication Required"),
                        endpoint_label,
                    )
                )
            if response.status_code != 200:
                raise ProxyRotationError(
                    f"住宅身份探针失败: HTTP {response.status_code} "
                    f"({self._response_detail(response)})"
                )
            payload = self._json(response, "住宅身份探针")
            if payload.get("success") is False:
                raise ProxyRotationError("住宅身份探针返回失败")
            exit_ip = self._parse_exit_ip(payload, response.text)
            country_code = str(payload.get("country_code") or "").strip().upper()
            if not is_valid_country_code(country_code):
                raise ProxyRotationError("住宅身份探针没有返回有效国家代码")
            expected = str(expected_country or "").strip().upper()
            if expected and expected != country_code:
                raise ProxyRotationError("HX-ProxyGroup 国家回显与住宅出口国家不一致")
            raw_timezone = payload.get("timezone")
            if isinstance(raw_timezone, dict):
                timezone = str(raw_timezone.get("id") or "").strip()
            else:
                timezone = str(raw_timezone or "").strip()
            if not timezone or not is_valid_timezone(timezone):
                raise ProxyRotationError("住宅身份探针没有返回有效时区")
            profile = select_identity_profile({
                "country_code": country_code,
                "timezone": timezone,
            })
            if verify_listener_credentials:
                self._verify_listener_credentials(proxy)
            return {
                "exit_ip": exit_ip,
                "country_code": country_code,
                "browser_locale": profile["browser_locale"],
                "timezone": profile["timezone"],
            }
        except ProxyRotationError:
            raise
        except requests.exceptions.ProxyError as exc:
            raise ProxyRotationError(
                self._listener_error(exc, endpoint_label)
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise ProxyRotationError(
                f"HX-ProxyGroup Listener 请求超时: {endpoint_label}"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise ProxyRotationError(
                self._listener_error(exc, endpoint_label)
            ) from exc
        except (requests.RequestException, OSError) as exc:
            raise ProxyRotationError("住宅身份探针请求失败") from exc

    def _verify_listener_credentials(self, proxy):
        """Reject a generic no-auth proxy masquerading as the HX Listener."""
        parsed = urlsplit(proxy)
        if parsed.scheme.casefold() not in ("http", "https"):
            return
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        listener = urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))
        invalid_proxy = self._proxy_with_credentials(
            listener,
            "__hx_invalid_session__",
            "__hx_invalid_secret__",
        )
        try:
            with self._request_lock:
                response = self._session.get(
                    self.identity_endpoint,
                    proxies={"http": invalid_proxy, "https": invalid_proxy},
                    timeout=self.timeout,
                    allow_redirects=False,
                )
        except requests.exceptions.ProxyError as exc:
            if "407" in str(exc) or "proxy authentication required" in str(exc).casefold():
                return
            raise ProxyRotationError(
                f"HX-ProxyGroup Listener 无法验证会话认证: {listener}"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise ProxyRotationError(
                f"HX-ProxyGroup Listener 认证探针超时: {listener}"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise ProxyRotationError(
                self._listener_error(exc, listener)
            ) from exc
        except requests.RequestException as exc:
            raise ProxyRotationError(
                f"HX-ProxyGroup Listener 认证探针失败: {listener}"
            ) from exc
        if response.status_code != 407:
            raise ProxyRotationError(
                f"HX-ProxyGroup Listener 未强制校验会话认证: {listener}"
            )

    @staticmethod
    def _proxy_label(proxy):
        """Return a proxy endpoint label without userinfo or credentials."""
        try:
            parsed = urlsplit(str(proxy or ""))
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            if parsed.port:
                host = f"{host}:{parsed.port}"
            return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        except (TypeError, ValueError):
            return "远程数据面"

    @staticmethod
    def _parse_exit_ip(payload, text=""):
        if isinstance(payload, dict):
            candidate = payload.get("ip") or payload.get("origin") or ""
        else:
            candidate = text
        if isinstance(candidate, (list, tuple)):
            candidates = candidate
        else:
            candidates = str(candidate or "").replace("\n", ",").split(",")
        for value in candidates:
            value = str(value or "").strip()
            if not value:
                continue
            try:
                return str(ipaddress.ip_address(value))
            except ValueError:
                continue
        raise ProxyRotationError("出口 IP 接口没有返回有效 IP 地址")
