"""Background account recovery actions used by the local dashboard."""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from controllers.patchright_controller import PatchrightController
from controllers.playwright_controller import PlaywrightController
from get_token import get_access_token
from hx_email_client import HXEmailClient
from traffic_tracker import TrafficRecorder


AUTHORIZE = "authorize"
IMPORT_HX_EMAIL = "import_hx_email"
VALID_ACTIONS = {AUTHORIZE, IMPORT_HX_EMAIL}

# The dashboard retry flow runs against pages that may still be rendering
# Microsoft security controls. Keep these waits local to that flow so normal
# registration is not slowed down as a side effect.
OAUTH_PAGE_DELAY_MS = 1500
HX_EMAIL_HANDOFF_DELAY_SECONDS = 1.5
SUCCESS_WINDOW_DELAY_MS = 3000


class DashboardActionError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


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

    def submit(self, email: str, action: str) -> dict[str, Any]:
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
                state.get("status") in {"queued", "running"}
                for state in account_states.values()
            ):
                raise DashboardActionError("该账号已有操作正在执行", status_code=409)
            state = {
                "email": normalized_email,
                "action": action,
                "status": "queued",
                "message": "操作已排队",
                "updated_at": now,
            }
            account_states[action] = state
        try:
            self.executor.submit(self._run, normalized_email, action)
        except Exception as exc:
            with self._lock:
                current_states = self._states.get(key, {})
                if current_states.get(action) is state:
                    current_states.pop(action, None)
                    if not current_states:
                        self._states.pop(key, None)
            raise DashboardActionError(
                f"操作排队失败，请稍后重试: {exc}",
                status_code=503,
            ) from exc
        return dict(state)

    def snapshot(self) -> dict[str, dict[str, dict[str, Any]]]:
        with self._lock:
            return {
                email: {
                    action: dict(state)
                    for action, state in action_states.items()
                }
                for email, action_states in self._states.items()
            }

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _set_state(
        self,
        email: str,
        action: str,
        status: str,
        message: str,
    ) -> None:
        with self._lock:
            self._states.setdefault(email.casefold(), {})[action] = {
                "email": email,
                "action": action,
                "status": status,
                "message": message,
                "updated_at": self._timestamp(),
            }

    def _run(self, email: str, action: str) -> None:
        running_message = (
            "正在执行 OAuth 授权"
            if action == AUTHORIZE
            else "正在加入 HX-Email"
        )
        self._set_state(email, action, "running", running_message)
        try:
            message = self._execute_action(email, action)
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            self._set_state(
                email,
                action,
                "failed",
                self._public_error(email, detail)[:500],
            )
        else:
            self._set_state(email, action, "succeeded", message)

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
        for secret in sorted(set(filter(None, secrets)), key=len, reverse=True):
            detail = detail.replace(secret, "[redacted]")
        return detail

    def _execute_action(self, email: str, action: str) -> str:
        if action == AUTHORIZE:
            return self._authorize(email)
        if action == IMPORT_HX_EMAIL:
            return self._import_hx_email(email)
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
        controller = self._controller(config)
        controller.results_dir = str(self.results_dir)
        controller.traffic = TrafficRecorder(self.results_dir)
        controller.hx_email.set_traffic_recorder(controller.traffic)
        controller.thread_local.flow_id = f"dashboard-{uuid.uuid4().hex}"
        controller.thread_local.worker_id = str(threading.get_ident())
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
                flow_id=controller.thread_local.flow_id,
                worker_id=controller.thread_local.worker_id,
            )
            traffic_started = True
            controller.traffic.attach_page(page, "oauth_browser", "oauth_browser")
            refresh_token, access_token, expires_at = get_access_token(
                page,
                local_part,
                password=password,
                proxy=controller.get_proxy(),
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
        recorder.start_task(normalized_email, flow_id=f"dashboard-{uuid.uuid4().hex}")
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
                proxy_url=str(hx_config.get("proxy_url") or "").strip(),
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

    def _config(self) -> dict[str, Any]:
        path = self.project_root / "config.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DashboardActionError("无法读取 config.json") from exc
        if not isinstance(value, dict):
            raise DashboardActionError("config.json 顶层必须是对象")
        return value

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
        record = {
            "timestamp": self._timestamp(),
            "outlook_email": email,
            "password": password,
            "stage": stage,
            "detail": detail,
            "flow_id": f"dashboard-{uuid.uuid4().hex}",
            "worker_id": str(threading.get_ident()),
        }
        with self._file_lock:
            with (self.results_dir / "account_checkpoints.jsonl").open(
                "a", encoding="utf-8"
            ) as checkpoint_file:
                checkpoint_file.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()
