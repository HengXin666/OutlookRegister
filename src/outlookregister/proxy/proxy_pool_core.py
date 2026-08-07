from __future__ import annotations

import json
from dataclasses import replace

from outlookregister.proxy.proxy_pool_types import ProxyLease, ProxyRotationError


class _ProxyPoolCore:
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
