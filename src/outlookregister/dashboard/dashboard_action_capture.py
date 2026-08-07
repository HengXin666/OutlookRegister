"""捕获/续期 mixin：浏览器证据采集、人工验证、登录确认。"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

import outlookregister.dashboard.dashboard_actions as _da
from outlookregister.browser.outlook_page_state import (
    is_authenticated,
    is_manual_verification,
)
from outlookregister.dashboard.dashboard_action_constants import (
    KEEPALIVE,
    DashboardActionError,
)


def _classify_runtime(page):
    return _da.classify_outlook_page(page)


class _CaptureActions:

    def _action_options(self, email: str, action: str) -> dict[str, Any]:
        with self._lock:
            state = self._states.get(email.casefold(), {}).get(action) or {}
            options = state.get("options") or {}
        return dict(options) if isinstance(options, dict) else {}

    def _capture_page_record(self, email: str, action: str, page: Any, reason: str) -> None:
        """Keep the exact visible page evidence when automation yields to a person."""
        record: dict[str, Any] = {
            "captured_at": self._timestamp(),
            "reason": reason,
            "url": "",
            "title": "",
            "body_text": "",
            "html": "",
            "frames": [],
        }
        try:
            record["url"] = str(page.url or "")
        except Exception:
            pass
        try:
            record["title"] = str(page.title() or "")
        except Exception:
            pass
        try:
            record["body_text"] = str(
                page.locator("body").inner_text(timeout=3000) or ""
            )
        except Exception as exc:
            record["body_text"] = f"[body_text unavailable: {exc}]"
        try:
            record["html"] = str(page.content() or "")
        except Exception as exc:
            record["html"] = f"[html unavailable: {exc}]"
        try:
            frames = page.frames
            if callable(frames):
                frames = frames()
            record["frames"] = [str(frame.url or "") for frame in frames]
        except Exception:
            record["frames"] = []

        # Keep the dashboard response bounded while retaining the complete
        # record in the task's local diagnostic file.
        record_path = self.results_dir / "keepalive_page_records.jsonl"
        try:
            record_path.parent.mkdir(parents=True, exist_ok=True)
            with self._file_lock:
                with record_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"email": email, "action": action, **record}, ensure_ascii=False) + "\n")
        except OSError:
            pass
        public_record = {
            **record,
            "body_text": record["body_text"][:50000],
            "html": record["html"][:100000],
            "local_record": str(record_path),
        }
        with self._lock:
            state = self._states.get(email.casefold(), {}).get(action)
            if state is not None:
                state["page_record"] = public_record
        self._set_state(
            email,
            action,
            "manual_verification_required",
            f"已记录人工介入页面：URL={record['url'] or '未知'}，正文 {len(record['body_text'])} 字符，HTML {len(record['html'])} 字符",
            step="manual_challenge",
            log_level="warning",
        )

    def _await_manual_verification(
        self,
        email: str,
        action: str,
        message: str,
        timeout_seconds: int,
        page: Any | None = None,
    ) -> None:
        key = (email.casefold(), action)
        event = threading.Event()
        with self._lock:
            self._verification_events[key] = event
        self._set_state(
            email,
            action,
            "manual_verification_required",
            message,
            step="manual_challenge" if action == KEEPALIVE else None,
            log_level="warning",
        )
        if page is not None:
            self._capture_page_record(email, action, page, message)
        try:
            started_at = time.monotonic()
            completed = event.wait(timeout=max(1, min(timeout_seconds, 3600)))
            elapsed = time.monotonic() - started_at
            with self._lock:
                state = self._states.get(key[0], {}).get(key[1])
                if state is not None:
                    state["_paused_seconds"] = float(
                        state.get("_paused_seconds") or 0.0
                    ) + elapsed
            if not completed:
                raise DashboardActionError("人工验证等待超时，请重新提交保活操作")
            if self._shutdown_event.is_set():
                raise DashboardActionError("服务正在关闭，人工验证等待已停止")
            self._set_state(email, action, "running", "人工验证已确认，正在继续保活流程")
        finally:
            with self._lock:
                self._verification_events.pop(key, None)

    @staticmethod
    def _verification_visible(page: Any) -> bool:
        return is_manual_verification(_classify_runtime(page))

    @staticmethod
    def _wait_for_login_success(page: Any, timeout_seconds: int = 30) -> bool:
        deadline = time.monotonic() + max(1, timeout_seconds)
        while time.monotonic() < deadline:
            if is_authenticated(_classify_runtime(page)):
                return True
            page.wait_for_timeout(500)
        return False
