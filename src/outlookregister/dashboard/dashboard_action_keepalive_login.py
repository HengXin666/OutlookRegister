"""保活动作的 Outlook 登录阶段。"""

from __future__ import annotations

from typing import Any

from outlookregister.dashboard.dashboard_action_constants import KEEPALIVE
from outlookregister.dashboard.dashboard_action_keepalive import _KeepaliveContext


class _KeepaliveLoginActions:
    def _login_keepalive(
        self,
        context: _KeepaliveContext,
    ) -> tuple[Any, str]:
        self._set_progress(
            context.email,
            KEEPALIVE,
            "browser",
            "正在启动保活浏览器",
        )
        context.page = context.controller.get_thread_page()
        self._mark_browser_open(context.email, KEEPALIVE)
        context.controller.traffic.start_task(
            context.email,
            flow_id=context.flow_id,
            proxy_session_id=getattr(context.proxy_lease, "session_id", ""),
            proxy_exit_ip=getattr(context.proxy_lease, "exit_ip", ""),
            proxy_country_code=(
                getattr(context.proxy_lease, "country_code", "")
                or context.identity_profile["country_code"]
            ),
            identity_country_code=context.identity_profile["country_code"],
            browser_locale=context.identity_profile["browser_locale"],
            browser_timezone=context.identity_profile["timezone"],
            worker_id=context.worker_id,
        )
        context.traffic_started = True
        context.controller.traffic.attach_page(
            context.page,
            "keepalive_login",
            "keepalive_browser",
        )
        login_state = self._login_outlook_account(
            context.page,
            context.controller,
            context.email,
            context.password,
            context.recovery_email,
            context.recovery_challenge_handler,
            context.config,
        )
        resume_destination = self._consume_resume_destination(context.email)
        requested_after_login = self._consume_resume_step(
            context.email,
            KEEPALIVE,
        )
        if requested_after_login == "hx_email":
            resume_destination = "hx_email"
        self._append_checkpoint(
            context.email,
            context.password,
            "keepalive_logged_in",
            f"保活登录成功（方式={context.auth_mode}；证据={login_state.evidence}）",
        )
        self._set_progress(
            context.email,
            KEEPALIVE,
            "oauth_check",
            "登录完成，正在检查 OAuth/Graph 授权",
        )
        requested_before_oauth = self._consume_resume_step(
            context.email,
            KEEPALIVE,
        )
        if requested_before_oauth == "hx_email":
            resume_destination = "hx_email"
        return login_state, resume_destination
