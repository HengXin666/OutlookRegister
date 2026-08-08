"""Base configuration and lifecycle mixin for ``HXEmailClient``.

State shared across every mixin (transport, mailbox, code and import helpers)
lives here so the composite client has a single ``__init__`` owning all
instance attributes.
"""

import os
import threading

import requests


class HXEmailError(RuntimeError):
    """HX-Email client error carrying an optional HTTP status code."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class _HXEmailBase:
    """Configuration, credentials and shared request helpers."""

    def __init__(self, config, session=None):
        configured_url = str(config.get("base_url", "")).strip().rstrip("/")
        self.base_url = (
            configured_url[:-7]
            if configured_url.lower().endswith("/api/v1")
            else configured_url
        )
        self.prefer_session_api = configured_url.lower().endswith("/api/v1")
        self.api_key = os.getenv("HX_EMAIL_API_KEY", str(config.get("api_key", ""))).strip()
        self.username = os.getenv("HX_EMAIL_USERNAME", str(config.get("username", ""))).strip()
        self.password = os.getenv("HX_EMAIL_PASSWORD", str(config.get("password", ""))).strip()
        self.timeout = float(config.get("request_timeout_seconds", 15))
        self.code_timeout = float(config.get("code_timeout_seconds", 120))
        self.poll_interval = float(config.get("poll_interval_seconds", 3))
        try:
            self.code_timestamp_skew_seconds = max(
                0.0, float(config.get("code_timestamp_skew_seconds", 15))
            )
        except (TypeError, ValueError):
            self.code_timestamp_skew_seconds = 15.0
        try:
            configured_max_age = float(
                config.get("code_max_age_seconds", max(self.code_timeout, 300.0))
            )
        except (TypeError, ValueError):
            configured_max_age = max(self.code_timeout, 300.0)
        self.code_max_age_seconds = max(1.0, configured_max_age)
        self.caller_id = str(config.get("caller_id", "outlook-register")).strip()
        self.account_group = str(
            config.get("account_group", "OutlookRegister 自动注册")
        ).strip()
        # Registration and keepalive can target different groups. Each falls
        # back to the shared ``account_group`` so older configs keep working.
        self.register_account_group = str(
            config.get("register_account_group") or self.account_group
        ).strip()
        self.keepalive_account_group = str(
            config.get("keepalive_account_group") or self.account_group
        ).strip()
        self.account_group_color = str(config.get("account_group_color", "#238636")).strip()
        self.session = session or requests.Session()
        try:
            self.session.trust_env = False
        except Exception:
            pass
        self.access_token = ""
        self._traffic_recorder = None
        self._account_group_lock = threading.Lock()
        # Cached per group name: one client may provision both stage groups.
        self._account_groups = {}

    def group_name_for_stage(self, stage=""):
        """Return the configured HX-Email group for a workflow stage."""
        normalized = str(stage or "").strip().casefold()
        if normalized == "keepalive":
            return self.keepalive_account_group or self.account_group
        if normalized == "register":
            return self.register_account_group or self.account_group
        return self.account_group

    def set_traffic_recorder(self, recorder):
        self._traffic_recorder = recorder

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    def api_headers(self):
        if not self.api_key:
            return {}
        if ":" in self.api_key:
            name, value = self.api_key.split(":", 1)
            if name.strip() and value.strip():
                return {name.strip(): value.strip()}
        return {"X-API-Key": self.api_key}

    def _external_data(self, payload, action):
        if not payload.get("success"):
            raise HXEmailError(f"HX-Email {action}失败: {payload.get('message') or 'unknown error'}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise HXEmailError(f"HX-Email {action}响应缺少 data")
        return data

    def _require_config(self):
        if not self.base_url:
            raise HXEmailError("recovery_email.hx_email.base_url 不能为空")
        if not self.api_key and not (self.username and self.password):
            raise HXEmailError("请配置 HX-Email api_key 或 username/password")
