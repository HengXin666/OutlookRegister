"""保活动作的 OAuth 检查、授权补充和 HX-Email 导入阶段。"""

from __future__ import annotations

from typing import Any

import outlookregister.dashboard.dashboard_actions as _da
from outlookregister.dashboard.dashboard_action_constants import (
    KEEPALIVE,
    OAUTH_PAGE_DELAY_MS,
    DashboardActionError,
)
from outlookregister.dashboard.dashboard_action_keepalive import _KeepaliveContext


class _KeepaliveCompletionActions:
    def _complete_keepalive(
        self,
        context: _KeepaliveContext,
        login_state: Any,
        resume_destination: str,
    ) -> str:
        config = context.config
        email = context.email
        password = context.password
        controller = context.controller
        proxy_lease = context.proxy_lease
        oauth_config = config.get("oauth2") or {}
        oauth_client_id = str(oauth_config.get("client_id") or "").strip()
        keepalive_config = config.get("keepalive") or {}
        token = self._read_existing_token(email)
        completion_notes: list[str] = []
        token_probe_error = ""

        skip_oauth = resume_destination == "hx_email"
        if skip_oauth:
            self._set_progress(
                email,
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
                email,
                KEEPALIVE,
                "oauth_check",
                "正在验证已有 OAuth refresh token",
            )
            probe = _da.refresh_oauth_token(
                token["refresh_token"],
                client_id=oauth_client_id,
                tenant=oauth_config.get("tenant"),
                scopes=oauth_config.get("Scopes"),
                proxy=getattr(proxy_lease, "proxy", "") or controller.get_proxy(),
                traffic_recorder=controller.traffic,
                email=email,
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
                    email,
                    password,
                    token["refresh_token"],
                    token["access_token"],
                    token["expires_at"],
                )
                self._append_checkpoint(
                    email,
                    password,
                    "oauth_success",
                    "已有 refresh token 经当前住宅 flow 探针验证可用",
                )
                self._mark_keepalive_step(
                    email,
                    "oauth",
                    "completed",
                    "已有 OAuth refresh token 验证可用",
                )
            else:
                token_probe_error = str(probe.get("error") or "probe_failed")
                token = None
                self._append_checkpoint(
                    email,
                    password,
                    "oauth_token_invalid",
                    f"已有 refresh token 探针未通过（{token_probe_error}）",
                )
        elif token and not oauth_client_id and verify_existing_token:
            token_probe_error = "missing_client_id"
            token = None
            self._append_checkpoint(
                email,
                password,
                "oauth_token_invalid",
                "已有 refresh token 无法探针：oauth2.client_id 未配置",
            )
        elif token:
            self._append_checkpoint(
                email,
                password,
                "oauth_token_present",
                "本地已有 refresh token；按配置跳过可用性探针",
            )

        if token and not bool(oauth_config.get("enable_oauth2", False)):
            self._mark_keepalive_step(
                email,
                "oauth",
                "completed",
                "已有 OAuth refresh token，按配置跳过重新授权",
            )
        if bool(oauth_config.get("enable_oauth2", False)) and not token:
            token = self._authorize_keepalive(
                context,
                oauth_client_id,
                token_probe_error,
            )
        if skip_oauth:
            self._mark_keepalive_step(
                email,
                "oauth",
                "completed",
                "按第 6 步继续，沿用已有 OAuth refresh token",
            )

        self._import_keepalive_account(
            context,
            token,
            oauth_client_id,
            completion_notes,
        )
        self._set_progress(
            email,
            KEEPALIVE,
            "finishing",
            "保活步骤已完成，正在整理结果",
        )
        return "保活登录完成" + (
            "；" + "；".join(completion_notes) if completion_notes else ""
        )

    def _read_existing_token(self, email: str) -> dict[str, str] | None:
        try:
            return self.artifacts.oauth_token(email)
        except DashboardActionError:
            return None

    def _authorize_keepalive(
        self,
        context: _KeepaliveContext,
        client_id: str,
        token_probe_error: str,
    ) -> dict[str, str] | None:
        email = context.email
        if not client_id:
            self._mark_keepalive_step(
                email,
                "oauth",
                "completed",
                "OAuth 未配置 client_id，已跳过授权",
            )
            self._append_checkpoint(
                email,
                context.password,
                "oauth_skipped",
                "保活登录成功，但 oauth2.client_id 未配置，跳过补充授权",
            )
            return None

        self._set_progress(
            email,
            KEEPALIVE,
            "oauth_authorize",
            "正在补充 OAuth/Graph 授权",
        )
        suffix = str(context.config.get("email_suffix") or "")
        local_part = email[: -len(suffix)] if suffix else email
        oauth_error = token_probe_error
        candidates = [context.page]
        for index, candidate_page in enumerate(candidates):
            try:
                context.controller.traffic.set_page_stage(
                    candidate_page,
                    "oauth_browser",
                    "oauth_browser_session",
                )
                refresh_token, access_token, expires_at = _da.get_access_token(
                    candidate_page,
                    local_part,
                    password=context.password,
                    proxy=getattr(context.proxy_lease, "proxy", "")
                    or context.controller.get_proxy(),
                    traffic_recorder=context.controller.traffic,
                    recovery_challenge_handler=(
                        context.recovery_challenge_handler
                        if context.recovery_email
                        else None
                    ),
                    page_delay_ms=OAUTH_PAGE_DELAY_MS,
                )
            except Exception as exc:
                refresh_token = access_token = expires_at = False
                oauth_error = str(exc)
            if refresh_token:
                self._append_token(
                    email,
                    context.password,
                    str(refresh_token),
                    str(access_token or ""),
                    str(expires_at),
                )
                self._append_checkpoint(
                    email,
                    context.password,
                    "oauth_success",
                    "保活登录后补充 OAuth2/Graph 授权成功（浏览器会话 + token endpoint）",
                )
                self._mark_keepalive_step(
                    email,
                    "oauth",
                    "completed",
                    "OAuth/Graph 授权已完成",
                )
                return {
                    "refresh_token": str(refresh_token),
                    "access_token": str(access_token or ""),
                    "expires_at": str(expires_at or ""),
                }
            if index == 0:
                context.oauth_page = context.controller.get_oauth_page(
                    context.page,
                    proxy=getattr(context.proxy_lease, "proxy", "")
                    or context.controller.get_proxy(),
                )
                if context.oauth_page:
                    candidates.append(context.oauth_page)
                    context.controller.traffic.set_page_stage(
                        context.oauth_page,
                        "oauth_browser",
                        "oauth_browser_context_fallback",
                    )
        self._append_checkpoint(
            email,
            context.password,
            "oauth_failed",
            f"保活登录后未获取到可用 refresh token（{oauth_error or 'unknown'}）",
        )
        raise DashboardActionError(
            "保活登录成功，但补充 OAuth/Graph 授权未获取到 refresh token"
        )
