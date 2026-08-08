"""dashboard 动作共享常量、异常与账号本地工件存储。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


class KeepaliveSuperseded(DashboardActionError):
    """保活已被用户重新提交的新流程取代，旧线程应静默退出。

    不写入任何状态（状态已由新流程占用），只在 _run 里被专门捕获后忽略。
    """

    def __init__(self, message: str = "保活已被重新开始，旧流程停止"):
        super().__init__(message, status_code=409)


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
