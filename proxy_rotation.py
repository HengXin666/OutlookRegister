from dataclasses import dataclass, replace
import hashlib
import ipaddress
import json
import threading
import time
import weakref
from urllib.parse import quote, urlsplit, urlunsplit
import uuid

import requests

from managed_mihomo import ManagedMihomo, ManagedMihomoError, SUPPORTED_PROTOCOLS

from proxy_rotation_config import (
    parse_control_plane_url,
    parse_remote_control_plane_url,
    parse_remote_residential_control_url,
    validate_proxy_endpoint,
    validate_remote_proxy_endpoint,
    validate_rotation_token,
)
from identity_profiles import (
    is_valid_country_code,
    is_valid_timezone,
    select_identity_profile,
)


class ProxyRotationError(Exception):
    """住宅代理服务端换 IP 失败时抛出。"""


class _DeclaredLeaseState:
    """Process-wide ownership for one high-privilege control URL."""

    def __init__(self):
        self.lock = threading.RLock()
        self.leased_node_indexes: set[int] = set()
        self.active_exit_ips: dict[str, tuple[str, str]] = {}
        self.next_index = 0


_declared_states_lock = threading.Lock()
_declared_states = weakref.WeakValueDictionary()


def _declared_lease_state(control_url: str) -> _DeclaredLeaseState:
    # Hash the bearer URL so the process registry cannot accidentally expose it
    # through diagnostics or object representations.
    key = hashlib.sha256(control_url.encode("utf-8")).hexdigest()
    with _declared_states_lock:
        state = _declared_states.get(key)
        if state is None:
            state = _DeclaredLeaseState()
            _declared_states[key] = state
        return state


@dataclass(frozen=True)
class ProxyLease:
    proxy: str
    token: str
    session_id: str = ""
    session_scoped: bool = False
    exit_ip: str = ""
    country_code: str = ""
    browser_locale: str = ""
    timezone: str = ""
    node_index: int = 0
    node_name: str = ""


class RotatingProxyPool:
    """
    对接 HX-ProxyGroup 的住宅代理换 IP 接口。

    每次整体注册流程开始前调用 acquire_proxy()：

      1. 从渠道声明的固定节点池租用一个空闲节点；
      2. 调用该节点的 next 接口刷新住宅出口；
      3. 优先使用节点返回的浏览器兼容入口；否则把 VLESS/VMess/Trojan WS 端点落地到本机 Mihomo；
      4. 浏览器关闭后只释放进程内租约，不删除服务端节点。
    """

    def __init__(self, config):
        control_url = str(config.get("control_url") or "").strip()
        legacy_rotation_url = str(config.get("rotation_url") or "").strip()
        if control_url and legacy_rotation_url:
            raise ProxyRotationError(
                "proxy_rotation.control_url 与 rotation_url 不能同时配置"
            )
        self.declared_pool = bool(control_url)
        self.auto_identity = self.declared_pool or bool(legacy_rotation_url)
        control_plane_value = (
            legacy_rotation_url if legacy_rotation_url else config.get("base_url", "")
        )
        try:
            if self.declared_pool:
                control_plane = parse_remote_residential_control_url(control_url)
            elif self.auto_identity:
                control_plane = parse_remote_control_plane_url(control_plane_value)
            else:
                control_plane = parse_control_plane_url(control_plane_value)
        except ValueError as exc:
            raise ProxyRotationError(str(exc)) from exc
        if self.auto_identity and not control_plane.embedded_token:
            raise ProxyRotationError(
                "HX-ProxyGroup 住宅控制 URL 必须包含访问 token"
            )
        self.base_url = control_plane.origin
        self.control_path = (
            f"/ctl/{control_plane.embedded_token}" if self.declared_pool else ""
        )
        try:
            self.timeout = float(config.get("timeout_seconds", 10))
            self.max_rotate_retries = int(config.get("max_rotate_retries", 2))
            required_pool_size = int(config.get("required_pool_size", 0))
        except (TypeError, ValueError) as exc:
            raise ProxyRotationError("proxy_rotation 的数值配置无效") from exc
        if self.timeout <= 0 or self.max_rotate_retries < 0:
            raise ProxyRotationError("proxy_rotation 的数值配置无效")
        self.session_scoped = bool(config.get("session_scoped", True))
        self.post_registration_route = str(
            config.get("post_registration_route", "residential")
        ).strip().lower()
        self.check_proxy = bool(config.get("check_proxy", False))
        self.exit_ip_endpoint = str(
            config.get("exit_ip_endpoint", "https://api.ipify.org?format=json")
        )
        self.identity_endpoint = "https://ipwho.is/"
        self.verify_browser_exit_ip = bool(config.get("verify_browser_exit_ip", True))
        self.require_country_echo = bool(config.get("require_country_echo", False))
        self.country_code = str(config.get("country_code", "")).strip()
        self.required_pool_size = max(required_pool_size, 0)
        self._enforce_unique_exit_ip = bool(
            config.get("enforce_unique_exit_ip", self.check_proxy)
        )

        if self.declared_pool:
            self.listener = ""
            self.session_scoped = True
            self.post_registration_route = "residential"
            self.check_proxy = True
            self.verify_browser_exit_ip = True
            self.require_country_echo = True
            self._enforce_unique_exit_ip = True
            raw_entries = []
        elif self.auto_identity:
            # A pasted rotation URL is a complete, self-contained deployment.
            # These flags are intentionally fixed so a UI or stale config cannot
            # turn the browser flow into a direct or shared-proxy request.
            self.listener = ""
            self.session_scoped = True
            self.post_registration_route = "residential"
            self.check_proxy = True
            self.verify_browser_exit_ip = True
            self.require_country_echo = True
            self._enforce_unique_exit_ip = True
            raw_entries = [{
                "token": control_plane.embedded_token,
                "proxy": "",
                "country_code": "",
            }]
        else:
            self.listener = ""
            raw_entries = config.get("tokens", []) or []
        if not isinstance(raw_entries, list):
            raise ProxyRotationError("proxy_rotation.tokens 必须是数组")

        self.entries = []
        for index, entry in enumerate(raw_entries):
            if not isinstance(entry, dict):
                raise ProxyRotationError(
                    f"proxy_rotation.tokens[{index}] 必须是对象"
                )
            token = str(entry.get("token", "")).strip()
            proxy = str(entry.get("proxy", "")).strip()
            if not token and not proxy:
                continue
            try:
                token = validate_rotation_token(token)
                if proxy:
                    proxy = validate_proxy_endpoint(proxy)
                elif not self.auto_identity:
                    raise ValueError("代理入口不能为空")
            except ValueError as exc:
                raise ProxyRotationError(
                    f"proxy_rotation.tokens[{index}] 配置无效: {exc}"
                ) from exc
            entry_country = str(entry.get("country_code", "")).strip()
            if self.country_code and entry_country and entry_country.casefold() != self.country_code.casefold():
                raise ProxyRotationError(
                    "proxy_rotation.tokens 中的 country_code 必须与全局 country_code 一致"
                )
            self.entries.append({
                "token": token,
                "proxy": proxy,
                "country_code": entry_country,
            })

        if not self.entries and not self.declared_pool:
            raise ProxyRotationError("proxy_rotation.tokens 至少需要配置一个 {token, proxy} 渠道")
        if control_plane.embedded_token and not self.auto_identity:
            if len(self.entries) != 1 or self.entries[0]["token"] != control_plane.embedded_token:
                raise ProxyRotationError(
                    "完整 /rot/<token> URL 中的 token 必须与唯一渠道 token 一致"
                )
        if self.post_registration_route not in ("residential", "direct", "upstream"):
            raise ProxyRotationError(
                "proxy_rotation.post_registration_route 只支持 residential、direct 或 upstream"
            )

        self._session = requests.Session()
        self._session.trust_env = False
        self._lock = threading.Lock()
        self._allocation_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._declared_state = (
            _declared_lease_state(f"{self.base_url}{self.control_path}")
            if self.declared_pool
            else None
        )
        self._active_exit_ips: dict[str, tuple[str, str]] = (
            self._declared_state.active_exit_ips if self._declared_state else {}
        )
        self._leased_node_indexes: set[int] = (
            self._declared_state.leased_node_indexes if self._declared_state else set()
        )
        self._control_nodes_loaded = False
        self._next_index = 0
        self._local_data_plane = ManagedMihomo() if self.declared_pool else None

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

    def identity_profile_for_lease(self, lease):
        """Return the browser identity confirmed for an automatic lease."""
        if not lease or not str(getattr(lease, "country_code", "")).strip():
            raise ProxyRotationError("住宅会话没有可确认的国家代码")
        return {
            "country_code": str(getattr(lease, "country_code", "")).strip().upper(),
            "browser_locale": str(getattr(lease, "browser_locale", "") or "en-US"),
            "timezone": str(getattr(lease, "timezone", "") or "UTC"),
        }

    def check_connection(self):
        """Create, verify, and release one automatic session for the dashboard."""
        if not self.auto_identity:
            raise ProxyRotationError("住宅 URL 自动校验模式未启用")
        lease = self.acquire_proxy()
        try:
            identity = self.identity_profile_for_lease(lease)
            return {
                "exit_ip": lease.exit_ip,
                **identity,
            }
        finally:
            self.release(lease)

    def acquire_proxy(self, country_code=None):
        """
        为本次注册流程获取独立代理租约。
        session_scoped=true 时多个窗口可复用同一个 token 和 listener。
        开启 check_proxy 时，会在切换后通过该代理请求出口 IP 回显接口，
        确认代理真实可用才返回，避免用坏代理浪费一次注册机会。
        """
        if self.declared_pool:
            return self._acquire_declared_proxy()

        # Serialize allocation and verification so two concurrent workers cannot
        # both reserve the same observed exit IP between the check and insert.
        requested_country = "" if self.auto_identity else str(
            country_code or self.country_code
        ).strip()
        with self._allocation_lock:
            with self._lock:
                start_index = self._next_index % len(self.entries)
                self._next_index += 1

            eligible_entries = [
                entry
                for entry in self.entries
                if not requested_country
                or not entry.get("country_code")
                or entry["country_code"].casefold() == requested_country.casefold()
            ]
            if requested_country and not eligible_entries:
                raise ProxyRotationError(
                    f"没有配置支持国家 {requested_country} 的 HX-ProxyGroup 渠道"
                )

            errors = []
            for offset in range(len(self.entries)):
                entry = self.entries[(start_index + offset) % len(self.entries)]
                if entry not in eligible_entries:
                    continue
                lease = None
                try:
                    if self.session_scoped:
                        lease = self._create_session(entry, requested_country)
                    else:
                        if requested_country:
                            raise ProxyRotationError(
                                "指定国家时必须启用 session_scoped，以固定国家约束"
                            )
                        self._rotate(entry)
                        lease = ProxyLease(
                            proxy=entry["proxy"],
                            token=entry["token"],
                            country_code=entry.get("country_code", ""),
                        )
                    if self.auto_identity:
                        identity = self._verify_exit_identity(
                            lease.proxy,
                            expected_country=lease.country_code,
                        )
                        lease = replace(
                            lease,
                            exit_ip=identity["exit_ip"],
                            country_code=identity["country_code"],
                            browser_locale=identity["browser_locale"],
                            timezone=identity["timezone"],
                        )
                        self._reserve_exit_ip(lease)
                        print(
                            "[ProxyRotate] 住宅会话校验通过 - "
                            f"session_id={lease.session_id}, "
                            f"exit_ip={lease.exit_ip}, country={lease.country_code}"
                        )
                    elif self.check_proxy:
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
                    errors.append(f"渠道 {offset + 1}: {exc}")

            raise ProxyRotationError("所有住宅代理渠道切换失败: " + " | ".join(errors))

    def switch_after_registration(self, lease):
        """Apply the configured post-flow route after the browser is closed."""
        if self.post_registration_route == "residential":
            # The flow already runs on this session's residential allocation.
            # Keeping it avoids an unnecessary route mutation and preserves the
            # same country/IP contract until the session is released.
            return lease
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
        if lease.node_index:
            response = self._request(
                "POST",
                f"{self.control_path}/nodes/{lease.node_index}/route",
                json={"route_mode": route_mode},
            )
            if response.status_code != 200:
                raise ProxyRotationError(
                    f"节点切换 {route_mode} 失败: HTTP {response.status_code} "
                    f"({self._response_detail(response)})"
                )
            payload = self._json(response, f"节点切换 {route_mode}")
            if payload.get("route_mode") != route_mode:
                raise ProxyRotationError(f"节点切换 {route_mode} 响应状态不一致")
            if not self.check_proxy or not verify_exit_ip:
                return lease
            exit_ip = self._verify(lease.proxy)
            updated = replace(lease, exit_ip=exit_ip)
            self._replace_exit_ip(lease, updated)
            return updated
        response = self._request(
            "POST",
            f"/rot/{lease.token}/sessions/{lease.session_id}/route",
            json={"route_mode": route_mode},
        )
        if response.status_code != 200:
            raise ProxyRotationError(
                f"会话切换 {route_mode} 失败: HTTP {response.status_code} ({self._response_detail(response)})"
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
        """Release a process-local lease after the browser has closed."""
        if not lease:
            return
        if lease.node_index:
            self._release_exit_ip(lease)
            if self._local_data_plane is not None:
                self._local_data_plane.stop(lease.node_index)
            with self._declared_state.lock:
                self._leased_node_indexes.discard(lease.node_index)
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
        lock = self._declared_state.lock if self._declared_state else self._lock
        with lock:
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
        lock = self._declared_state.lock if self._declared_state else self._lock
        with lock:
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
        lock = self._declared_state.lock if self._declared_state else self._lock
        with lock:
            if self._active_exit_ips.get(lease.exit_ip) == owner:
                del self._active_exit_ips[lease.exit_ip]

    def _acquire_declared_proxy(self):
        """Lease and rotate one server-declared residential node."""
        with self._declared_state.lock:
            self._ensure_control_nodes()
            start_index = self._declared_state.next_index % len(self.entries)
            self._declared_state.next_index += 1
            candidates = [
                self.entries[(start_index + offset) % len(self.entries)]
                for offset in range(len(self.entries))
                if self.entries[(start_index + offset) % len(self.entries)]["index"]
                not in self._leased_node_indexes
            ]

            if not candidates:
                raise ProxyRotationError(
                    "HX-ProxyGroup 住宅节点池已全部占用；请降低并发或增加渠道会话数"
                )

            errors = []
            for node in candidates:
                node_index = node["index"]
                if node_index in self._leased_node_indexes:
                    continue
                self._leased_node_indexes.add(node_index)
                try:
                    return self._rotate_and_verify_declared_node(node)
                except ProxyRotationError as exc:
                    self._leased_node_indexes.discard(node_index)
                    errors.append(f"节点 {node_index}: {exc}")

            raise ProxyRotationError(
                "所有 HX-ProxyGroup 住宅节点均不可用: " + " | ".join(errors)
            )

    def _ensure_control_nodes(self):
        if self._control_nodes_loaded:
            return
        response = self._request("GET", f"{self.control_path}/nodes")
        if response.status_code != 200:
            if response.status_code == 404:
                raise ProxyRotationError(
                    "HX-ProxyGroup 住宅 control token 无效、已轮换或渠道未启用"
                )
            raise ProxyRotationError(
                f"读取住宅节点池失败: HTTP {response.status_code} "
                f"({self._response_detail(response)})"
            )
        payload = self._json(response, "读取住宅节点池")
        raw_nodes = payload.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ProxyRotationError("HX-ProxyGroup 没有声明可用的住宅节点")

        nodes = []
        seen_indexes = set()
        for position, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, dict):
                raise ProxyRotationError(
                    f"HX-ProxyGroup 住宅节点 {position + 1} 响应格式错误"
                )
            try:
                node_index = int(raw_node.get("index"))
            except (TypeError, ValueError) as exc:
                raise ProxyRotationError(
                    f"HX-ProxyGroup 住宅节点 {position + 1} 缺少有效 index"
                ) from exc
            if node_index < 1 or node_index in seen_indexes:
                raise ProxyRotationError("HX-ProxyGroup 住宅节点 index 无效或重复")
            seen_indexes.add(node_index)
            nodes.append({
                "index": node_index,
                "node_name": str(raw_node.get("node_name") or f"node-{node_index}"),
                "proxy_url": raw_node.get("proxy_url"),
                "endpoints": raw_node.get("endpoints") if isinstance(raw_node.get("endpoints"), list) else [],
                "residential_endpoint": (
                    raw_node.get("residential_endpoint")
                    if isinstance(raw_node.get("residential_endpoint"), dict)
                    else None
                ),
                "country_code": str(raw_node.get("country_code") or "").strip(),
                "route_mode": str(raw_node.get("route_mode") or "residential"),
                "hint": str(raw_node.get("hint") or "").strip(),
            })
        if self.required_pool_size and len(nodes) < self.required_pool_size:
            raise ProxyRotationError(
                f"住宅节点池容量不足: nodes={len(nodes)}, "
                f"required={self.required_pool_size}"
            )
        self.entries = nodes
        self._control_nodes_loaded = True

    def _rotate_and_verify_declared_node(self, node):
        last_error = None
        for attempt in range(self.max_rotate_retries + 1):
            response = self._request(
                "POST",
                f"{self.control_path}/nodes/{node['index']}/next",
            )
            if response.status_code == 429:
                last_error = "服务端限流(rotate_rate_limited)"
                time.sleep(0.5 * (attempt + 1))
                continue
            if response.status_code != 200:
                if response.status_code == 404:
                    last_error = "control token 或住宅节点已失效"
                else:
                    last_error = (
                        f"HTTP {response.status_code} "
                        f"({self._response_detail(response)})"
                    )
                time.sleep(0.5 * (attempt + 1))
                continue
            payload = self._json(response, "刷新住宅节点")
            try:
                returned_index = int(payload.get("index"))
            except (TypeError, ValueError) as exc:
                raise ProxyRotationError("刷新住宅节点响应缺少有效 index") from exc
            if returned_index != node["index"]:
                raise ProxyRotationError("刷新住宅节点响应的 index 不一致")
            updated_node = {
                **node,
                "node_name": str(payload.get("node_name") or node["node_name"]),
                "proxy_url": payload.get("proxy_url"),
                "endpoints": payload.get("endpoints") if isinstance(payload.get("endpoints"), list) else node.get("endpoints", []),
                "residential_endpoint": (
                    payload.get("residential_endpoint")
                    if isinstance(payload.get("residential_endpoint"), dict)
                    else None
                ),
                "country_code": str(payload.get("country_code") or "").strip(),
                "route_mode": str(payload.get("route_mode") or "residential"),
                "hint": str(payload.get("hint") or "").strip(),
            }
            try:
                proxy = self._proxy_from_control_node(updated_node)
                identity = self._verify_exit_identity(
                    proxy,
                    expected_country=updated_node["country_code"],
                    verify_listener_credentials=bool(
                        str(updated_node.get("proxy_url") or "").strip()
                    ) and not isinstance(updated_node.get("residential_endpoint"), dict),
                )
                lease = ProxyLease(
                    proxy=proxy,
                    token="control-node",
                    session_id=f"node-{node['index']}",
                    session_scoped=True,
                    exit_ip=identity["exit_ip"],
                    country_code=identity["country_code"],
                    browser_locale=identity["browser_locale"],
                    timezone=identity["timezone"],
                    node_index=node["index"],
                    node_name=updated_node["node_name"],
                )
                self._reserve_exit_ip(lease)
            except ProxyRotationError as exc:
                diagnostic = ""
                managed_data_plane = False
                if self._local_data_plane is not None:
                    managed_data_plane = self._local_data_plane.is_active(node["index"])
                    diagnostic = self._local_data_plane.failure_detail(node["index"])
                    self._local_data_plane.stop(node["index"])
                last_error = str(exc)
                if managed_data_plane and diagnostic:
                    last_error = f"{last_error}；本机 Mihomo: {diagnostic}"
                time.sleep(0.5 * (attempt + 1))
                continue
            for position, current in enumerate(self.entries):
                if current["index"] == node["index"]:
                    self.entries[position] = updated_node
                    break
            print(
                "[ProxyRotate] 住宅节点校验通过 - "
                f"node={lease.node_index}, exit_ip={lease.exit_ip}, "
                f"country={lease.country_code}"
            )
            return lease
        raise ProxyRotationError(last_error or "住宅节点刷新失败")

    def _proxy_from_control_node(self, node):
        residential_endpoint = node.get("residential_endpoint")
        if isinstance(residential_endpoint, dict) and self._local_data_plane is not None:
            try:
                return self._local_data_plane.start(node["index"], {
                    **residential_endpoint,
                    "transport": "tcp",
                })
            except ManagedMihomoError as exc:
                raise ProxyRotationError(str(exc)) from exc
        proxy = str(node.get("proxy_url") or "").strip()
        if proxy:
            try:
                return validate_remote_proxy_endpoint(proxy)
            except ValueError as exc:
                raise ProxyRotationError(
                    f"HX-ProxyGroup 返回的住宅节点代理入口无效: {exc}"
                ) from exc
        if self._local_data_plane is not None:
            for endpoint in node.get("endpoints") or []:
                if not isinstance(endpoint, dict):
                    continue
                protocol = str(endpoint.get("protocol") or "").strip().casefold()
                transport = str(endpoint.get("transport") or "").strip().casefold()
                if protocol not in SUPPORTED_PROTOCOLS or transport != "ws":
                    continue
                try:
                    return self._local_data_plane.start(node["index"], endpoint)
                except ManagedMihomoError as exc:
                    raise ProxyRotationError(str(exc)) from exc
        raise ProxyRotationError(
            "节点没有可用的数据端点；api-list 渠道应返回住宅端点，其他渠道必须发布 WebSocket 端点"
        )

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
