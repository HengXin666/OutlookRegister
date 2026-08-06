"""Background account recovery actions used by the local dashboard."""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from controllers.patchright_controller import PatchrightController
from controllers.playwright_controller import PlaywrightController
from config_store import ConfigError, ConfigStore, validate_config
from get_token import get_access_token, refresh_oauth_token
from hx_email_client import HXEmailClient
from identity_profiles import select_identity_profile
from outlook_page_state import (
    OutlookPageState,
    classify_outlook_page,
    is_authenticated,
    is_manual_verification,
)
from proxy_rotation import ProxyRotationError, RotatingProxyPool
from traffic_tracker import TrafficRecorder


AUTHORIZE = "authorize"
IMPORT_HX_EMAIL = "import_hx_email"
KEEPALIVE = "keepalive"
VALID_ACTIONS = {AUTHORIZE, IMPORT_HX_EMAIL, KEEPALIVE}

# The dashboard retry flow runs against pages that may still be rendering
# Microsoft security controls. Keep these waits local to that flow so normal
# registration is not slowed down as a side effect.
OAUTH_PAGE_DELAY_MS = 1500
HX_EMAIL_HANDOFF_DELAY_SECONDS = 1.5
SUCCESS_WINDOW_DELAY_MS = 3000
ACTION_LOG_LIMIT = 100
KEEPALIVE_STEP_ORDER = (
    "login",
    "email_login",
    "email_code",
    "manual_challenge",
    "oauth",
    "hx_email",
)
KEEPALIVE_STEP_LABELS = {
    "login": "登录",
    "email_login": "邮箱登录",
    "email_code": "获取邮箱验证码并提交完成登录",
    "manual_challenge": "账号停止登录，等待人工按压测试",
    "oauth": "获取授权",
    "hx_email": "加入 HX-Email",
}
KEEPALIVE_STEP_INDEX = {
    step: index for index, step in enumerate(KEEPALIVE_STEP_ORDER)
}


class DashboardActionError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class ManualVerificationRequired(DashboardActionError):
    """The visible browser needs an operator to complete a site challenge."""

    def __init__(self, message: str):
        super().__init__(message, status_code=409)


class AccountArtifactStore:
    """Resolve private account artifacts without exposing them through the API."""

    def __init__(self, results_dir: Path | str):
        self.results_dir = Path(results_dir)

    @staticmethod
    def _email_key(email: str) -> str:
        return str(email or "").strip().casefold()

    def credentials(self, email: str) -> tuple[str, str]:
        target = self._email_key(email)
        if not target:
            raise DashboardActionError("账号地址不能为空")
        path = self.results_dir / "account_checkpoints.jsonl"
        for record in reversed(self._jsonl(path)):
            record_email = str(
                record.get("outlook_email") or record.get("email") or ""
            ).strip()
            password = str(record.get("password") or "")
            if self._email_key(record_email) == target and password:
                return record_email, password
        raise DashboardActionError("未找到该账号的本地凭据", status_code=404)

    def oauth_token(self, email: str) -> dict[str, str]:
        target = self._email_key(email)
        path = self.results_dir / "outlook_token.txt"
        if path.exists():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise DashboardActionError("无法读取本地 OAuth token 文件") from exc
            for line in reversed(lines):
                fields = line.split("---", 4)
                if len(fields) != 5 or self._email_key(fields[0]) != target:
                    continue
                return {
                    "email": fields[0],
                    "password": fields[1],
                    "refresh_token": fields[2],
                    "access_token": fields[3],
                    "expires_at": fields[4],
                }
        raise DashboardActionError("该账号尚无可用的 OAuth token", status_code=409)

    def recovery_email(self, email: str) -> str:
        return self.recovery_mailbox(email).get("email", "")

    def recovery_mailbox(self, email: str) -> dict[str, Any]:
        target = self._email_key(email)
        path = self.results_dir / "recovery_email_status.jsonl"
        for record in reversed(self._jsonl(path)):
            record_email = str(
                record.get("outlook_email") or record.get("email") or ""
            ).strip()
            recovery_email = str(record.get("recovery_email") or "").strip()
            if (
                self._email_key(record_email) == target
                and record.get("bound") is True
                and recovery_email
            ):
                return {
                    "email": recovery_email,
                    "usable_email_id": record.get("usable_email_id"),
                    "mode": str(record.get("mailbox_mode") or ""),
                }
        return {}

    @staticmethod
    def _jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                records.append(value)
        return records


class DashboardActionRunner:
    """Run long browser/API account operations outside request event loops."""

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

    def _run(self, email: str, action: str) -> None:
        running_message = (
            "正在执行 OAuth 授权"
            if action == AUTHORIZE
            else "正在保活登录" if action == KEEPALIVE else "正在加入 HX-Email"
        )
        self._set_state(email, action, "running", running_message, step="starting")
        try:
            self._wait_if_paused(email, action)
            message = self._execute_action(email, action)
        except ManualVerificationRequired as exc:
            self._set_state(
                email,
                action,
                "manual_verification_required",
                str(exc)[:500],
                log_level="warning",
            )
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            self._set_state(
                email,
                action,
                "failed",
                self._public_error(email, detail)[:500],
                step="failed",
                log_level="error",
            )
        else:
            # The final phase can finish between two browser/API calls. Check
            # once more before publishing success so a late pause request
            # still keeps the browser and task state available to the user.
            self._wait_if_paused(email, action)
            if action == KEEPALIVE:
                self._finish_keepalive_steps(email)
            self._set_state(email, action, "succeeded", message, step="complete")
        finally:
            with self._lock:
                self._control_events.pop((email.casefold(), action), None)

    def _public_error(self, email: str, detail: str) -> str:
        secrets: list[str] = []
        try:
            _normalized_email, password = self.artifacts.credentials(email)
            secrets.append(password)
        except DashboardActionError:
            pass
        try:
            token = self.artifacts.oauth_token(email)
            secrets.extend(
                str(token.get(key) or "")
                for key in ("password", "refresh_token", "access_token")
            )
        except DashboardActionError:
            pass
        try:
            secrets.extend(self._config_secret_values(self._config()))
        except Exception:
            pass
        for secret in sorted(set(filter(None, secrets)), key=len, reverse=True):
            detail = detail.replace(secret, "[redacted]")
        detail = re.sub(
            r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@",
            r"\1[redacted]@",
            detail,
        )
        detail = re.sub(
            r"(?i)(/(?:ctl|rot)/)[^/?#\s]+",
            r"\1[redacted]",
            detail,
        )
        detail = re.sub(
            r"(?i)([?&](?:token|api[_-]?key|password|secret)=)[^&#\s]+",
            r"\1[redacted]",
            detail,
        )
        return re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [redacted]", detail)

    @classmethod
    def _config_secret_values(cls, value: Any, key: str = "") -> list[str]:
        secret_keys = {
            "api_key",
            "password",
            "proxy",
            "proxy_url",
            "control_url",
            "rotation_url",
            "refresh_token",
            "token",
            "tokens",
        }
        normalized_key = key.casefold()
        if normalized_key in secret_keys or normalized_key.endswith("_secret"):
            if isinstance(value, list):
                return [str(item) for item in value if str(item)]
            return [str(value)] if value not in (None, "") else []
        if isinstance(value, dict):
            return [
                secret
                for child_key, child_value in value.items()
                for secret in cls._config_secret_values(child_value, str(child_key))
            ]
        if isinstance(value, list):
            return [
                secret
                for child_value in value
                for secret in cls._config_secret_values(child_value, key)
            ]
        return []

    def _execute_action(self, email: str, action: str) -> str:
        if action == AUTHORIZE:
            return self._authorize(email)
        if action == IMPORT_HX_EMAIL:
            return self._import_hx_email(email)
        if action == KEEPALIVE:
            return self._keepalive(email)
        raise DashboardActionError("不支持的账号操作")

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
        controller.traffic = TrafficRecorder(self.results_dir)
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
            refresh_token, access_token, expires_at = get_access_token(
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
            else classify_outlook_page(page)
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

    def _login_outlook_account(
        self,
        page: Any,
        controller: Any,
        email: str,
        password: str,
        recovery_email: str,
        recovery_challenge_handler: Any,
        config: dict[str, Any],
    ) -> OutlookPageState:
        """Run the recoverable Outlook login state machine in one browser flow."""

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
            current = classify_outlook_page(page)
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
        last_state_name = ""

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
                    current = classify_outlook_page(page)
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
            state = classify_outlook_page(page)
            if state.name != last_state_name:
                step_messages = {
                    "email_form": ("email_login", "正在填写 Outlook 登录邮箱"),
                    "login_form": ("email_login", "正在填写 Outlook 登录密码"),
                    "recovery_email_form": ("email_code", "正在填写密保邮箱"),
                    "locked": ("unlock", "检测到账号锁定页，准备点击继续"),
                    "px_challenge": ("manual_challenge", "检测到按压验证，等待人工处理"),
                    "verify_needed": ("manual_challenge", "检测到需要人工处理的安全验证"),
                    "sms_verify": ("email_code", "正在处理密保邮箱安全代码"),
                    "fido_setup": ("email_login", "正在处理登录选项"),
                    "net_error": ("login", "Outlook 登录页出现网络错误，准备重试"),
                    "error_page": ("login", "Outlook 登录页返回错误，准备重试"),
                    "unknown": ("login", "正在识别 Outlook 登录页面"),
                }
                progress = step_messages.get(state.name)
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
                if email_rounds >= 8 and classify_outlook_page(page).name == "email_form":
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

        final_state = classify_outlook_page(page)
        if is_authenticated(final_state):
            return final_state
        raise DashboardActionError(
            f"Outlook 登录超时，最后状态为 {final_state.name}（{final_state.evidence}）"
        )

    def _keepalive(self, email: str) -> str:
        normalized_email, password = self.artifacts.credentials(email)
        self._set_progress(
            normalized_email,
            KEEPALIVE,
            "preparing",
            "正在读取保活配置与账号资料",
        )
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
        if proxy_pool is not None:
            self._set_progress(
                normalized_email,
                KEEPALIVE,
                "proxy",
                "正在申请保活住宅代理",
            )
            proxy_lease = self._acquire_proxy(
                proxy_pool,
                "" if getattr(proxy_pool, "auto_identity", False)
                else identity_profile["country_code"],
            )
            if getattr(proxy_pool, "auto_identity", False):
                identity_profile = proxy_pool.identity_profile_for_lease(proxy_lease)
            controller.set_proxy(proxy_lease.proxy)
        controller.results_dir = str(self.results_dir)
        controller.traffic = TrafficRecorder(self.results_dir)
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
        resolved_mailbox = None

        def recovery_challenge_handler(challenge_page):
            nonlocal resolved_mailbox
            if resolved_mailbox is None:
                resolved_mailbox = controller.get_flow_hx_email().resolve_mailbox(
                    recovery_email,
                    mailbox_hint=recovery_record,
                )
            return controller.confirm_recovery_email_challenge(
                challenge_page,
                controller.get_flow_hx_email(),
                resolved_mailbox,
                recovery_email,
            )

        page = None
        oauth_page = None
        traffic_started = False
        self._append_checkpoint(
            normalized_email,
            password,
            "keepalive_started",
            f"任务面板启动保活登录（方式={auth_mode}）",
        )
        try:
            self._set_progress(
                normalized_email,
                KEEPALIVE,
                "browser",
                "正在启动保活浏览器",
            )
            page = controller.get_thread_page()
            self._mark_browser_open(normalized_email, KEEPALIVE)
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
            controller.traffic.attach_page(page, "keepalive_login", "keepalive_browser")
            login_state = self._login_outlook_account(
                page,
                controller,
                normalized_email,
                password,
                recovery_email,
                recovery_challenge_handler if recovery_email else None,
                config,
            )
            resume_destination = self._consume_resume_destination(normalized_email)
            requested_after_login = self._consume_resume_step(
                normalized_email,
                KEEPALIVE,
            )
            if requested_after_login == "hx_email":
                resume_destination = "hx_email"
            self._append_checkpoint(
                normalized_email,
                password,
                "keepalive_logged_in",
                f"保活登录成功（方式={auth_mode}；证据={login_state.evidence}）",
            )
            self._set_progress(
                normalized_email,
                KEEPALIVE,
                "oauth_check",
                "登录完成，正在检查 OAuth/Graph 授权",
            )
            requested_before_oauth = self._consume_resume_step(
                normalized_email,
                KEEPALIVE,
            )
            if requested_before_oauth == "hx_email":
                resume_destination = "hx_email"

            oauth_config = config.get("oauth2") or {}
            oauth_client_id = str(oauth_config.get("client_id") or "").strip()
            keepalive_config = config.get("keepalive") or {}
            token = None
            completion_notes: list[str] = []
            token_probe_error = ""
            try:
                token = self.artifacts.oauth_token(normalized_email)
            except DashboardActionError:
                token = None

            skip_oauth = resume_destination == "hx_email"
            if skip_oauth:
                self._set_progress(
                    normalized_email,
                    KEEPALIVE,
                    "oauth_check",
                    "已选择从第 6 步继续，正在检查已有 OAuth 授权",
                )
                if not token or not str(token.get("refresh_token") or "").strip():
                    raise DashboardActionError(
                        "无法直接从第 6 步继续：该账号没有可用的 OAuth refresh token"
                    )

            verify_existing_token = bool(
                keepalive_config.get("verify_existing_oauth_token", True)
                and not skip_oauth
            )
            if token and not str(token.get("refresh_token") or "").strip():
                token = None
            if token and verify_existing_token and oauth_client_id:
                self._set_progress(
                    normalized_email,
                    KEEPALIVE,
                    "oauth_check",
                    "正在验证已有 OAuth refresh token",
                )
                probe = refresh_oauth_token(
                    token["refresh_token"],
                    client_id=oauth_client_id,
                    tenant=oauth_config.get("tenant"),
                    scopes=oauth_config.get("Scopes"),
                    proxy=getattr(proxy_lease, "proxy", "") or controller.get_proxy(),
                    traffic_recorder=controller.traffic,
                    email=normalized_email,
                )
                if probe.get("ok"):
                    token = {
                        **token,
                        "refresh_token": str(
                            probe.get("refresh_token") or token["refresh_token"]
                        ),
                        "access_token": str(probe.get("access_token") or ""),
                        "expires_at": str(probe.get("expires_at") or ""),
                    }
                    self._append_token(
                        normalized_email,
                        password,
                        token["refresh_token"],
                        token["access_token"],
                        token["expires_at"],
                    )
                    self._append_checkpoint(
                        normalized_email,
                        password,
                        "oauth_success",
                        "已有 refresh token 经当前住宅 flow 探针验证可用",
                    )
                    self._mark_keepalive_step(
                        normalized_email,
                        "oauth",
                        "completed",
                        "已有 OAuth refresh token 验证可用",
                    )
                else:
                    token_probe_error = str(probe.get("error") or "probe_failed")
                    token = None
                    self._append_checkpoint(
                        normalized_email,
                        password,
                        "oauth_token_invalid",
                        f"已有 refresh token 探针未通过（{token_probe_error}）",
                    )
            elif token and not oauth_client_id and verify_existing_token:
                token_probe_error = "missing_client_id"
                token = None
                self._append_checkpoint(
                    normalized_email,
                    password,
                    "oauth_token_invalid",
                    "已有 refresh token 无法探针：oauth2.client_id 未配置",
                )
            elif token:
                self._append_checkpoint(
                    normalized_email,
                    password,
                    "oauth_token_present",
                    "本地已有 refresh token；按配置跳过可用性探针",
                )

            if token and not bool(oauth_config.get("enable_oauth2", False)):
                self._mark_keepalive_step(
                    normalized_email,
                    "oauth",
                    "completed",
                    "已有 OAuth refresh token，按配置跳过重新授权",
                )

            if bool(oauth_config.get("enable_oauth2", False)) and not token:
                if not oauth_client_id:
                    completion_notes.append("OAuth 未配置 client_id，已跳过补充授权")
                    self._mark_keepalive_step(
                        normalized_email,
                        "oauth",
                        "completed",
                        "OAuth 未配置 client_id，已跳过授权",
                    )
                    self._append_checkpoint(
                        normalized_email,
                        password,
                        "oauth_skipped",
                        "保活登录成功，但 oauth2.client_id 未配置，跳过补充授权",
                    )
                else:
                    self._set_progress(
                        normalized_email,
                        KEEPALIVE,
                        "oauth_authorize",
                        "正在补充 OAuth/Graph 授权",
                    )
                    local_part = normalized_email[: -len(str(config.get("email_suffix") or ""))]
                    oauth_error = token_probe_error
                    oauth_candidates = [page]
                    for candidate_index, candidate_page in enumerate(oauth_candidates):
                        try:
                            controller.traffic.set_page_stage(
                                candidate_page,
                                "oauth_browser",
                                "oauth_browser_session",
                            )
                            refresh_token, access_token, expires_at = get_access_token(
                                candidate_page,
                                local_part,
                                password=password,
                                proxy=getattr(proxy_lease, "proxy", "") or controller.get_proxy(),
                                traffic_recorder=controller.traffic,
                                recovery_challenge_handler=(
                                    recovery_challenge_handler if recovery_email else None
                                ),
                                page_delay_ms=OAUTH_PAGE_DELAY_MS,
                            )
                        except Exception as exc:
                            refresh_token = access_token = expires_at = False
                            oauth_error = str(exc)
                        if refresh_token:
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
                                "保活登录后补充 OAuth2/Graph 授权成功（浏览器会话 + token endpoint）",
                            )
                            token = {
                                "refresh_token": str(refresh_token),
                                "access_token": str(access_token or ""),
                                "expires_at": str(expires_at or ""),
                            }
                            self._mark_keepalive_step(
                                normalized_email,
                                "oauth",
                                "completed",
                                "OAuth/Graph 授权已完成",
                            )
                            break
                        if candidate_index == 0:
                            oauth_page = controller.get_oauth_page(
                                page,
                                proxy=getattr(proxy_lease, "proxy", "") or controller.get_proxy(),
                            )
                            if oauth_page:
                                oauth_candidates.append(oauth_page)
                                controller.traffic.set_page_stage(
                                    oauth_page,
                                    "oauth_browser",
                                    "oauth_browser_context_fallback",
                                )
                    if not token:
                        self._append_checkpoint(
                            normalized_email,
                            password,
                            "oauth_failed",
                            f"保活登录后未获取到可用 refresh token（{oauth_error or 'unknown'}）",
                        )
                        raise DashboardActionError(
                            "保活登录成功，但补充 OAuth/Graph 授权未获取到 refresh token"
                        )

            if skip_oauth:
                self._mark_keepalive_step(
                    normalized_email,
                    "oauth",
                    "completed",
                    "按第 6 步继续，沿用已有 OAuth refresh token",
                )

            if token and bool(keepalive_config.get("auto_import_hx_email", True)):
                if not oauth_client_id:
                    completion_notes.append("HX-Email 未配置 client_id，已跳过导入")
                    self._mark_keepalive_step(
                        normalized_email,
                        "hx_email",
                        "completed",
                        "HX-Email 未配置 client_id，已跳过导入",
                    )
                    self._append_checkpoint(
                        normalized_email,
                        password,
                        "hx_email_import_skipped",
                        "保活登录成功，但 oauth2.client_id 未配置，跳过 HX-Email 导入",
                    )
                else:
                    self._set_progress(
                        normalized_email,
                        KEEPALIVE,
                        "hx_email",
                        "正在将账号加入 HX-Email",
                    )
                    hx_config = dict((config.get("recovery_email") or {}).get("hx_email") or {})
                    imported = controller.get_flow_hx_email().import_outlook_account(
                        email=normalized_email,
                        password=password,
                        recovery_email=recovery_email,
                        client_id=oauth_client_id,
                        refresh_token=token["refresh_token"],
                        proxy_url=(
                            getattr(proxy_lease, "proxy", "")
                            or str(hx_config.get("proxy_url") or "").strip()
                        ),
                    )
                    self._append_checkpoint(
                        normalized_email,
                        password,
                        "hx_email_imported",
                        f'保活后加入 HX-Email account_id={imported["account_id"]}',
                    )
                    self._mark_keepalive_step(
                        normalized_email,
                        "hx_email",
                        "completed",
                        "账号已加入 HX-Email",
                    )
            elif not token and bool(keepalive_config.get("auto_import_hx_email", True)):
                completion_notes.append("没有可用 OAuth/Graph refresh token，已跳过 HX-Email")
                self._mark_keepalive_step(
                    normalized_email,
                    "hx_email",
                    "completed",
                    "没有可用 OAuth refresh token，已跳过 HX-Email 导入",
                )
                self._append_checkpoint(
                    normalized_email,
                    password,
                    "hx_email_import_skipped",
                    "保活登录完成，但没有可用 refresh token，跳过 HX-Email 导入",
                )
            elif not bool(keepalive_config.get("auto_import_hx_email", True)):
                completion_notes.append("按配置跳过 HX-Email 导入")
                self._mark_keepalive_step(
                    normalized_email,
                    "hx_email",
                    "completed",
                    "按配置跳过 HX-Email 导入",
                )
                self._append_checkpoint(
                    normalized_email,
                    password,
                    "hx_email_import_skipped",
                    "keepalive.auto_import_hx_email=false",
                )
            self._set_progress(
                normalized_email,
                KEEPALIVE,
                "finishing",
                "保活步骤已完成，正在整理结果",
            )
            return "保活登录完成" + ("；" + "；".join(completion_notes) if completion_notes else "")
        finally:
            if oauth_page is not None:
                try:
                    controller.clean_up(oauth_page, "done_browser")
                except Exception:
                    pass
            if page is not None:
                try:
                    controller.clean_up(page, "done_browser")
                except Exception:
                    pass
            try:
                controller.close_thread_browser()
            except Exception:
                pass
            try:
                controller.clean_up(type="all_browser")
            except Exception:
                pass
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
        return is_manual_verification(classify_outlook_page(page))

    @staticmethod
    def _wait_for_login_success(page: Any, timeout_seconds: int = 30) -> bool:
        deadline = time.monotonic() + max(1, timeout_seconds)
        while time.monotonic() < deadline:
            if is_authenticated(classify_outlook_page(page)):
                return True
            page.wait_for_timeout(500)
        return False

    def _import_hx_email(self, email: str) -> str:
        normalized_email, password = self.artifacts.credentials(email)
        token = self.artifacts.oauth_token(normalized_email)
        config = self._config()
        oauth_config = config.get("oauth2") or {}
        client_id = str(oauth_config.get("client_id") or "").strip()
        if not client_id:
            raise DashboardActionError("oauth2.client_id 尚未配置")
        recovery_config = config.get("recovery_email") or {}
        hx_config = dict(recovery_config.get("hx_email") or {})
        identity_profile = select_identity_profile(config.get("identity") or {})
        proxy_pool = self._proxy_pool(config, 1)
        proxy_lease = None
        if proxy_pool is not None:
            proxy_lease = self._acquire_proxy(
                proxy_pool,
                identity_profile["country_code"],
            )
        if config.get("isolate_hx_email_group", config.get("strict_isolation", True)):
            base_group = str(
                hx_config.get("account_group", "OutlookRegister 自动注册")
            ).strip()
            hx_config["account_group"] = (
                f"{base_group} [dashboard-{uuid.uuid4().hex[:8]}]"
            )

        recorder = TrafficRecorder(self.results_dir)
        client = HXEmailClient(hx_config)
        client.set_traffic_recorder(recorder)
        flow_id = f"dashboard-{uuid.uuid4().hex}"
        recorder.start_task(
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
            worker_id=str(threading.get_ident()),
        )
        self._set_checkpoint_context(
            flow_id,
            str(threading.get_ident()),
            proxy_lease,
            identity_profile,
        )
        self._append_checkpoint(
            normalized_email,
            password,
            "hx_email_import_started",
            "已从任务面板启动 HX-Email 导入",
        )
        try:
            # Keep the handoff request separate from the just-finished OAuth
            # flow so HX-Email does not receive an immediate burst of work.
            time.sleep(HX_EMAIL_HANDOFF_DELAY_SECONDS)
            imported = client.import_outlook_account(
                email=normalized_email,
                password=password,
                recovery_email=self.artifacts.recovery_email(normalized_email),
                client_id=client_id,
                refresh_token=token["refresh_token"],
                proxy_url=(
                    getattr(proxy_lease, "proxy", "")
                    or str(hx_config.get("proxy_url") or "").strip()
                ),
            )
        except Exception as exc:
            self._append_checkpoint(
                normalized_email,
                password,
                "hx_email_import_failed",
                str(exc),
            )
            raise
        else:
            self._append_checkpoint(
                normalized_email,
                password,
                "hx_email_imported",
                (
                    f'account_id={imported["account_id"]}, '
                    f'group_id={imported["group_id"]}; 来源=任务面板'
                ),
            )
            return "已加入 HX-Email"
        finally:
            recorder.finish_task()
            client.close()
            self._clear_checkpoint_context()
            if proxy_pool is not None:
                try:
                    proxy_pool.release(proxy_lease)
                except Exception as exc:
                    print(f"[Dashboard Proxy] 释放会话失败: {exc}")

    def _proxy_pool(self, config: dict[str, Any], required_pool_size: int):
        if not config.get("proxy_rotation"):
            # Keep lightweight adapters usable, but never let a configured
            # dynamic/strict deployment silently fall back to direct traffic.
            if config.get("strict_isolation") is True or "identity" in config:
                errors = validate_config(config, for_run=True)
                if errors:
                    raise DashboardActionError(
                        "配置不允许执行该操作: " + "；".join(errors)
                    )
            return None
        errors = validate_config(config, for_run=True)
        if errors:
            raise DashboardActionError("配置不允许执行该操作: " + "；".join(errors))
        proxy_config = dict(config.get("proxy_rotation") or {})
        proxy_config["required_pool_size"] = max(1, int(required_pool_size))
        try:
            return RotatingProxyPool(proxy_config)
        except ProxyRotationError as exc:
            raise DashboardActionError(f"HX-ProxyGroup 配置无效: {exc}") from exc

    @staticmethod
    def _acquire_proxy(proxy_pool, country_code=""):
        country_code = str(country_code or "").strip()
        try:
            return proxy_pool.acquire_proxy(country_code) if country_code else proxy_pool.acquire_proxy()
        except ProxyRotationError as exc:
            raise DashboardActionError(f"无法获取动态住宅 IP 会话: {exc}") from exc

    def _config(self) -> dict[str, Any]:
        try:
            return ConfigStore(self.project_root / "config.json").read()
        except ConfigError as exc:
            raise DashboardActionError(str(exc)) from exc

    @staticmethod
    def _controller(config: dict[str, Any]):
        browser = str(config.get("choose_browser") or "").strip().casefold()
        if browser == "patchright":
            return PatchrightController()
        if browser == "playwright":
            return PlaywrightController()
        raise DashboardActionError("choose_browser 必须是 patchright 或 playwright")

    def _append_token(
        self,
        email: str,
        password: str,
        refresh_token: str,
        access_token: str,
        expires_at: str,
    ) -> None:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        path = self.results_dir / "outlook_token.txt"
        with self._file_lock:
            with path.open("a", encoding="utf-8") as token_file:
                token_file.write(
                    f"{email}---{password}---{refresh_token}---"
                    f"{access_token}---{expires_at}\n"
                )

    def _append_checkpoint(
        self,
        email: str,
        password: str,
        stage: str,
        detail: str,
    ) -> None:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        context = getattr(self._checkpoint_context, "value", {}) or {}
        record = {
            "timestamp": self._timestamp(),
            "outlook_email": email,
            "password": password,
            "stage": stage,
            "detail": detail,
            "flow_id": str(context.get("flow_id") or f"dashboard-{uuid.uuid4().hex}"),
            "proxy_session_id": str(context.get("proxy_session_id") or ""),
            "proxy_exit_ip": str(context.get("proxy_exit_ip") or ""),
            "proxy_country_code": str(context.get("proxy_country_code") or ""),
            "identity_country_code": str(context.get("identity_country_code") or ""),
            "browser_locale": str(context.get("browser_locale") or ""),
            "browser_timezone": str(context.get("browser_timezone") or ""),
            "worker_id": str(context.get("worker_id") or threading.get_ident()),
        }
        with self._file_lock:
            with (self.results_dir / "account_checkpoints.jsonl").open(
                "a", encoding="utf-8"
            ) as checkpoint_file:
                checkpoint_file.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()
