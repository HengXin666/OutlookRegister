"""保活动作的准备、资源上下文和最终清理。"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any

import outlookregister.dashboard.dashboard_actions as _da
from outlookregister.config.identity_profiles import select_identity_profile
from outlookregister.dashboard.dashboard_action_constants import (
    KEEPALIVE,
    DashboardActionError,
    KeepaliveSuperseded,
)


@dataclass
class _KeepaliveContext:
    email: str
    password: str
    config: dict[str, Any]
    auth_mode: str
    recovery_record: dict[str, Any]
    recovery_email: str
    identity_profile: dict[str, str]
    controller: Any
    proxy_pool: Any
    proxy_lease: Any = None
    flow_id: str = ""
    worker_id: str = ""
    force_oauth_reauth: bool = False
    page: Any = None
    oauth_page: Any = None
    traffic_started: bool = False
    resolved_mailbox: Any = None
    recovery_challenge_handler: Any = None
    # submit 时创建的状态对象；重新开始（supersede）时新提交会在此对象上打
    # _superseded 标记，旧线程据此检测自己被取代。
    state: dict[str, Any] | None = None
    # 幂等清理标记：被取代的旧线程与新流程的 _discard_preserved_keepalive 可能
    # 并发清理同一个 context，靠此标记避免重复关闭浏览器/重复释放代理。
    cleaned: bool = False


class _KeepaliveActions:
    # 已保留的保活浏览器表：key=email.casefold()，用 self._lock 保护。
    # 保活失败/人工等待时浏览器、页面与代理租约都保留在这里，绝不自动关闭；
    # 仅当用户重新提交保活（主动重新开始）或流程完整成功时才清理。
    _preserved_keepalive: dict[str, _KeepaliveContext]

    def _keepalive(
        self,
        email: str,
        state: dict[str, Any] | None = None,
    ) -> str:
        self._set_progress(
            email,
            KEEPALIVE,
            "preparing",
            "正在读取保活配置与账号资料",
        )
        # 排队/启动期间已被用户重新开始取代：立即退出，不再消耗昂贵的住宅代理。
        self._raise_if_keepalive_superseded(state)
        # 用户主动重新开始保活：先显式清理上一次保留的旧浏览器（允许关闭）。
        self._discard_preserved_keepalive(email)
        context = self._prepare_keepalive_context(email)
        context.state = state
        self._append_checkpoint(
            context.email,
            context.password,
            "keepalive_started",
            f"任务面板启动保活登录（方式={context.auth_mode}）",
        )
        # 保活分两个阶段：登录阶段与完成阶段。任何异常都先保留浏览器、页面证据
        # 与代理租约，绝不自动关闭；只要浏览器已经打开，就保持线程存活并等待人工，
        # 用户点击“继续”后在同一台浏览器里从当前阶段继续，而不是报错退出。
        phase = "login"
        login_state = None
        resume_destination = ""
        while True:
            try:
                self._raise_if_keepalive_superseded(context.state)
                try:
                    if phase == "login":
                        login_state, resume_destination = self._login_keepalive(context)
                        phase = "complete"
                    message = self._complete_keepalive(
                        context,
                        login_state,
                        resume_destination,
                    )
                    break
                except Exception as exc:
                    self._preserve_keepalive_browser(context)
                    if context.page is None:
                        # 浏览器尚未打开（启动阶段失败），没有现场可保留，交给上层报错。
                        raise
                    self._wait_preserved_keepalive_continue(context, phase, exc)
            except KeepaliveSuperseded:
                # 被用户重新开始取代：退出前清理自己尚未移交新流程的浏览器/代理，
                # 但不清洗已被新流程 _discard_preserved_keepalive 接管的现场。
                self._cleanup_if_not_preserved(context)
                raise
        # 只有完整成功路径才正常收尾关闭浏览器并释放代理。
        self._cleanup_keepalive(context)
        return message

    def _wait_preserved_keepalive_continue(
        self,
        context: _KeepaliveContext,
        phase: str,
        error: Exception,
    ) -> None:
        """异常后保留浏览器等待人工；用户点“继续”后返回并从当前阶段重试。

        与人工验证等待一样使用 retry_on_timeout=True：等待超时不清空状态、不
        退出线程、不关闭浏览器，只是刷新提示继续等待。
        """
        config = context.config
        try:
            manual_timeout = max(
                1,
                min(
                    int(
                        (config.get("keepalive") or {}).get(
                            "manual_verification_timeout_seconds", 300
                        )
                    ),
                    3600,
                ),
            )
        except (TypeError, ValueError):
            manual_timeout = 300
        detail = str(error).strip() or error.__class__.__name__
        step = "oauth" if phase == "complete" else "manual_challenge"
        self._await_manual_verification(
            context.email,
            KEEPALIVE,
            f"保活流程异常：{detail}；浏览器已保留且不会自动关闭，"
            "请完成页面操作后点击“继续”",
            timeout_seconds=manual_timeout,
            page=context.page,
            retry_on_timeout=True,
            step=step,
        )
        # 等待期间被用户重新开始取代：不再从旧阶段继续，直接退出。
        self._raise_if_keepalive_superseded(context.state)

    def _preserved_keepalive_table(self) -> dict[str, _KeepaliveContext]:
        table = getattr(self, "_preserved_keepalive", None)
        if table is None:
            table = {}
            self._preserved_keepalive = table
        return table

    def _cleanup_if_not_preserved(self, context: _KeepaliveContext) -> None:
        """被取代的旧流程退出前清理自己尚未移交新流程的浏览器/代理。

        新流程在 _keepalive 开头会 _discard_preserved_keepalive 清理保留表里的
        旧 context；若旧 context 已不在保留表中（中途被取代），则由本线程自行
        清理，避免浏览器与住宅代理泄漏。_cleanup_keepalive 幂等，即使与新流程的
        移交清理并发执行也不会重复关闭/释放。
        """
        key = context.email.casefold()
        with self._lock:
            table = self._preserved_keepalive_table()
            if table.get(key) is context:
                # 已在新流程 _discard_preserved_keepalive 的管辖下，等待其清理。
                return
        self._cleanup_keepalive(context)

    def _discard_preserved_keepalive(self, email: str) -> None:
        """用户重新提交保活时，显式清理该邮箱保留的旧浏览器（语义允许关闭）。"""
        key = str(email or "").strip().casefold()
        with self._lock:
            context = self._preserved_keepalive_table().pop(key, None)
        if context is not None:
            self._cleanup_keepalive(context)

    def _preserve_keepalive_browser(self, context: _KeepaliveContext) -> None:
        """保活异常收尾：记录页面证据并保留 controller/page/代理租约，可恢复。"""
        # 已被重新开始取代：不写回保留表、不覆盖新流程状态，直接上抛退出。
        self._raise_if_keepalive_superseded(context.state)
        reason = "保活流程异常，浏览器已保留"
        if context.page is not None:
            try:
                self._capture_page_record(
                    context.email,
                    KEEPALIVE,
                    context.page,
                    reason,
                )
            except Exception:
                pass
        with self._lock:
            self._preserved_keepalive_table()[context.email.casefold()] = context
        try:
            self._set_state(
                context.email,
                KEEPALIVE,
                "running",
                f"{reason}：页面证据已记录，代理与会话保持有效，可重新提交保活继续",
                log_level="warning",
            )
        except Exception:
            pass

    def _prepare_keepalive_context(self, email: str) -> _KeepaliveContext:
        normalized_email, password = self.artifacts.credentials(email)
        config = self._config()
        options = self._action_options(normalized_email, KEEPALIVE)
        auth_mode = str(options.get("auth_mode") or "password").strip().casefold()
        force_oauth_reauth = bool(options.get("force_oauth_reauth"))
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
        try:
            if proxy_pool is not None:
                self._set_progress(
                    normalized_email,
                    KEEPALIVE,
                    "proxy",
                    "正在申请保活住宅代理",
                )
                proxy_lease = self._acquire_proxy(
                    proxy_pool,
                    ""
                    if getattr(proxy_pool, "auto_identity", False)
                    else identity_profile["country_code"],
                )
                if getattr(proxy_pool, "auto_identity", False):
                    identity_profile = proxy_pool.identity_profile_for_lease(proxy_lease)
                controller.set_proxy(proxy_lease.proxy)

            controller.results_dir = str(self.results_dir)
            controller.traffic = _da.TrafficRecorder(self.results_dir)
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
            context = _KeepaliveContext(
                email=normalized_email,
                password=password,
                config=config,
                auth_mode=auth_mode,
                recovery_record=recovery_record,
                recovery_email=recovery_email,
                identity_profile=identity_profile,
                controller=controller,
                proxy_pool=proxy_pool,
                proxy_lease=proxy_lease,
                flow_id=flow_id,
                worker_id=worker_id,
                force_oauth_reauth=force_oauth_reauth,
            )
            context.recovery_challenge_handler = (
                self._recovery_challenge_handler(context)
                if recovery_email
                else None
            )
            return context
        except Exception:
            if proxy_pool is not None:
                try:
                    proxy_pool.release(proxy_lease)
                except Exception:
                    pass
            try:
                controller.hx_email.close()
            except Exception:
                pass
            raise

    @staticmethod
    def _recovery_challenge_handler(context: _KeepaliveContext):
        def handle(challenge_page):
            if context.resolved_mailbox is None:
                context.resolved_mailbox = context.controller.get_flow_hx_email().resolve_mailbox(
                    context.recovery_email,
                    mailbox_hint=context.recovery_record,
                )
            return context.controller.confirm_recovery_email_challenge(
                challenge_page,
                context.controller.get_flow_hx_email(),
                context.resolved_mailbox,
                context.recovery_email,
            )

        return handle

    def _cleanup_keepalive(self, context: _KeepaliveContext) -> None:
        # 被取代的旧线程与新流程的 _discard_preserved_keepalive 可能并发清理同一
        # 个 context；用锁保证“检查-置位”原子，避免重复关闭浏览器/释放代理。
        with self._lock:
            if context.cleaned:
                return
            context.cleaned = True
        if context.oauth_page is not None:
            try:
                context.controller.clean_up(context.oauth_page, "done_browser")
            except Exception:
                pass
        if context.page is not None:
            try:
                context.controller.clean_up(context.page, "done_browser")
            except Exception:
                pass
        try:
            context.controller.close_thread_browser()
        except Exception:
            pass
        try:
            context.controller.clean_up(type="all_browser")
        except Exception:
            pass
        if context.traffic_started:
            try:
                context.controller.traffic.finish_task()
            except Exception:
                pass
        try:
            context.controller.hx_email.close()
        except Exception:
            pass
        try:
            context.controller.clear_flow_context()
        except Exception:
            pass
        self._clear_checkpoint_context()
        if context.proxy_pool is not None:
            try:
                context.proxy_pool.release(context.proxy_lease)
            except Exception as exc:
                print(f"[Dashboard Proxy] 释放会话失败: {exc}")
        # 成功收尾（或主动重新开始清理）时同时清除保留表条目。
        try:
            with self._lock:
                table = getattr(self, "_preserved_keepalive", None)
                if table is not None:
                    table.pop(context.email.casefold(), None)
        except Exception:
            pass
