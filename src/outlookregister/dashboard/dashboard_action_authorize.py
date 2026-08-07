"""_authorize 动作 mixin。"""
from __future__ import annotations

import threading
import uuid

# classify_outlook_page 与 TrafficRecorder 通过 _da 运行时查找以保 patch 兼容
import outlookregister.dashboard.dashboard_actions as _da
from outlookregister.config.identity_profiles import select_identity_profile
from outlookregister.dashboard.dashboard_action_constants import (
    OAUTH_PAGE_DELAY_MS,
    SUCCESS_WINDOW_DELAY_MS,
    DashboardActionError,
)


class _AuthorizeActions:

    def _authorize(self, email: str) -> str:
        normalized_email, password = self.artifacts.credentials(email)
        config = self._config()
        oauth_config = config.get("oauth2") or {}
        if not str(oauth_config.get("client_id") or "").strip():
            raise DashboardActionError("oauth2.client_id 尚未配置")
        suffix = str(config.get("email_suffix") or "").strip()
        if not suffix or not normalized_email.casefold().endswith(suffix.casefold()):
            raise DashboardActionError("账号后缀与当前 email_suffix 配置不一致")
        local_part = normalized_email[: -len(suffix)]
        identity_profile = select_identity_profile(config.get("identity") or {})
        controller = self._controller(config)
        proxy_pool = self._proxy_pool(config, 1)
        proxy_lease = None
        if proxy_pool is not None:
            proxy_lease = self._acquire_proxy(
                proxy_pool,
                "" if getattr(proxy_pool, "auto_identity", False)
                else identity_profile["country_code"],
            )
            if getattr(proxy_pool, "auto_identity", False):
                identity_profile = proxy_pool.identity_profile_for_lease(proxy_lease)
            controller.set_proxy(proxy_lease.proxy)
        controller.results_dir = str(self.results_dir)
        controller.traffic = _da.TrafficRecorder(self.results_dir)
        controller.hx_email.set_traffic_recorder(controller.traffic)
        flow_id = f"dashboard-{uuid.uuid4().hex}"
        worker_id = str(threading.get_ident())
        controller.set_flow_context(
            flow_id,
            proxy_session_id=getattr(proxy_lease, "session_id", ""),
            proxy_exit_ip=getattr(proxy_lease, "exit_ip", ""),
            proxy_country_code=(
                getattr(proxy_lease, "country_code", "")
                or identity_profile["country_code"]
            ),
            worker_id=worker_id,
            browser_locale=identity_profile["browser_locale"],
            browser_timezone=identity_profile["timezone"],
            flow_country_code=identity_profile["country_code"],
        )
        self._set_checkpoint_context(
            flow_id,
            worker_id,
            proxy_lease,
            identity_profile,
        )
        recovery_mailbox_record = self.artifacts.recovery_mailbox(normalized_email)
        recovery_email = str(recovery_mailbox_record.get("email") or "")
        resolved_recovery_mailbox = None

        def recovery_challenge_handler(challenge_page):
            nonlocal resolved_recovery_mailbox
            if resolved_recovery_mailbox is None:
                resolved_recovery_mailbox = controller.hx_email.resolve_mailbox(
                    recovery_email,
                    mailbox_hint=recovery_mailbox_record,
                )
            return controller.confirm_recovery_email_challenge(
                challenge_page,
                controller.hx_email,
                resolved_recovery_mailbox,
                recovery_email,
            )

        page = None
        traffic_started = False
        action_succeeded = False
        self._append_checkpoint(
            normalized_email,
            password,
            "oauth_retry_started",
            "已从任务面板启动补充授权",
        )
        try:
            try:
                page = controller.get_thread_page()
            except Exception as exc:
                self._append_checkpoint(
                    normalized_email,
                    password,
                    "oauth_launch_failed",
                    str(exc),
                )
                raise DashboardActionError(f"无法启动 OAuth 浏览器: {exc}") from exc

            # Let the newly-created page settle before attaching listeners or
            # starting the first navigation/request.
            self._wait_for_page(page, OAUTH_PAGE_DELAY_MS)
            controller.traffic.start_task(
                normalized_email,
                flow_id=flow_id,
                proxy_session_id=getattr(proxy_lease, "session_id", ""),
                proxy_exit_ip=getattr(proxy_lease, "exit_ip", ""),
                proxy_country_code=(
                    getattr(proxy_lease, "country_code", "")
                    or identity_profile["country_code"]
                ),
                identity_country_code=identity_profile["country_code"],
                browser_locale=identity_profile["browser_locale"],
                browser_timezone=identity_profile["timezone"],
                worker_id=worker_id,
            )
            traffic_started = True
            controller.traffic.attach_page(page, "oauth_browser", "oauth_browser")
            refresh_token, access_token, expires_at = _da.get_access_token(
                page,
                local_part,
                password=password,
                proxy=getattr(proxy_lease, "proxy", "") or controller.get_proxy(),
                traffic_recorder=controller.traffic,
                page_delay_ms=OAUTH_PAGE_DELAY_MS,
                recovery_challenge_handler=(
                    recovery_challenge_handler if recovery_email else None
                ),
            )
            if not refresh_token:
                self._append_checkpoint(
                    normalized_email,
                    password,
                    "oauth_failed",
                    "任务面板补充授权未获取到 refresh token",
                )
                raise DashboardActionError("OAuth 授权未获取到 refresh token")
            self._append_token(
                normalized_email,
                password,
                str(refresh_token),
                str(access_token or ""),
                str(expires_at),
            )
            self._append_checkpoint(
                normalized_email,
                password,
                "oauth_success",
                "已通过任务面板补充 OAuth2 授权",
            )
            action_succeeded = True
            return "OAuth 授权已完成"
        finally:
            if action_succeeded:
                if page is not None:
                    # Keep the successful result visible long enough for a
                    # human to confirm the final redirect/consent state.
                    self._wait_for_page(page, SUCCESS_WINDOW_DELAY_MS)
            if page is not None:
                try:
                    controller.clean_up(page, "done_browser")
                except Exception as exc:
                    print(f"[Dashboard Cleanup] OAuth 页面清理失败: {exc}")
            try:
                controller.close_thread_browser()
            except Exception as exc:
                print(f"[Dashboard Cleanup] OAuth 浏览器进程清理失败: {exc}")
            try:
                controller.clean_up(type="all_browser")
            except Exception as exc:
                print(f"[Dashboard Cleanup] OAuth 资源清理失败: {exc}")
            if traffic_started:
                try:
                    controller.traffic.finish_task()
                except Exception:
                    pass
            try:
                controller.hx_email.close()
            except Exception:
                pass
            try:
                controller.clear_flow_context()
            except Exception:
                pass
            self._clear_checkpoint_context()
            if proxy_pool is not None:
                try:
                    proxy_pool.release(proxy_lease)
                except Exception as exc:
                    print(f"[Dashboard Proxy] 释放会话失败: {exc}")
