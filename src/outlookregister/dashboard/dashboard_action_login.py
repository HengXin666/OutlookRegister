"""登录设置阶段 mixin：_login_outlook_account（setup + 委托 loop）。"""
from __future__ import annotations

import time
from typing import Any

import outlookregister.dashboard.dashboard_actions as _da
from outlookregister.browser.outlook_page_state import (
    OutlookPageState,
    is_authenticated,
)
from outlookregister.dashboard.dashboard_action_constants import (
    KEEPALIVE,
    KEEPALIVE_STEP_INDEX,
    DashboardActionError,
)


class _LoginActions:

    def _login_outlook_account(
        self,
        page: Any,
        controller: Any,
        email: str,
        password: str,
        recovery_email: str,
        recovery_challenge_handler: Any,
        config: dict[str, Any],
        *,
        page_holder: list[Any] | None = None,
    ) -> OutlookPageState:
        """Run the recoverable Outlook login state machine in one browser flow.

        ``page_holder`` is an optional single-element list. When the login loop
        detects that the page died it replaces the element with a freshly
        opened page so the caller can keep using a live page afterwards.
        """

        keepalive_config = config.get("keepalive") or {}
        try:
            timeout_seconds = max(
                30,
                min(int(keepalive_config.get("login_timeout_seconds", 180)), 900),
            )
        except (TypeError, ValueError):
            timeout_seconds = 180
        try:
            manual_timeout = max(
                1,
                min(
                    int(keepalive_config.get("manual_verification_timeout_seconds", 300)),
                    3600,
                ),
            )
        except (TypeError, ValueError):
            manual_timeout = 300

        initial_resume_step = self._consume_resume_step(email, KEEPALIVE)
        fresh_start = not initial_resume_step or initial_resume_step == "login"
        if initial_resume_step in {"oauth", "hx_email"}:
            current = _da.classify_outlook_page(page)
            if is_authenticated(current):
                with self._lock:
                    state = self._states.get(email.casefold(), {}).get(KEEPALIVE)
                    if state is not None:
                        state["_resume_destination"] = initial_resume_step
                return current
            raise DashboardActionError(
                f"无法从第 {KEEPALIVE_STEP_INDEX[initial_resume_step] + 1} 步继续：当前页面还没有完成 Outlook 登录"
            )

        start_step = initial_resume_step or "login"
        start_messages = {
            "login": "正在打开 Outlook 登录页",
            "email_login": "正在继续邮箱登录步骤",
            "email_code": "正在继续密保邮箱验证步骤",
            "manual_challenge": "正在继续人工验证步骤",
        }
        self._set_progress(
            email,
            KEEPALIVE,
            start_step,
            start_messages.get(start_step, "正在继续 Outlook 登录流程"),
        )
        self._ensure_outlook_step_page(
            page,
            start_step,
            fresh=fresh_start,
        )
        started_at = time.monotonic()
        paused_at_start = self._paused_seconds(email, KEEPALIVE)
        net_errors = 0
        unknown_rounds = 0
        email_rounds = 0
        kmsi_rounds = 0
        last_state_name = ""
        ctx = {
            "timeout_seconds": timeout_seconds,
            "manual_timeout": manual_timeout,
            "started_at": started_at,
            "paused_at_start": paused_at_start,
            "net_errors": net_errors,
            "unknown_rounds": unknown_rounds,
            "email_rounds": email_rounds,
            "kmsi_rounds": kmsi_rounds,
            "last_state_name": last_state_name,
        }
        return self._login_outlook_loop(
            page, controller, email, password, recovery_email,
            recovery_challenge_handler, config, ctx,
            page_holder=page_holder,
        )
