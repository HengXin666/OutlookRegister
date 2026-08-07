"""保活动作的准备、资源上下文和最终清理。"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any

import outlookregister.dashboard.dashboard_actions as _da
from outlookregister.config.identity_profiles import select_identity_profile
from outlookregister.dashboard.dashboard_action_constants import (
    KEEPALIVE,
    DashboardActionError,
)


@dataclass
class _KeepaliveContext:
    email: str
    password: str
    config: dict[str, Any]
    auth_mode: str
    recovery_record: dict[str, Any]
    recovery_email: str
    identity_profile: dict[str, str]
    controller: Any
    proxy_pool: Any
    proxy_lease: Any = None
    flow_id: str = ""
    worker_id: str = ""
    page: Any = None
    oauth_page: Any = None
    traffic_started: bool = False
    resolved_mailbox: Any = None
    recovery_challenge_handler: Any = None


class _KeepaliveActions:
    def _keepalive(self, email: str) -> str:
        self._set_progress(
            email,
            KEEPALIVE,
            "preparing",
            "正在读取保活配置与账号资料",
        )
        context = self._prepare_keepalive_context(email)
        self._append_checkpoint(
            context.email,
            context.password,
            "keepalive_started",
            f"任务面板启动保活登录（方式={context.auth_mode}）",
        )
        try:
            login_state, resume_destination = self._login_keepalive(context)
            return self._complete_keepalive(
                context,
                login_state,
                resume_destination,
            )
        finally:
            self._cleanup_keepalive(context)

    def _prepare_keepalive_context(self, email: str) -> _KeepaliveContext:
        normalized_email, password = self.artifacts.credentials(email)
        config = self._config()
        options = self._action_options(normalized_email, KEEPALIVE)
        auth_mode = str(options.get("auth_mode") or "password").strip().casefold()
        if auth_mode not in {"password", "recovery"}:
            raise DashboardActionError("保活登录方式必须是 password 或 recovery")

        recovery_record = self.artifacts.recovery_mailbox(normalized_email)
        recovery_email = str(recovery_record.get("email") or "")
        if auth_mode == "recovery" and not recovery_email:
            raise DashboardActionError("该账号没有已确认的密保邮箱")

        identity_profile = select_identity_profile(config.get("identity") or {})
        controller = self._controller(config)
        proxy_pool = self._proxy_pool(config, 1)
        proxy_lease = None
        try:
            if proxy_pool is not None:
                self._set_progress(
                    normalized_email,
                    KEEPALIVE,
                    "proxy",
                    "正在申请保活住宅代理",
                )
                proxy_lease = self._acquire_proxy(
                    proxy_pool,
                    ""
                    if getattr(proxy_pool, "auto_identity", False)
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
            context = _KeepaliveContext(
                email=normalized_email,
                password=password,
                config=config,
                auth_mode=auth_mode,
                recovery_record=recovery_record,
                recovery_email=recovery_email,
                identity_profile=identity_profile,
                controller=controller,
                proxy_pool=proxy_pool,
                proxy_lease=proxy_lease,
                flow_id=flow_id,
                worker_id=worker_id,
            )
            context.recovery_challenge_handler = (
                self._recovery_challenge_handler(context)
                if recovery_email
                else None
            )
            return context
        except Exception:
            if proxy_pool is not None:
                try:
                    proxy_pool.release(proxy_lease)
                except Exception:
                    pass
            try:
                controller.hx_email.close()
            except Exception:
                pass
            raise

    @staticmethod
    def _recovery_challenge_handler(context: _KeepaliveContext):
        def handle(challenge_page):
            if context.resolved_mailbox is None:
                context.resolved_mailbox = context.controller.get_flow_hx_email().resolve_mailbox(
                    context.recovery_email,
                    mailbox_hint=context.recovery_record,
                )
            return context.controller.confirm_recovery_email_challenge(
                challenge_page,
                context.controller.get_flow_hx_email(),
                context.resolved_mailbox,
                context.recovery_email,
            )

        return handle

    def _cleanup_keepalive(self, context: _KeepaliveContext) -> None:
        if context.oauth_page is not None:
            try:
                context.controller.clean_up(context.oauth_page, "done_browser")
            except Exception:
                pass
        if context.page is not None:
            try:
                context.controller.clean_up(context.page, "done_browser")
            except Exception:
                pass
        try:
            context.controller.close_thread_browser()
        except Exception:
            pass
        try:
            context.controller.clean_up(type="all_browser")
        except Exception:
            pass
        if context.traffic_started:
            try:
                context.controller.traffic.finish_task()
            except Exception:
                pass
        try:
            context.controller.hx_email.close()
        except Exception:
            pass
        try:
            context.controller.clear_flow_context()
        except Exception:
            pass
        self._clear_checkpoint_context()
        if context.proxy_pool is not None:
            try:
                context.proxy_pool.release(context.proxy_lease)
            except Exception as exc:
                print(f"[Dashboard Proxy] 释放会话失败: {exc}")
