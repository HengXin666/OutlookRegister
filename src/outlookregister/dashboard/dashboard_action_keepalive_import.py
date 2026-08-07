"""保活动作的 HX-Email 导入阶段。"""

from __future__ import annotations

from outlookregister.dashboard.dashboard_action_constants import KEEPALIVE
from outlookregister.dashboard.dashboard_action_keepalive import _KeepaliveContext


class _KeepaliveImportActions:
    def _import_keepalive_account(
        self,
        context: _KeepaliveContext,
        token: dict[str, str] | None,
        client_id: str,
        completion_notes: list[str],
    ) -> None:
        email = context.email
        password = context.password
        should_import = bool(
            (context.config.get("keepalive") or {}).get("auto_import_hx_email", True)
        )
        if not should_import:
            completion_notes.append("按配置跳过 HX-Email 导入")
            self._mark_keepalive_step(email, "hx_email", "completed", "按配置跳过 HX-Email 导入")
            self._append_checkpoint(
                email,
                password,
                "hx_email_import_skipped",
                "keepalive.auto_import_hx_email=false",
            )
            return
        if not token:
            completion_notes.append("没有可用 OAuth/Graph refresh token，已跳过 HX-Email")
            self._mark_keepalive_step(
                email,
                "hx_email",
                "completed",
                "没有可用 OAuth refresh token，已跳过 HX-Email 导入",
            )
            self._append_checkpoint(
                email,
                password,
                "hx_email_import_skipped",
                "保活登录完成，但没有可用 refresh token，跳过 HX-Email 导入",
            )
            return
        if not client_id:
            completion_notes.append("HX-Email 未配置 client_id，已跳过导入")
            self._mark_keepalive_step(
                email,
                "hx_email",
                "completed",
                "HX-Email 未配置 client_id，已跳过导入",
            )
            self._append_checkpoint(
                email,
                password,
                "hx_email_import_skipped",
                "保活登录成功，但 oauth2.client_id 未配置，跳过 HX-Email 导入",
            )
            return

        self._set_progress(email, KEEPALIVE, "hx_email", "正在将账号加入 HX-Email")
        hx_config = dict(
            (context.config.get("recovery_email") or {}).get("hx_email") or {}
        )
        imported = context.controller.get_flow_hx_email().import_outlook_account(
            email=email,
            password=password,
            recovery_email=context.recovery_email,
            client_id=client_id,
            refresh_token=token["refresh_token"],
            proxy_url=(
                getattr(context.proxy_lease, "proxy", "")
                or str(hx_config.get("proxy_url") or "").strip()
            ),
        )
        self._append_checkpoint(
            email,
            password,
            "hx_email_imported",
            f'保活后加入 HX-Email account_id={imported["account_id"]}',
        )
        self._mark_keepalive_step(email, "hx_email", "completed", "账号已加入 HX-Email")
