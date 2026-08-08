"""runner 编排：_run / _public_error / _config_secret_values / _execute_action。"""
from __future__ import annotations

import re
from typing import Any


from outlookregister.dashboard.dashboard_action_constants import (
    AUTHORIZE,
    IMPORT_HX_EMAIL,
    KEEPALIVE,
    DashboardActionError,
    KeepaliveSuperseded,
    ManualVerificationRequired,
)


class _RunnerOrchestrator:

    def _run(
        self,
        email: str,
        action: str,
        state: dict[str, Any] | None = None,
    ) -> None:
        if action == KEEPALIVE and state is not None and state.get("_superseded"):
            # 排队/启动期间已被用户重新开始取代：静默退出，不触碰任何状态。
            return
        running_message = (
            "正在执行 OAuth 授权"
            if action == AUTHORIZE
            else "正在保活登录" if action == KEEPALIVE else "正在加入 HX-Email"
        )
        self._set_state(email, action, "running", running_message, step="starting")
        try:
            if action == KEEPALIVE:
                # 供 _await_manual_verification/_wait_if_paused 等等待点检测被取代。
                self._keepalive_thread_state.state = state
            self._wait_if_paused(email, action)
            message = self._execute_action(email, action, state=state)
        except KeepaliveSuperseded:
            # 被新提交的保活流程取代：新流程已接管状态与浏览器，旧线程直接退出。
            return
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
            # 同时：被取代的旧线程若恰好在收尾前完成，绝不能把 succeeded 写进
            # 新流程的状态，检测到被取代后静默退出。
            try:
                self._wait_if_paused(email, action)
            except KeepaliveSuperseded:
                return
            if action == KEEPALIVE:
                self._finish_keepalive_steps(email)
            self._set_state(email, action, "succeeded", message, step="complete")
        finally:
            # 只移除属于本流程的 control event：被取代的旧线程退出时，绝不能误删
            # 新流程的 control event（否则新流程的暂停/继续控制会失效）。
            with self._lock:
                key = (email.casefold(), action)
                event = self._control_events.get(key)
                current_state = self._states.get(key[0], {}).get(key[1])
                if (
                    event is not None
                    and current_state is not None
                    and current_state.get("_control_event") is event
                ):
                    self._control_events.pop(key, None)

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

    def _execute_action(
        self,
        email: str,
        action: str,
        state: dict[str, Any] | None = None,
    ) -> str:
        if action == AUTHORIZE:
            return self._authorize(email)
        if action == IMPORT_HX_EMAIL:
            return self._import_hx_email(email)
        if action == KEEPALIVE:
            return self._keepalive(email, state=state)
        raise DashboardActionError("不支持的账号操作")
