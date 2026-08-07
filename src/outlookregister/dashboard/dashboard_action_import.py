"""HX-Email 导入与配置/控制器工厂 mixin。"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import outlookregister.dashboard.dashboard_actions as _da
from outlookregister.browser.patchright_controller import PatchrightController
from outlookregister.browser.playwright_controller import PlaywrightController
from outlookregister.config.config_store import (
    ConfigError,
    ConfigStore,
    validate_config,
)
from outlookregister.config.identity_profiles import select_identity_profile
from outlookregister.dashboard.dashboard_action_constants import (
    HX_EMAIL_HANDOFF_DELAY_SECONDS,
    DashboardActionError,
)
from outlookregister.email.hx_email_client import HXEmailClient
from outlookregister.proxy.proxy_rotation import ProxyRotationError, RotatingProxyPool


def _traffic_recorder_runtime(results_dir):
    return _da.TrafficRecorder(results_dir)


class _ImportActions:

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

        recorder = _traffic_recorder_runtime(self.results_dir)
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
        if config.get("debug"):
            return None
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
        return datetime.now(UTC).isoformat()
