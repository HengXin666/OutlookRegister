"""DashboardActionRunner 基础生命周期与状态机（不含具体动作实现）。"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from outlookregister.dashboard.dashboard_action_constants import (
    IMPORT_HX_EMAIL,
    KEEPALIVE,
    KEEPALIVE_STEP_INDEX,
    KEEPALIVE_STEP_LABELS,
    KEEPALIVE_STEP_ORDER,
    VALID_ACTIONS,
    AccountArtifactStore,
    DashboardActionError,
)


class _RunnerBase:
    """Runner 基类：保存状态、生命周期与状态机编排。具体动作由 mixin 提供。"""

    def __init__(
        self,
        project_root: Path | str,
        results_dir: Path | str,
        max_workers: int = 2,
    ):
        self.project_root = Path(project_root)
        self.results_dir = Path(results_dir)
        self.artifacts = AccountArtifactStore(self.results_dir)
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="dashboard-account-action",
        )
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()
        self._states: dict[str, dict[str, dict[str, Any]]] = {}
        self._verification_events: dict[tuple[str, str], threading.Event] = {}
        self._control_events: dict[tuple[str, str], threading.Event] = {}
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._shutdown_event = threading.Event()
        self._checkpoint_context = threading.local()

    def submit(
        self,
        email: str,
        action: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_email, _password = self.artifacts.credentials(email)
        if action not in VALID_ACTIONS:
            raise DashboardActionError("不支持的账号操作")
        if action == IMPORT_HX_EMAIL:
            self.artifacts.oauth_token(normalized_email)

        key = normalized_email.casefold()
        now = self._timestamp()
        with self._lock:
            account_states = self._states.setdefault(key, {})
            if any(
                state.get("status")
                in {
                    "queued",
                    "running",
                    "pausing",
                    "paused",
                    "manual_verification_required",
                }
                for state in account_states.values()
            ):
                raise DashboardActionError("该账号已有操作正在执行", status_code=409)
            state = {
                "email": normalized_email,
                "action": action,
                "status": "queued",
                "step": "queued",
                "message": "操作已排队",
                "updated_at": now,
                "logs": [
                    {
                        "timestamp": now,
                        "level": "info",
                        "message": "操作已排队",
                    }
                ],
                "steps": {step: "pending" for step in KEEPALIVE_STEP_ORDER},
            }
            if options:
                state["options"] = {
                    "auth_mode": str(options.get("auth_mode") or "password"),
                }
            account_states[action] = state
            control_event = threading.Event()
            control_event.set()
            self._control_events[(key, action)] = control_event
            self._publish_state_locked(state)
            queued_state = self._public_state(state)
        try:
            self.executor.submit(self._run, normalized_email, action)
        except Exception as exc:
            with self._lock:
                current_states = self._states.get(key, {})
                if current_states.get(action) is state:
                    current_states.pop(action, None)
                    if not current_states:
                        self._states.pop(key, None)
                self._control_events.pop((key, action), None)
            raise DashboardActionError(
                f"操作排队失败，请稍后重试: {exc}",
                status_code=503,
            ) from exc
        return queued_state

    def resume_verification(self, email: str, action: str) -> dict[str, Any]:
        return self.resume(email, action)

    def pause(
        self,
        email: str,
        action: str,
        step: str | None = None,
    ) -> dict[str, Any]:
        key = (str(email or "").strip().casefold(), str(action or "").strip())
        if key[1] != KEEPALIVE:
            raise DashboardActionError("暂停控制目前仅支持保活操作", status_code=409)
        with self._lock:
            event = self._control_events.get(key)
            state = self._states.get(key[0], {}).get(key[1])
            if (
                event is None
                or not state
                or state.get("status") not in {"queued", "running"}
            ):
                raise DashboardActionError("该账号当前没有可暂停的自动化操作", status_code=409)
            requested_step = self._keepalive_step(str(step or "").strip())
            current_step = self._keepalive_step(str(state.get("step") or ""))
            if requested_step and requested_step not in KEEPALIVE_STEP_ORDER:
                raise DashboardActionError("未知的保活步骤", status_code=422)
            requested_step = requested_step or current_step
            if requested_step not in KEEPALIVE_STEP_ORDER:
                raise DashboardActionError("当前保活步骤尚未确定", status_code=409)
            if current_step in KEEPALIVE_STEP_ORDER and requested_step != current_step:
                raise DashboardActionError("只能暂停当前正在执行的保活步骤", status_code=409)
            event.clear()
            state["control_step"] = requested_step
            self._update_state_locked(
                state,
                status="pausing",
                message="已请求暂停，正在等待自动化到达安全检查点",
                step=requested_step,
                log_level="warning",
            )
            return self._public_state(state)

    def resume(
        self,
        email: str,
        action: str,
        step: str | None = None,
    ) -> dict[str, Any]:
        key = (str(email or "").strip().casefold(), str(action or "").strip())
        with self._lock:
            verification_event = self._verification_events.get(key)
            control_event = self._control_events.get(key)
            state = self._states.get(key[0], {}).get(key[1])
            if not state:
                raise DashboardActionError("该账号当前没有可继续的操作", status_code=409)
            status = state.get("status")
            if not (
                status == "manual_verification_required"
                and verification_event is not None
            ) and not (
                status in {"pausing", "paused"}
                and control_event is not None
            ):
                raise DashboardActionError("该账号当前没有可继续的操作", status_code=409)
            requested_step = ""
            if action == KEEPALIVE:
                requested_step = self._keepalive_step(str(step or "").strip())
                current_step = self._keepalive_step(str(state.get("step") or ""))
                if requested_step and requested_step not in KEEPALIVE_STEP_ORDER:
                    raise DashboardActionError("未知的保活步骤", status_code=422)
                requested_step = requested_step or self._keepalive_step(
                    str(state.get("control_step") or current_step or "")
                )
                if requested_step and requested_step not in KEEPALIVE_STEP_ORDER:
                    requested_step = ""
                if not requested_step:
                    raise DashboardActionError("当前保活步骤尚未确定", status_code=409)
                if (
                    status in {"paused", "pausing", "manual_verification_required"}
                    and current_step in KEEPALIVE_STEP_ORDER
                    and KEEPALIVE_STEP_INDEX[requested_step]
                    > KEEPALIVE_STEP_INDEX[current_step]
                ):
                    raise DashboardActionError(
                        "不能从尚未到达的保活步骤继续",
                        status_code=422,
                    )
                self._reset_keepalive_steps_locked(state, requested_step)
                state["control_step"] = ""
                state["step"] = requested_step
            if requested_step:
                state["_resume_step"] = requested_step
            if step:
                message = (
                    f"已选择从步骤“{KEEPALIVE_STEP_LABELS[requested_step]}”继续"
                    if action == KEEPALIVE
                    else "已选择继续当前操作"
                )
                self._update_state_locked(
                    state,
                    status=status,
                    message=message,
                    step=requested_step or None,
                    log_level="info",
                )
            if status == "manual_verification_required" and verification_event is not None:
                verification_event.set()
                return self._public_state(state)
            if status in {"pausing", "paused"} and control_event is not None:
                control_event.set()
                self._update_state_locked(
                    state,
                    status="running",
                    message=(
                        f"已继续步骤“{KEEPALIVE_STEP_LABELS[requested_step]}”，"
                        "正在检查当前页面"
                        if requested_step
                        else "人工操作已完成，自动化正在从当前页面继续"
                    ),
                    step=requested_step,
                )
                return self._public_state(state)
        raise DashboardActionError("该账号当前没有可继续的操作", status_code=409)

    def snapshot(self) -> dict[str, dict[str, dict[str, Any]]]:
        with self._lock:
            return {
                email: {
                    action: self._public_state(state)
                    for action, state in action_states.items()
                }
                for email, action_states in self._states.items()
            }

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.add(subscriber)
            for action_states in self._states.values():
                for state in action_states.values():
                    self._publish_state_locked(state, target=subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def shutdown(self) -> None:
        self._shutdown_event.set()
        with self._lock:
            waiting_events = [
                *self._control_events.values(),
                *self._verification_events.values(),
            ]
        for event in waiting_events:
            event.set()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _set_state(
        self,
        email: str,
        action: str,
        status: str,
        message: str,
        *,
        step: str | None = None,
        log_level: str = "info",
    ) -> None:
        with self._lock:
            state = self._states.setdefault(email.casefold(), {}).setdefault(
                action,
                {"email": email, "action": action, "logs": []},
            )
            self._update_state_locked(
                state,
                status=status,
                message=message,
                step=step,
                log_level=log_level,
            )
