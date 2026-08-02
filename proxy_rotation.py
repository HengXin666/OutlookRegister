from dataclasses import dataclass, replace
import ipaddress
import json
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
    exit_ip: str = ""


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
        self.verify_browser_exit_ip = bool(config.get("verify_browser_exit_ip", True))
        self.required_pool_size = max(int(config.get("required_pool_size", 0)), 0)
        self._enforce_unique_exit_ip = bool(
            config.get("enforce_unique_exit_ip", self.check_proxy)
        )

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
        self._session.trust_env = False
        self._lock = threading.Lock()
        self._allocation_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._active_exit_ips: dict[str, tuple[str, str]] = {}
        self._next_index = 0

        if self.enforce_unique_exit_ip and not self.check_proxy:
            raise ProxyRotationError(
                "enforce_unique_exit_ip=true 时必须同时启用 check_proxy"
            )
        if self.enforce_unique_exit_ip and not self.session_scoped:
            raise ProxyRotationError(
                "enforce_unique_exit_ip=true 时必须同时启用 session_scoped"
            )

    @property
    def enforce_unique_exit_ip(self):
        return bool(getattr(self, "_enforce_unique_exit_ip", False))

    def acquire_proxy(self):
        """
        为本次注册流程获取独立代理租约。
        session_scoped=true 时多个窗口可复用同一个 token 和 listener。
        开启 check_proxy 时，会在切换后通过该代理请求出口 IP 回显接口，
        确认代理真实可用才返回，避免用坏代理浪费一次注册机会。
        """
        # Serialize allocation and verification so two concurrent workers cannot
        # both reserve the same observed exit IP between the check and insert.
        with self._allocation_lock:
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
                        lease = replace(lease, exit_ip=exit_ip)
                        self._reserve_exit_ip(lease)
                        print(
                            "[ProxyRotate] 代理可用性检查通过 - "
                            f"session_id={lease.session_id}, exit_ip={exit_ip}"
                        )
                    return lease
                except ProxyRotationError as exc:
                    if lease is not None:
                        self.release(lease)
                    errors.append(f"token {entry['token'][:8]}...: {exc}")

            raise ProxyRotationError("所有住宅代理渠道切换失败: " + " | ".join(errors))

    def switch_after_registration(self, lease):
        """Move a completed flow to the configured route after its browser is closed."""
        return self._switch_route(
            lease,
            self.post_registration_route,
            verify_exit_ip=False,
        )

    def switch_to_direct(self, lease):
        """Compatibility helper for callers that explicitly require DIRECT."""
        return self._switch_route(lease, "direct")

    def verify_browser_page(self, page, lease):
        """Verify that a browser page uses the same exit IP as its lease."""
        if (
            not self.verify_browser_exit_ip
            or not self.check_proxy
            or not lease
            or not lease.exit_ip
        ):
            return
        try:
            page.goto(
                self.exit_ip_endpoint,
                timeout=int(self.timeout * 1000),
                wait_until="domcontentloaded",
            )
            body = page.locator("body").inner_text(timeout=5000).strip()
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            browser_ip = self._parse_exit_ip(payload, body)
        except Exception as exc:
            raise ProxyRotationError(f"浏览器出口 IP 验证失败: {exc}") from exc
        if browser_ip != lease.exit_ip:
            raise ProxyRotationError(
                f"浏览器出口 IP 与 lease 不一致: expected={lease.exit_ip}, actual={browser_ip}"
            )
        print(
            "[ProxyRotate] 浏览器出口 IP 验证通过 - "
            f"session_id={lease.session_id}, exit_ip={browser_ip}"
        )

    def _switch_route(self, lease, route_mode, verify_exit_ip=True):
        if not lease or not lease.session_scoped:
            return lease
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
        if not self.check_proxy or not verify_exit_ip:
            return lease

        exit_ip = self._verify(lease.proxy)
        updated = replace(lease, exit_ip=exit_ip)
        self._replace_exit_ip(lease, updated)
        print(
            "[ProxyRotate] 会话出口已重新确认 - "
            f"session_id={lease.session_id}, route={route_mode}, exit_ip={exit_ip}"
        )
        return updated

    def release(self, lease):
        """Release server-side credentials after the browser has closed."""
        if not lease:
            return
        if not lease.session_scoped:
            self._release_exit_ip(lease)
            return
        try:
            response = self._request(
                "DELETE",
                f"/rot/{lease.token}/sessions/{lease.session_id}",
            )
            if response.status_code not in (204, 404):
                print(f"[ProxyRotate] 释放会话失败 - HTTP {response.status_code}")
                return
            self._release_exit_ip(lease)
        except ProxyRotationError as exc:
            print(f"[ProxyRotate] 释放会话失败 - {exc}")

    def _reserve_exit_ip(self, lease):
        if not self.enforce_unique_exit_ip or not lease.exit_ip:
            return
        owner = (lease.token, lease.session_id)
        with self._lock:
            current_owner = self._active_exit_ips.get(lease.exit_ip)
            if current_owner is not None and current_owner != owner:
                raise ProxyRotationError(
                    f"活动窗口出口 IP 重复: {lease.exit_ip} "
                    f"(已有 session_id={current_owner[1]})"
                )
            self._active_exit_ips[lease.exit_ip] = owner

    def _replace_exit_ip(self, previous, updated):
        if not self.enforce_unique_exit_ip or not updated.exit_ip:
            return
        owner = (updated.token, updated.session_id)
        with self._lock:
            current_owner = self._active_exit_ips.get(updated.exit_ip)
            if current_owner is not None and current_owner != owner:
                raise ProxyRotationError(
                    f"切换后活动窗口出口 IP 重复: {updated.exit_ip} "
                    f"(已有 session_id={current_owner[1]})"
                )
            if previous.exit_ip and self._active_exit_ips.get(previous.exit_ip) == owner:
                del self._active_exit_ips[previous.exit_ip]
            self._active_exit_ips[updated.exit_ip] = owner

    def _release_exit_ip(self, lease):
        if not self.enforce_unique_exit_ip or not lease.exit_ip:
            return
        owner = (lease.token, lease.session_id)
        with self._lock:
            if self._active_exit_ips.get(lease.exit_ip) == owner:
                del self._active_exit_ips[lease.exit_ip]

    def _create_session(self, entry):
        session_id = uuid.uuid4().hex
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
            try:
                payload = response.json()
            except ValueError:
                payload = None
            return self._parse_exit_ip(payload, response.text)
        except (requests.RequestException, OSError) as exc:
            raise ProxyRotationError(f"代理可用性检查失败: {exc}") from exc
        except ValueError as exc:
            raise ProxyRotationError(f"出口 IP 接口返回异常: {exc}") from exc

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
