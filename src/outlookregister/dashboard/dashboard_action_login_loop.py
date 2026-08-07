"""登录循环阶段 mixin：_login_outlook_loop（状态机循环 + 最终判定）。"""
from __future__ import annotations

import time

import outlookregister.dashboard.dashboard_actions as _da
from outlookregister.browser.outlook_page_state import (
    is_authenticated,
    is_manual_verification,
)
from outlookregister.dashboard.dashboard_action_constants import (
    KEEPALIVE,
    KEEPALIVE_STEP_INDEX,
    DashboardActionError,
)
from outlookregister.dashboard.dashboard_action_login_messages import (
    LOGIN_STEP_MESSAGES,
)


def _classify_outlook_page(page):
    """运行时查找 dashboard_actions.classify_outlook_page，保持测试 patch 兼容。"""
    return _da.classify_outlook_page(page)


class _LoginActionsLoop:
    def _login_outlook_loop(self, page, controller, email, password, recovery_email, recovery_challenge_handler, config, ctx):
        timeout_seconds = ctx["timeout_seconds"]
        manual_timeout = ctx["manual_timeout"]
        started_at = ctx["started_at"]
        paused_at_start = ctx["paused_at_start"]
        net_errors = ctx["net_errors"]
        unknown_rounds = ctx["unknown_rounds"]
        email_rounds = ctx["email_rounds"]
        last_state_name = ctx["last_state_name"]
        while (
            time.monotonic()
            - started_at
            - (self._paused_seconds(email, KEEPALIVE) - paused_at_start)
            < timeout_seconds
        ):
            self._wait_if_paused(email, KEEPALIVE)
            requested_step = self._consume_resume_step(email, KEEPALIVE)
            if requested_step:
                if requested_step in {"oauth", "hx_email"}:
                    current = _classify_outlook_page(page)
                    if is_authenticated(current):
                        with self._lock:
                            state = self._states.get(email.casefold(), {}).get(KEEPALIVE)
                            if state is not None:
                                state["_resume_destination"] = requested_step
                        return current
                    raise DashboardActionError(
                        f"无法从第 {KEEPALIVE_STEP_INDEX[requested_step] + 1} 步继续：当前页面还没有完成 Outlook 登录"
                    )
                self._ensure_outlook_step_page(page, requested_step)
                last_state_name = ""
            state = _classify_outlook_page(page)
            if state.name != last_state_name:
                progress = LOGIN_STEP_MESSAGES.get(state.name)
                if progress:
                    if state.name in {"email_form", "login_form"}:
                        self._mark_keepalive_step(
                            email,
                            "login",
                            "completed",
                            "Outlook 登录页已打开",
                        )
                    elif state.name in {
                        "recovery_email_form",
                        "sms_verify",
                        "locked",
                        "px_challenge",
                        "verify_needed",
                    }:
                        self._mark_keepalive_step(
                            email,
                            "login",
                            "completed",
                            "Outlook 登录页已打开",
                        )
                        self._mark_keepalive_step(
                            email,
                            "email_login",
                            "completed",
                            "账号登录信息已提交",
                        )
                    self._set_progress(email, KEEPALIVE, progress[0], progress[1])
                last_state_name = state.name
            if is_authenticated(state):
                self._mark_keepalive_step(
                    email,
                    "email_login",
                    "completed",
                    "账号登录信息已提交",
                )
                self._set_progress(
                    email,
                    KEEPALIVE,
                    "email_code",
                    "正在确认邮箱验证码步骤",
                )
                self._mark_keepalive_step(
                    email,
                    "email_code",
                    "completed",
                    "当前登录没有要求密保邮箱验证码",
                )
                self._set_progress(
                    email,
                    KEEPALIVE,
                    "manual_challenge",
                    "正在确认人工按压步骤",
                )
                self._mark_keepalive_step(
                    email,
                    "manual_challenge",
                    "completed",
                    "当前登录没有出现人工按压验证",
                )
                self._set_state(
                    email,
                    KEEPALIVE,
                    "running",
                    f"Outlook 登录成功（{state.evidence}）",
                    step="manual_challenge",
                )
                return state

            if is_manual_verification(state):
                self._mark_keepalive_step(
                    email,
                    "login",
                    "completed",
                    "Outlook 登录页已打开",
                )
                self._mark_keepalive_step(
                    email,
                    "email_login",
                    "completed",
                    "账号登录信息已提交",
                )
                self._set_progress(
                    email,
                    KEEPALIVE,
                    "email_code",
                    "当前页面未出现邮箱验证码输入框",
                )
                self._mark_keepalive_step(
                    email,
                    "email_code",
                    "completed",
                    "当前页面未出现邮箱验证码输入框",
                )
                self._await_manual_verification(
                    email,
                    KEEPALIVE,
                    f"检测到需要人工处理的页面（{state.evidence}）。已停止自动化并保留浏览器，请查看页面记录并完成操作后点击继续",
                    timeout_seconds=manual_timeout,
                    page=page,
                )
                self._wait_for_page(page, 750)
                continue

            if state.name == "fido_setup":
                clicked = self._click_first_visible(
                    page,
                    (
                        '#idBtn_Back',
                        '#iCancel',
                        '#idBtn_Skip',
                        '#skipBtn',
                        'button:has-text("Cancel")',
                        'button:has-text("Skip")',
                        'button:has-text("Not now")',
                        'button:has-text("取消")',
                        'button:has-text("跳过")',
                    ),
                )
                if not clicked:
                    page.goto(
                        "https://outlook.live.com/mail/0/",
                        timeout=30000,
                        wait_until="domcontentloaded",
                    )
                self._wait_for_page(page, 1200)
                continue

            if state.name in {"recovery_email_form", "sms_verify"}:
                if not recovery_email or recovery_challenge_handler is None:
                    raise DashboardActionError(
                        "Outlook 要求安全代码/手机验证，但当前账号没有可用的密保邮箱取件处理器"
                    )
                if not recovery_challenge_handler(page):
                    raise DashboardActionError("密保邮箱安全代码验证未完成")
                self._mark_keepalive_step(
                    email,
                    "email_code",
                    "completed",
                    "密保邮箱验证码已提交",
                )
                self._wait_for_page(page, 1000)
                continue

            if state.name == "email_form":
                email_rounds += 1
                field = self._first_visible_locator(
                    page,
                    ('input[type="email"]', 'input[name="loginfmt"]', "#usernameEntry", "#i0116"),
                )
                if field is not None:
                    try:
                        field.fill(email, timeout=8000)
                    except Exception as exc:
                        raise DashboardActionError(f"无法填写 Outlook 登录邮箱: {exc}") from exc
                    self._submit_outlook_form(page)
                self._wait_for_page(page, 900)
                if email_rounds >= 8 and _classify_outlook_page(page).name == "email_form":
                    raise DashboardActionError("Outlook 登录停留在邮箱输入页，账号可能已被拒绝或不可用")
                continue

            if state.name == "login_form":
                field = self._first_visible_locator(
                    page,
                    ('input[type="password"]', 'input[name="passwd"]', "#passwordEntry"),
                )
                if field is not None:
                    try:
                        field.fill(password, timeout=8000)
                    except Exception as exc:
                        raise DashboardActionError(f"无法填写 Outlook 登录密码: {exc}") from exc
                    self._submit_outlook_form(page)
                self._wait_for_page(page, 900)
                continue

            if state.name == "locked":
                self._mark_keepalive_step(
                    email,
                    "email_code",
                    "completed",
                    "当前页面未出现邮箱验证码输入框",
                )
                self._await_manual_verification(
                    email,
                    KEEPALIVE,
                    "检测到账号停止登录页面。自动化已停止并保留浏览器，请完成页面上的人工操作后点击继续",
                    timeout_seconds=manual_timeout,
                    page=page,
                )
                self._wait_for_page(page, 750)
                continue

            if state.name == "error_page":
                if not self._click_first_visible(
                    page,
                    (
                        'button:has-text("Try again")',
                        'button:has-text("重试")',
                        'button:has-text("再试一次")',
                        'a:has-text("Try again")',
                    ),
                ):
                    try:
                        page.go_back(timeout=10000)
                    except Exception:
                        page.goto(
                            "https://outlook.live.com/mail/0/",
                            timeout=30000,
                            wait_until="domcontentloaded",
                        )
                self._wait_for_page(page, 1200)
                continue

            if state.name == "net_error":
                net_errors += 1
                if net_errors >= 3:
                    raise DashboardActionError("Outlook 登录网络错误次数过多")
                page.goto(
                    "https://outlook.live.com/mail/0/",
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
                self._wait_for_page(page, 1200)
                continue

            unknown_rounds += 1
            if self._submit_outlook_form(page):
                unknown_rounds = 0
            elif unknown_rounds >= 8:
                raise DashboardActionError(
                    f"无法识别 Outlook 登录页面状态（{state.evidence}）"
                )
            self._wait_for_page(page, 750)

        final_state = _classify_outlook_page(page)
        if is_authenticated(final_state):
            return final_state
        raise DashboardActionError(
            f"Outlook 登录超时，最后状态为 {final_state.name}（{final_state.evidence}）"
        )
