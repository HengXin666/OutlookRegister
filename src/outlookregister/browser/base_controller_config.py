"""Shared controller configuration, flow context, and traffic state."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import outlookregister.browser.base_controller as _base_controller
from outlookregister import PROJECT_ROOT
from outlookregister.config.config_store import ConfigStore


class _BaseControllerConfig:
    """Initialize per-controller settings and per-flow thread-local state."""

    def __init__(self) -> None:
        data = ConfigStore(PROJECT_ROOT / "config.json").read()
        self.wait_time = data["bot_protection_wait"] * 1000
        self.max_captcha_retries = data["max_captcha_retries"]
        self.enable_oauth2 = data["oauth2"]["enable_oauth2"]
        self.proxy = data["proxy"]
        self.debug = bool(data.get("debug", False))
        self.strict_isolation = bool(data.get("strict_isolation", True)) and not self.debug
        self.isolate_hx_email_group = bool(
            data.get("isolate_hx_email_group", self.strict_isolation)
        )
        self.prevent_direct_network_leaks = bool(
            data.get("prevent_direct_network_leaks", True)
        )

        identity = data.get("identity") or {}
        self.identity_config = dict(identity)
        self.country_code = str(identity.get("country_code") or "").strip()
        self.browser_locale = str(
            identity.get("browser_locale")
            or identity.get("locale")
            or "en-US"
        ).strip()
        self.browser_timezone = str(identity.get("timezone") or "").strip()
        self.require_dynamic_residential_ip = bool(
            identity.get("require_dynamic_residential_ip", self.strict_isolation)
        )
        self.email_suffix = data["email_suffix"]

        self.results_dir = str(PROJECT_ROOT / "Results")
        os.makedirs(self.results_dir, exist_ok=True)
        self.recovery_email_config = data.get("recovery_email") or {}
        self.recovery_email_enabled = bool(
            self.recovery_email_config.get("enabled", False)
        )
        self.hx_email_proxy_url = str(
            (self.recovery_email_config.get("hx_email") or {}).get("proxy_url", "")
        ).strip()
        self.recovery_code_attempts = max(
            1, int(self.recovery_email_config.get("max_code_attempts", 2))
        )
        self.hx_email = _base_controller.HXEmailClient(
            self.recovery_email_config.get("hx_email") or {}
        )
        self.traffic = _base_controller.TrafficRecorder(self.results_dir)
        self.hx_email.set_traffic_recorder(self.traffic)
        self.oauth_client_id = data["oauth2"]["client_id"]

        self.thread_local = threading.local()
        self.cleanup_lock = threading.Lock()
        self.results_lock = threading.Lock()
        self.active_resources: list[tuple[Any, Any]] = []
        self.oauth_browsers: dict[int, tuple[Any, Any]] = {}

    def set_proxy(self, proxy: str | None) -> None:
        """Set the proxy used by the current registration flow."""
        normalized = str(proxy or "").strip()
        if normalized:
            self.thread_local.proxy = normalized
        elif hasattr(self.thread_local, "proxy"):
            delattr(self.thread_local, "proxy")

    def set_flow_context(
        self,
        flow_id: str,
        proxy_session_id: str = "",
        proxy_exit_ip: str = "",
        proxy_country_code: str = "",
        worker_id: str = "",
        browser_locale: str = "",
        browser_timezone: str = "",
        flow_country_code: str = "",
    ) -> None:
        self.thread_local.flow_id = str(flow_id or "")
        self.thread_local.proxy_session_id = str(proxy_session_id or "")
        self.thread_local.proxy_exit_ip = str(proxy_exit_ip or "")
        self.thread_local.flow_country_code = str(
            flow_country_code
            or proxy_country_code
            or getattr(self, "country_code", "")
            or ""
        ).strip()
        self.thread_local.proxy_country_code = str(
            proxy_country_code or self.thread_local.flow_country_code or ""
        ).strip()
        self.thread_local.worker_id = str(worker_id or "")
        self.thread_local.browser_locale = str(
            browser_locale or getattr(self, "browser_locale", "") or ""
        ).strip()
        self.thread_local.browser_timezone = str(
            browser_timezone or getattr(self, "browser_timezone", "") or ""
        ).strip()

        previous_client = getattr(self.thread_local, "hx_email", None)
        if previous_client is not None:
            try:
                previous_client.close()
            except Exception:
                pass

        hx_email_config = dict(self.recovery_email_config.get("hx_email") or {})
        if self.isolate_hx_email_group:
            base_group = str(
                hx_email_config.get("account_group", "OutlookRegister 自动注册")
            ).strip()
            hx_email_config["account_group"] = (
                f"{base_group} [{self.thread_local.flow_id}]"
            )
        flow_client = _base_controller.HXEmailClient(hx_email_config)
        flow_client.set_traffic_recorder(getattr(self, "traffic", None))
        self.thread_local.hx_email = flow_client

    def get_flow_hx_email(self) -> Any:
        return getattr(self.thread_local, "hx_email", self.hx_email)

    def clear_flow_context(self) -> None:
        for attribute in (
            "flow_id", "proxy_session_id", "proxy_exit_ip", "flow_country_code",
            "proxy_country_code", "worker_id", "browser_locale", "browser_timezone",
            "captcha_attempts", "proxy", "last_pos", "recovery_email",
            "recovery_mailbox", "credentials_saved", "recovery_result",
        ):
            if hasattr(self.thread_local, attribute):
                delattr(self.thread_local, attribute)
        flow_client = getattr(self.thread_local, "hx_email", None)
        if flow_client is not None:
            try:
                flow_client.close()
            except Exception:
                pass
            delattr(self.thread_local, "hx_email")

    def record_captcha_attempt(self) -> int:
        attempts = getattr(self.thread_local, "captcha_attempts", 0) + 1
        self.thread_local.captcha_attempts = attempts
        traffic = getattr(self, "traffic", None)
        if traffic is not None:
            traffic.set_captcha_attempts(attempts)
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "flow_id": getattr(self.thread_local, "flow_id", ""),
            "proxy_session_id": getattr(self.thread_local, "proxy_session_id", ""),
            "proxy_exit_ip": getattr(self.thread_local, "proxy_exit_ip", ""),
            "identity_country_code": getattr(self.thread_local, "flow_country_code", ""),
            "proxy_country_code": getattr(self.thread_local, "proxy_country_code", ""),
            "browser_locale": getattr(self.thread_local, "browser_locale", ""),
            "browser_timezone": getattr(self.thread_local, "browser_timezone", ""),
            "worker_id": getattr(self.thread_local, "worker_id", ""),
            "attempt": attempts,
        }
        path = os.path.join(self.results_dir, "captcha_attempts.jsonl")
        try:
            with self.results_lock:
                with open(path, "a", encoding="utf-8") as attempts_file:
                    attempts_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"[Captcha] 尝试记录失败: {exc}")
        return attempts

    def get_proxy(self) -> str | None:
        """Return the flow proxy, or no route when dynamic mode is required."""
        flow_proxy = getattr(self.thread_local, "proxy", None)
        if flow_proxy:
            return flow_proxy
        if getattr(self, "require_dynamic_residential_ip", False) and not getattr(
            self, "debug", False
        ):
            return None
        return getattr(self, "proxy", None)
