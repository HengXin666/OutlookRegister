from dataclasses import dataclass
import threading
import time
from urllib.parse import quote, urlsplit, urlunsplit
import uuid

import requests


class ProxyRotationError(Exception):
    """住宅代理服务端换 IP 失败时抛出。"""


@dataclass(frozen=True)
class ProxyLease:
    proxy: str
    token: str
    session_id: str = ""
    session_scoped: bool = False


class RotatingProxyPool:
    """
    对接 HX-ProxyGroup 的住宅代理换 IP 接口。

    每次整体注册流程开始前调用 acquire_proxy()：

      1. 为当前窗口生成独立 session_id；
      2. 使用一个渠道 token 创建服务端逻辑会话；
      3. 将服务端签发的代理账号写入同一个 listener 地址；
      4. 注册完成后按 session_id 切到配置的低成本线路，不关闭浏览器页面。
    """

    def __init__(self, config):
        self.base_url = str(config.get("base_url", "")).strip().rstrip("/")
        self.timeout = float(config.get("timeout_seconds", 10))
        self.max_rotate_retries = int(config.get("max_rotate_retries", 2))
        self.session_scoped = bool(config.get("session_scoped", True))
        self.post_registration_route = str(
            config.get("post_registration_route", "direct")
        ).strip().lower()
        self.check_proxy = bool(config.get("check_proxy", False))
        self.exit_ip_endpoint = str(config.get("exit_ip_endpoint", "https://api.ipify.org?format=json"))

        self.entries = []
        for entry in config.get("tokens", []) or []:
            token = str(entry.get("token", "")).strip()
            proxy = str(entry.get("proxy", "")).strip()
            if token and proxy:
                self.entries.append({"token": token, "proxy": proxy})

        if not self.base_url:
            raise ProxyRotationError("proxy_rotation.base_url 不能为空")
        if not self.entries:
            raise ProxyRotationError("proxy_rotation.tokens 至少需要配置一个 {token, proxy} 渠道")
        if self.post_registration_route not in ("direct", "upstream"):
            raise ProxyRotationError(
                "proxy_rotation.post_registration_route 只支持 direct 或 upstream"
            )

        self._session = requests.Session()
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._next_index = 0

    def acquire_proxy(self):
        """
        为本次注册流程获取独立代理租约。
        session_scoped=true 时多个窗口可复用同一个 token 和 listener。
        开启 check_proxy 时，会在切换后通过该代理请求出口 IP 回显接口，
        确认代理真实可用才返回，避免用坏代理浪费一次注册机会。
        """
        with self._lock:
            start_index = self._next_index % len(self.entries)
            self._next_index += 1

        errors = []
        for offset in range(len(self.entries)):
            entry = self.entries[(start_index + offset) % len(self.entries)]
            lease = None
            try:
                if self.session_scoped:
                    lease = self._create_session(entry)
                else:
                    self._rotate(entry)
                    lease = ProxyLease(proxy=entry["proxy"], token=entry["token"])
                if self.check_proxy:
                    exit_ip = self._verify(lease.proxy)
                    print(f"[ProxyRotate] 代理可用性检查通过 - exit_ip={exit_ip}")
                return lease
            except ProxyRotationError as exc:
                if lease is not None and lease.session_scoped:
                    self.release(lease)
                errors.append(f"token {entry['token'][:8]}...: {exc}")

        raise ProxyRotationError("所有住宅代理渠道切换失败: " + " | ".join(errors))

    def switch_after_registration(self, lease):
        """Keep the browser open while moving to the configured low-cost route."""
        self._switch_route(lease, self.post_registration_route)

    def switch_to_direct(self, lease):
        """Compatibility helper for callers that explicitly require DIRECT."""
        self._switch_route(lease, "direct")

    def _switch_route(self, lease, route_mode):
        if not lease or not lease.session_scoped:
            return
        response = self._request(
            "POST",
            f"/rot/{lease.token}/sessions/{lease.session_id}/route",
            json={"route_mode": route_mode},
        )
        if response.status_code != 200:
            raise ProxyRotationError(
                f"会话切换 {route_mode} 失败: HTTP {response.status_code}: {response.text[:200]}"
            )
        payload = self._json(response, f"会话切换 {route_mode}")
        if payload.get("route_mode") != route_mode:
            raise ProxyRotationError(f"会话切换 {route_mode} 响应状态不一致")

    def release(self, lease):
        """Release server-side credentials after the browser has closed."""
        if not lease or not lease.session_scoped:
            return
        try:
            response = self._request(
                "DELETE",
                f"/rot/{lease.token}/sessions/{lease.session_id}",
            )
            if response.status_code not in (204, 404):
                print(f"[ProxyRotate] 释放会话失败 - HTTP {response.status_code}")
        except ProxyRotationError as exc:
            print(f"[ProxyRotate] 释放会话失败 - {exc}")

    def _create_session(self, entry):
        session_id = uuid.uuid4().hex[:12]
        response = self._request(
            "PUT",
            f"/rot/{entry['token']}/sessions/{session_id}",
        )
        if response.status_code != 200:
            raise ProxyRotationError(
                f"创建窗口会话失败: HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            payload = self._json(response, "创建窗口会话")
            username = str(payload.get("proxy_username") or "")
            password = str(payload.get("proxy_password") or "")
            if not username or not password:
                raise ProxyRotationError("创建窗口会话响应缺少代理账号或密码")
            proxy = self._proxy_with_credentials(entry["proxy"], username, password)
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
        print(
            "[ProxyRotate] 已创建独立窗口会话 - "
            f"session_id={session_id}, slot={payload.get('session_index')}/{payload.get('pool_size')}"
        )
        return ProxyLease(
            proxy=proxy,
            token=entry["token"],
            session_id=session_id,
            session_scoped=True,
        )

    def _request(self, method, path, **kwargs):
        try:
            with self._request_lock:
                return self._session.request(
                    method,
                    f"{self.base_url}{path}",
                    timeout=self.timeout,
                    **kwargs,
                )
        except requests.RequestException as exc:
            raise ProxyRotationError(f"请求失败: {exc}") from exc

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
    def _proxy_with_credentials(proxy, username, password):
        parsed = urlsplit(proxy)
        if parsed.scheme not in ("http", "https", "socks5") or not parsed.hostname:
            raise ProxyRotationError("代理地址必须是 http、https 或 socks5 URL")
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        credentials = f"{quote(username, safe='')}:{quote(password, safe='')}@"
        return urlunsplit((parsed.scheme, credentials + host, parsed.path, parsed.query, parsed.fragment))

    def _rotate(self, entry):
        url = f"{self.base_url}/rot/{entry['token']}/next"
        last_error = None

        for attempt in range(self.max_rotate_retries + 1):
            try:
                with self._request_lock:
                    response = self._session.post(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"请求失败: {exc}"
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
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            time.sleep(0.5 * (attempt + 1))

        raise ProxyRotationError(last_error or "未知错误")

    def _verify(self, proxy):
        """通过候选代理请求出口 IP 回显接口，确认代理真实可用。"""
        try:
            with self._request_lock:
                response = self._session.get(
                    self.exit_ip_endpoint,
                    proxies={"http": proxy, "https": proxy},
                    timeout=self.timeout,
                )
            response.raise_for_status()
            payload = response.json()
            exit_ip = payload.get("ip") or payload.get("origin") or ""
            if exit_ip:
                return exit_ip
            return str(response.text).strip()
        except (requests.RequestException, OSError) as exc:
            raise ProxyRotationError(f"代理可用性检查失败: {exc}") from exc
        except ValueError as exc:
            raise ProxyRotationError(f"出口 IP 接口返回异常: {exc}") from exc
