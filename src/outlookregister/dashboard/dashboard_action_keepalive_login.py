"""保活动作的 Outlook 登录阶段。"""

from __future__ import annotations

from typing import Any

from outlookregister.dashboard.dashboard_action_constants import KEEPALIVE
from outlookregister.dashboard.dashboard_action_keepalive import _KeepaliveContext


class _KeepaliveLoginActions:
    @staticmethod
    def _keepalive_page_dead(page: Any) -> bool:
        """Whether the keepalive page object can no longer be used.

        ``page.url`` raises on a closed Playwright/Patchright page and is cheap
        on a live one. MagicMock test doubles expose a truthy attribute instead
        of raising, so they are correctly treated as alive.
        """
        if page is None:
            return True
        try:
            _ = page.url
            return False
        except Exception:
            return True

    def _reopen_keepalive_page(
        self,
        email: str,
        controller: Any,
        page_holder: list[Any],
    ) -> Any:
        """Recreate the keepalive page when the previous one was closed.

        Uses the same controller and the same thread-local proxy session, so
        no new residential session is consumed; the traffic recorder is
        re-attached so the page keeps being tracked. ``page_holder[0]`` is
        replaced with the new page so callers keep a live reference.
        """
        old_page = page_holder[0]
        try:
            controller.traffic.detach_page(old_page)
        except Exception:
            pass
        new_page = controller.get_thread_page()
        controller.traffic.attach_page(
            new_page,
            "keepalive_login",
            "keepalive_browser",
        )
        page_holder[0] = new_page
        self._mark_browser_open(email, KEEPALIVE)
        self._set_progress(
            email,
            KEEPALIVE,
            "browser",
            "检测到浏览器页面已失效，已重新打开页面（同一代理会话）",
        )
        print(
            f"[Keepalive] 页面已失效，已重建 keepalive 页面（{email}）",
            flush=True,
        )
        return new_page

    def _login_keepalive(
        self,
        context: _KeepaliveContext,
    ) -> tuple[Any, str]:
        # 被用户重新开始取代后不得再启动/继续旧浏览器。
        self._raise_if_keepalive_superseded(context.state)
        # 异常保留后的“继续”会再次进入这里：复用已打开的 page 与流量会话，
        # 绝不在同一台浏览器上重复开新页面。
        if context.page is None:
            self._set_progress(
                context.email,
                KEEPALIVE,
                "browser",
                "正在启动保活浏览器",
            )
            context.page = context.controller.get_thread_page()
            self._mark_browser_open(context.email, KEEPALIVE)
            context.controller.traffic.start_task(
                context.email,
                flow_id=context.flow_id,
                proxy_session_id=getattr(context.proxy_lease, "session_id", ""),
                proxy_exit_ip=getattr(context.proxy_lease, "exit_ip", ""),
                proxy_country_code=(
                    getattr(context.proxy_lease, "country_code", "")
                    or context.identity_profile["country_code"]
                ),
                identity_country_code=context.identity_profile["country_code"],
                browser_locale=context.identity_profile["browser_locale"],
                browser_timezone=context.identity_profile["timezone"],
                worker_id=context.worker_id,
            )
            context.traffic_started = True
            context.controller.traffic.attach_page(
                context.page,
                "keepalive_login",
                "keepalive_browser",
            )
        elif self._keepalive_page_dead(context.page):
            # 页面在人工等待期间被关闭/崩溃：立即重建页面而不是反复把流程
            # 卡在“无法识别页面状态”的死等里。代理会话保持不变。
            context.page = self._reopen_keepalive_page(
                context.email,
                context.controller,
                [context.page],
            )
        # 登录循环内部也可能检测到页面失效并重建；通过 page_holder 把重建后
        # 的新页面传回 context，确保后续 OAuth 授权阶段使用的是同一个活页面。
        page_holder = [context.page]
        login_state = self._login_outlook_account(
            context.page,
            context.controller,
            context.email,
            context.password,
            context.recovery_email,
            context.recovery_challenge_handler,
            context.config,
            page_holder=page_holder,
        )
        context.page = page_holder[0]
        resume_destination = self._consume_resume_destination(context.email)
        requested_after_login = self._consume_resume_step(
            context.email,
            KEEPALIVE,
        )
        if requested_after_login == "hx_email":
            resume_destination = "hx_email"
        self._append_checkpoint(
            context.email,
            context.password,
            "keepalive_logged_in",
            f"保活登录成功（方式={context.auth_mode}；证据={login_state.evidence}）",
        )
        self._set_progress(
            context.email,
            KEEPALIVE,
            "oauth_check",
            "登录完成，正在检查 OAuth/Graph 授权",
        )
        requested_before_oauth = self._consume_resume_step(
            context.email,
            KEEPALIVE,
        )
        if requested_before_oauth == "hx_email":
            resume_destination = "hx_email"
        return login_state, resume_destination
