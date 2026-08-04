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
            if options:
                state["options"] = {
                    "auth_mode": str(options.get("auth_mode") or "password")
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

    def resume_verification(self, email: str, action: str) -> dict[str, Any]:
        key = (str(email or "").strip().casefold(), str(action or "").strip())
        with self._lock:
            event = self._verification_events.get(key)
            state = self._states.get(key[0], {}).get(key[1])
        if event is None or not state or state.get("status") != "manual_verification_required":
            raise DashboardActionError("该账号当前没有等待人工验证的操作", status_code=409)
        event.set()
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
            else "正在保活登录" if action == KEEPALIVE else "正在加入 HX-Email"
        )
        self._set_state(email, action, "running", running_message)
        try:
            message = self._execute_action(email, action)
        except ManualVerificationRequired as exc:
            self._set_state(email, action, "manual_verification_required", str(exc)[:500])
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
            if proxy_pool is not None:
                proxy_pool.verify_browser_page(page, proxy_lease)
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

        page.goto(
            "https://outlook.live.com/mail/0/",
            timeout=30000,
            wait_until="domcontentloaded",
        )
        deadline = time.monotonic() + timeout_seconds
        net_errors = 0
        unknown_rounds = 0
        email_rounds = 0

        while time.monotonic() < deadline:
            state = classify_outlook_page(page)
            if is_authenticated(state):
                return state

            if is_manual_verification(state):
                self._await_manual_verification(
                    email,
                    KEEPALIVE,
                    f"检测到人工验证（{state.evidence}）。请在已打开的浏览器中完成后点击继续；继续前会再次检查验证是否消失",
                    timeout_seconds=manual_timeout,
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

            if state.name == "sms_verify":
                if not recovery_email or recovery_challenge_handler is None:
                    raise DashboardActionError(
                        "Outlook 要求安全代码/手机验证，但当前账号没有可用的密保邮箱取件处理器"
                    )
                if not recovery_challenge_handler(page):
                    raise DashboardActionError("密保邮箱安全代码验证未完成")
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
                self._submit_outlook_form(page)
                self._wait_for_page(page, 1500)
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
            page = controller.get_thread_page()
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
            if proxy_pool is not None:
                proxy_pool.verify_browser_page(page, proxy_lease)
            login_state = self._login_outlook_account(
                page,
                controller,
                normalized_email,
                password,
                recovery_email,
                recovery_challenge_handler if recovery_email else None,
                config,
            )
            self._append_checkpoint(
                normalized_email,
                password,
                "keepalive_logged_in",
                f"保活登录成功（方式={auth_mode}；证据={login_state.evidence}）",
            )

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

            verify_existing_token = bool(
                keepalive_config.get("verify_existing_oauth_token", True)
            )
            if token and not str(token.get("refresh_token") or "").strip():
                token = None
            if token and verify_existing_token and oauth_client_id:
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

            if bool(oauth_config.get("enable_oauth2", False)) and not token:
                if not oauth_client_id:
                    completion_notes.append("OAuth 未配置 client_id，已跳过补充授权")
                    self._append_checkpoint(
                        normalized_email,
                        password,
                        "oauth_skipped",
                        "保活登录成功，但 oauth2.client_id 未配置，跳过补充授权",
                    )
                else:
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
                            if proxy_pool is not None:
                                proxy_pool.verify_browser_page(candidate_page, proxy_lease)
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
                                if proxy_pool is not None:
                                    proxy_pool.verify_browser_page(oauth_page, proxy_lease)
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

            if token and bool(keepalive_config.get("auto_import_hx_email", True)):
                if not oauth_client_id:
                    completion_notes.append("HX-Email 未配置 client_id，已跳过导入")
                    self._append_checkpoint(
                        normalized_email,
                        password,
                        "hx_email_import_skipped",
                        "保活登录成功，但 oauth2.client_id 未配置，跳过 HX-Email 导入",
                    )
                else:
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
            elif not token and bool(keepalive_config.get("auto_import_hx_email", True)):
                completion_notes.append("没有可用 OAuth/Graph refresh token，已跳过 HX-Email")
                self._append_checkpoint(
                    normalized_email,
                    password,
                    "hx_email_import_skipped",
                    "保活登录完成，但没有可用 refresh token，跳过 HX-Email 导入",
                )
            elif not bool(keepalive_config.get("auto_import_hx_email", True)):
                completion_notes.append("按配置跳过 HX-Email 导入")
                self._append_checkpoint(
                    normalized_email,
                    password,
                    "hx_email_import_skipped",
                    "keepalive.auto_import_hx_email=false",
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

    def _await_manual_verification(
        self,
        email: str,
        action: str,
        message: str,
        timeout_seconds: int,
    ) -> None:
        key = (email.casefold(), action)
        event = threading.Event()
        with self._lock:
            self._verification_events[key] = event
        self._set_state(email, action, "manual_verification_required", message)
        try:
            if not event.wait(timeout=max(1, min(timeout_seconds, 3600))):
                raise DashboardActionError("人工验证等待超时，请重新提交保活操作")
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
