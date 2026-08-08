"""登录辅助子例程 mixin：等待/调度/续期/页面修正。"""
from __future__ import annotations

import time
from typing import Any

import outlookregister.dashboard.dashboard_actions as _da
from outlookregister.browser.outlook_page_state import OutlookPageState
from outlookregister.dashboard.dashboard_action_constants import (
    KEEPALIVE,
    KEEPALIVE_STEP_INDEX,
    KEEPALIVE_STEP_ORDER,
    DashboardActionError,
)


class _RunnerSubroutines:

    @staticmethod
    def _wait_for_page(page: Any, milliseconds: int) -> None:
        """Wait without masking cleanup/debugging when a test page is minimal."""
        if page is None or milliseconds <= 0:
            return
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            try:
                wait_for_timeout(milliseconds)
            except Exception:
                # A page that closed itself should not prevent the remaining
                # success/error handling from running.
                pass

    def _set_checkpoint_context(
        self,
        flow_id: str,
        worker_id: str,
        proxy_lease: Any,
        identity_profile: dict[str, str],
    ) -> None:
        self._checkpoint_context.value = {
            "flow_id": str(flow_id or ""),
            "worker_id": str(worker_id or ""),
            "proxy_session_id": str(getattr(proxy_lease, "session_id", "") or ""),
            "proxy_exit_ip": str(getattr(proxy_lease, "exit_ip", "") or ""),
            "proxy_country_code": str(
                getattr(proxy_lease, "country_code", "")
                or identity_profile.get("country_code", "")
            ),
            "identity_country_code": str(identity_profile.get("country_code", "")),
            "browser_locale": str(identity_profile.get("browser_locale", "")),
            "browser_timezone": str(identity_profile.get("timezone", "")),
        }

    def _clear_checkpoint_context(self) -> None:
        if hasattr(self._checkpoint_context, "value"):
            delattr(self._checkpoint_context, "value")

    @staticmethod
    def _first_visible_locator(page: Any, selectors: tuple[str, ...]):
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if int(locator.count()) > 0 and locator.is_visible():
                    return locator
            except Exception:
                pass
        return None

    def _click_first_visible(self, page: Any, selectors: tuple[str, ...]) -> bool:
        locator = self._first_visible_locator(page, selectors)
        if locator is None:
            return False
        try:
            locator.click(timeout=8000)
            return True
        except Exception:
            try:
                locator.click()
                return True
            except Exception:
                return False

    def _submit_outlook_form(self, page: Any) -> bool:
        return self._click_first_visible(
            page,
            (
                "#idSIButton9",
                "#iNext",
                'button[type="submit"]',
                'input[type="submit"]',
            ),
        )

    def _consume_resume_step(self, email: str, action: str) -> str:
        with self._lock:
            state = self._states.get(email.casefold(), {}).get(action)
            if state is None:
                return ""
            value = str(state.pop("_resume_step", "") or "").strip()
            return value if value in KEEPALIVE_STEP_ORDER else ""

    def _consume_resume_destination(self, email: str) -> str:
        with self._lock:
            state = self._states.get(email.casefold(), {}).get(KEEPALIVE)
            if state is None:
                return ""
            value = str(state.pop("_resume_destination", "") or "").strip()
            return value if value in {"oauth", "hx_email"} else ""

    def _wait_if_paused(
        self,
        email: str,
        action: str,
        *,
        checkpoint_step: str | None = None,
    ) -> float:
        if self._shutdown_event.is_set():
            raise DashboardActionError("服务正在关闭，保活自动化已停止")
        # 已被用户重新开始取代：旧线程不得再触碰/覆盖新流程的状态，直接退出。
        self._raise_if_keepalive_superseded_thread()
        key = (email.casefold(), action)
        with self._lock:
            event = self._control_events.get(key)
            state = self._states.get(key[0], {}).get(key[1])
            raw_requested_step = str(state.get("control_step") or "").strip() if state else ""
            raw_current_step = str(
                checkpoint_step or (state.get("step") if state else "") or ""
            ).strip()
            requested_step = (
                self._keepalive_step(raw_requested_step)
                if action == KEEPALIVE
                else raw_requested_step
            )
            current_step = (
                self._keepalive_step(raw_current_step)
                if action == KEEPALIVE
                else raw_current_step
            )
        if event is None or event.is_set():
            return 0.0

        if requested_step and current_step:
            current_index = KEEPALIVE_STEP_INDEX.get(current_step)
            requested_index = KEEPALIVE_STEP_INDEX.get(requested_step)
            if (
                current_index is not None
                and requested_index is not None
                and current_index < requested_index
            ):
                # A pause request for a later row takes effect when the
                # worker reaches that row, rather than relabelling the row
                # that is currently running.
                return 0.0

        started_at = time.monotonic()
        pause_step = requested_step or current_step
        if state is not None and state.get("status") != "paused":
            if pause_step in KEEPALIVE_STEP_ORDER:
                with self._lock:
                    current = self._states.get(key[0], {}).get(key[1])
                    if current is not None:
                        current.setdefault("steps", {})[pause_step] = "paused"
            pause_message = (
                "自动化已暂停，浏览器保持打开，可进行人工操作"
                if state.get("_browser_open")
                else "自动化已暂停；浏览器尚未启动"
            )
            self._set_state(
                email,
                action,
                "paused",
                pause_message,
                step=pause_step or None,
                log_level="warning",
            )
        event.wait()
        elapsed = time.monotonic() - started_at
        with self._lock:
            current = self._states.get(key[0], {}).get(key[1])
            if current is not None:
                current["_paused_seconds"] = float(
                    current.get("_paused_seconds") or 0.0
                ) + elapsed
        if self._shutdown_event.is_set():
            raise DashboardActionError("服务正在关闭，保活自动化已停止")
        if current is not None and current.get("status") == "paused":
            self._set_state(
                email,
                action,
                "running",
                "人工操作已完成，自动化正在从当前页面继续",
            )
        return elapsed

    def _paused_seconds(self, email: str, action: str) -> float:
        with self._lock:
            state = self._states.get(email.casefold(), {}).get(action) or {}
            try:
                return float(state.get("_paused_seconds") or 0.0)
            except (TypeError, ValueError):
                return 0.0

    def _mark_browser_open(self, email: str, action: str) -> None:
        with self._lock:
            state = self._states.get(email.casefold(), {}).get(action)
            if state is not None:
                state["_browser_open"] = True

    @staticmethod
    def _outlook_login_url() -> str:
        return (
            "https://login.live.com/login.srf?wa=wsignin1.0&"
            "wreply=https%3A%2F%2Foutlook.live.com%2Fmail%2F0%2F"
        )

    def _ensure_outlook_step_page(
        self,
        page: Any,
        step: str,
        *,
        fresh: bool = False,
    ) -> OutlookPageState:
        """Navigate only when the visible page cannot serve the requested step."""
        current = (
            OutlookPageState("unknown", "fresh-start")
            if fresh
            else _da.classify_outlook_page(page)
        )
        login_surface = {
            "email_form",
            "login_form",
            "recovery_email_form",
            "sms_verify",
            "locked",
            "px_challenge",
            "verify_needed",
            "fido_setup",
            "error_page",
        }
        if step == "login":
            if fresh or current.name == "logged_in":
                page.goto(
                    "https://login.live.com/logout.srf",
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
                current = OutlookPageState("unknown", "navigated:logout")
            if fresh or current.name not in login_surface:
                page.goto(
                    self._outlook_login_url(),
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
                current = OutlookPageState("unknown", "navigated:login")
        elif step == "email_login":
            if current.name in {"email_form", "login_form"}:
                return current
            if current.name == "logged_in":
                page.goto(
                    "https://login.live.com/logout.srf",
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
            page.goto(
                self._outlook_login_url(),
                timeout=30000,
                wait_until="domcontentloaded",
            )
            current = OutlookPageState("unknown", "navigated:login")
        elif step == "email_code":
            # A code page is created by Microsoft after credentials are
            # submitted. Keep a visible login/code/auth page in place so the
            # next loop can fill it directly; only recover an unrelated page.
            if current.name not in {
                "email_form",
                "login_form",
                "recovery_email_form",
                "sms_verify",
            }:
                page.goto(
                    self._outlook_login_url(),
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
                current = OutlookPageState("unknown", "navigated:login")
        elif step == "manual_challenge":
            # Step 4 is intentionally opaque. The page record is the source
            # of truth for the operator, so do not navigate or click here.
            return current
        return current
