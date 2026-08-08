"""保活登录中的账号锁定页自动解锁子流程。

覆盖「警告需要重新按压的锁定页 → 按压验证页 → 恢复页面」这三段：先点击锁定页
的继续按钮，再复用注册流程验证过的按压解法，最后把恢复页面点回可识别的登录状态。
任何一段失败都返回 False，由调用方退回原有的人工介入等待。
"""

from __future__ import annotations

import time
from typing import Any

import outlookregister.dashboard.dashboard_actions as _da
from outlookregister.browser.base_controller_unlock import UNLOCK_CONTINUE_SELECTORS
from outlookregister.dashboard.dashboard_action_constants import KEEPALIVE

# 恢复页面的文案随语言变化，所以用「已经回到已知登录状态」而不是文案判断完成。
UNLOCK_RESOLVED_STATES = frozenset(
    {
        "email_form",
        "login_form",
        "kmsi",
        "logged_in",
        "sms_verify",
        "recovery_email_form",
        "fido_setup",
    }
)
# 这些状态说明挑战没过或页面还在过渡，需要继续点或直接判定失败。
UNLOCK_BLOCKED_STATES = frozenset({"locked", "px_challenge", "verify_needed"})

UNLOCK_CHALLENGE_WAIT_SECONDS = 20.0
UNLOCK_RECOVERY_ROUNDS = 8
# 一次登录里最多自动解锁几轮；用尽后仍是锁定/按压页就交给人工。
MAX_UNLOCK_ROUNDS = 2

# _auto_unlock_locked_account 的三种结果。
UNLOCK_SOLVED = "solved"
UNLOCK_RETRY = "retry"
UNLOCK_MANUAL = "manual"


class _LoginUnlockActions:
    def _handle_locked_state(
        self,
        page: Any,
        controller: Any,
        email: str,
        config: dict[str, Any],
        manual_timeout: int,
        unlock_rounds: int,
    ) -> int:
        """锁定页：先尝试自动解锁，用尽次数后退回人工等待。"""
        self._mark_keepalive_step(
            email,
            "email_code",
            "completed",
            "当前页面未出现邮箱验证码输入框",
        )
        if unlock_rounds < MAX_UNLOCK_ROUNDS:
            unlock_rounds += 1
            result = self._auto_unlock_locked_account(
                page,
                controller,
                email,
                config,
                from_locked=True,
            )
            if result == UNLOCK_SOLVED:
                return unlock_rounds
            if result == UNLOCK_RETRY and unlock_rounds < MAX_UNLOCK_ROUNDS:
                # 还有自动轮次，让状态机重新识别页面后再试一次，不打扰人。
                return unlock_rounds
        self._await_manual_verification(
            email,
            KEEPALIVE,
            "检测到账号停止登录页面。自动化已停止并保留浏览器，请完成页面上的人工操作后点击继续",
            timeout_seconds=manual_timeout,
            page=page,
            retry_on_timeout=True,
        )
        return unlock_rounds

    def _handle_manual_verification_state(
        self,
        page: Any,
        controller: Any,
        email: str,
        config: dict[str, Any],
        state: Any,
        manual_timeout: int,
        unlock_rounds: int,
    ) -> int:
        """按压页/安全验证页：按压页先自动处理，其余直接交给人工。"""
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
        if state.name == "px_challenge" and unlock_rounds < MAX_UNLOCK_ROUNDS:
            unlock_rounds += 1
            result = self._auto_unlock_locked_account(
                page,
                controller,
                email,
                config,
                from_locked=False,
            )
            if result == UNLOCK_SOLVED:
                return unlock_rounds
            if result == UNLOCK_RETRY and unlock_rounds < MAX_UNLOCK_ROUNDS:
                # 还有自动轮次，让状态机重新识别页面后再试一次，不打扰人。
                return unlock_rounds
        self._await_manual_verification(
            email,
            KEEPALIVE,
            f"检测到需要人工处理的页面（{state.evidence}）。已停止自动化并保留浏览器，请查看页面记录并完成操作后点击继续",
            timeout_seconds=manual_timeout,
            page=page,
            retry_on_timeout=True,
        )
        return unlock_rounds

    @staticmethod
    def _unlock_settings(config: dict[str, Any]) -> tuple[bool, int]:
        keepalive = config.get("keepalive") or {}
        if not isinstance(keepalive, dict):
            keepalive = {}
        enabled = keepalive.get("auto_unlock_locked_account", True)
        try:
            attempts = max(1, min(int(keepalive.get("unlock_press_attempts", 2)), 8))
        except (TypeError, ValueError):
            attempts = 2
        return bool(enabled), attempts

    def _auto_unlock_locked_account(
        self,
        page: Any,
        controller: Any,
        email: str,
        config: dict[str, Any],
        *,
        from_locked: bool,
    ) -> str:
        """自动走完锁定页到恢复页面。

        返回 ``solved``（已回到登录状态）、``retry``（试过但挑战仍在，再来一轮
        可能有用）或 ``manual``（自动路径不适用，直接交给人工）。
        """
        enabled, attempts = self._unlock_settings(config)
        if not enabled:
            return UNLOCK_MANUAL

        if from_locked:
            self._set_progress(
                email,
                KEEPALIVE,
                "unlock",
                "检测到账号锁定页，正在点击继续",
            )
            if not self._click_unlock_continue(page, controller):
                self._set_progress(
                    email,
                    KEEPALIVE,
                    "unlock",
                    "锁定页没有可点击的继续按钮，转人工处理",
                )
                return UNLOCK_MANUAL

        self._set_progress(
            email,
            KEEPALIVE,
            "unlock_loading",
            "正在等待按压验证加载",
        )
        if not self._wait_for_unlock_challenge(page, UNLOCK_CHALLENGE_WAIT_SECONDS):
            # 有时点击继续后 Microsoft 直接放行，没有按压页。
            return (
                UNLOCK_SOLVED
                if self._wait_for_unlock_recovery(page, controller, email)
                else UNLOCK_RETRY
            )

        self._set_progress(
            email,
            KEEPALIVE,
            "unlock_verification",
            f"正在自动完成按压验证（最多 {attempts} 次）",
        )
        try:
            solved = controller.solve_unlock_challenge(page, attempts)
        except Exception as exc:
            self._set_progress(
                email,
                KEEPALIVE,
                "unlock_verification",
                f"按压验证执行失败，转人工处理：{exc}",
            )
            return UNLOCK_MANUAL
        if not solved:
            self._set_progress(
                email,
                KEEPALIVE,
                "unlock_verification",
                f"按压验证 {attempts} 次后仍未通过",
            )
            return UNLOCK_RETRY

        self._set_progress(
            email,
            KEEPALIVE,
            "unlock",
            "按压验证已通过，正在恢复登录页面",
        )
        if self._wait_for_unlock_recovery(page, controller, email):
            return UNLOCK_SOLVED
        self._set_progress(
            email,
            KEEPALIVE,
            "unlock",
            "按压通过后挑战又出现，准备重试",
        )
        return UNLOCK_RETRY

    def _click_unlock_continue(self, page: Any, controller: Any) -> bool:
        """优先用控制器的拟人点击，控制器不支持时退回通用选择器。"""
        click = getattr(controller, "click_unlock_continue", None)
        if callable(click):
            try:
                if click(page):
                    return True
            except Exception:
                pass
        return self._click_first_visible(page, UNLOCK_CONTINUE_SELECTORS)

    def _wait_for_unlock_challenge(self, page: Any, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            state = _da.classify_outlook_page(page)
            if state.name == "px_challenge":
                return True
            if state.name in UNLOCK_RESOLVED_STATES:
                return False
            self._wait_for_page(page, 600)
        return False

    def _wait_for_unlock_recovery(
        self,
        page: Any,
        controller: Any,
        email: str,
    ) -> bool:
        """按压通过后的恢复页面：点到回归已知登录状态为止。"""
        for _ in range(UNLOCK_RECOVERY_ROUNDS):
            state = _da.classify_outlook_page(page)
            if state.name in UNLOCK_RESOLVED_STATES:
                self._mark_keepalive_step(
                    email,
                    "manual_challenge",
                    "completed",
                    f"账号锁定页已自动解锁（{state.evidence}）",
                )
                return True
            if state.name in UNLOCK_BLOCKED_STATES:
                return False
            # unknown/error_page：恢复页面通常只有一个主按钮，点它继续。
            self._click_unlock_continue(page, controller)
            self._wait_for_page(page, 1200)
        return False
