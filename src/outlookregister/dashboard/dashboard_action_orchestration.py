"""runner 编排：_run / _public_error / _config_secret_values / _execute_action。"""
from __future__ import annotations

import re
from typing import Any

from outlookregister.dashboard.dashboard_action_constants import (
    AUTHORIZE,
    IMPORT_HX_EMAIL,
    KEEPALIVE,
    DashboardActionError,
    ManualVerificationRequired,
)


class _RunnerOrchestrator:

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
