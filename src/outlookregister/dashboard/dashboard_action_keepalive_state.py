"""keepalive 分步状态推进辅助（共享给 _RunnerBase）。"""
from __future__ import annotations

import queue
from typing import Any

from outlookregister.dashboard.dashboard_action_constants import (
    ACTION_LOG_LIMIT,
    KEEPALIVE,
    KEEPALIVE_STEP_INDEX,
    KEEPALIVE_STEP_ORDER,
)


class _RunnerKeepaliveState:

    @staticmethod
    def _keepalive_step(step: str) -> str:
        if step in {"queued", "starting", "preparing", "proxy", "browser", "login"}:
            return "login"
        if step in {"login_email", "login_password", "login_options", "login_complete"}:
            return "email_login"
        if step in {"recovery_code", "sms_verify"}:
            return "email_code"
        if step in {"unlock", "unlock_loading", "unlock_verification", "verification"}:
            return "manual_challenge"
        if step in {"oauth_check", "oauth_authorize"}:
            return "oauth"
        if step in {"hx_email", "finishing", "complete"}:
            return "hx_email"
        return step

    @staticmethod
    def _reset_keepalive_steps_locked(
        state: dict[str, Any],
        start_step: str,
    ) -> None:
        """Make a requested resume point the only active boundary."""
        if start_step not in KEEPALIVE_STEP_INDEX:
            return
        steps = state.setdefault(
            "steps",
            {step: "pending" for step in KEEPALIVE_STEP_ORDER},
        )
        start_index = KEEPALIVE_STEP_INDEX[start_step]
        for index, step in enumerate(KEEPALIVE_STEP_ORDER):
            if index >= start_index:
                steps[step] = "pending"
        steps[start_step] = "running"

    def _set_progress(self, email: str, action: str, step: str, message: str) -> None:
        normalized_step = self._keepalive_step(step) if action == KEEPALIVE else step
        if action == KEEPALIVE:
            with self._lock:
                state = self._states.get(email.casefold(), {}).get(action)
                if state is not None:
                    steps = state.setdefault(
                        "steps",
                        {step_id: "pending" for step_id in KEEPALIVE_STEP_ORDER},
                    )
                    state["step"] = normalized_step
                    steps[normalized_step] = "running"
        self._set_state(
            email,
            action,
            "running",
            message,
            step=normalized_step,
        )
        self._wait_if_paused(email, action, checkpoint_step=normalized_step)

    def _mark_keepalive_step(
        self,
        email: str,
        step: str,
        status: str,
        message: str,
        *,
        log_level: str = "info",
    ) -> None:
        if step not in KEEPALIVE_STEP_ORDER:
            return
        with self._lock:
            state = self._states.get(email.casefold(), {}).get(KEEPALIVE)
            if state is None:
                return
            steps = state.setdefault(
                "steps",
                {step_id: "pending" for step_id in KEEPALIVE_STEP_ORDER},
            )
            steps[step] = status
        self._set_state(
            email,
            KEEPALIVE,
            state.get("status", "running"),
            message,
            step=str(state.get("step") or step),
            log_level=log_level,
        )

    def _finish_keepalive_steps(self, email: str) -> None:
        with self._lock:
            state = self._states.get(email.casefold(), {}).get(KEEPALIVE)
            if state is None:
                return
            steps = state.setdefault(
                "steps",
                {step_id: "pending" for step_id in KEEPALIVE_STEP_ORDER},
            )
            for step in KEEPALIVE_STEP_ORDER:
                if steps.get(step) != "completed":
                    steps[step] = "completed"
            self._publish_state_locked(state)

    def _update_state_locked(
        self,
        state: dict[str, Any],
        *,
        status: str,
        message: str,
        step: str | None = None,
        log_level: str = "info",
    ) -> None:
        now = self._timestamp()
        state.update({"status": status, "message": message, "updated_at": now})
        if step is not None:
            state["step"] = step
        logs = state.setdefault("logs", [])
        if not logs or logs[-1].get("message") != message:
            logs.append({"timestamp": now, "level": log_level, "message": message})
            del logs[:-ACTION_LOG_LIMIT]
        self._publish_state_locked(state)

    def _publish_state_locked(
        self,
        state: dict[str, Any],
        *,
        target: queue.Queue[dict[str, Any]] | None = None,
    ) -> None:
        payload = {
            "email": state.get("email", ""),
            "action": state.get("action", ""),
            "state": self._public_state(state),
        }
        subscribers = (target,) if target is not None else tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(payload)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(payload)
                except queue.Empty:
                    pass

    @staticmethod
    def _public_state(state: dict[str, Any]) -> dict[str, Any]:
        public = {
            key: value
            for key, value in state.items()
            if not str(key).startswith("_")
        }
        if isinstance(public.get("logs"), list):
            public["logs"] = [
                dict(entry) if isinstance(entry, dict) else entry
                for entry in public["logs"]
            ]
        if isinstance(public.get("options"), dict):
            public["options"] = dict(public["options"])
        return public
