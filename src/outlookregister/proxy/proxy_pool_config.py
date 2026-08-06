from __future__ import annotations

import ipaddress
import json
import threading
import time
import uuid
from dataclasses import replace
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from src.outlookregister.proxy.managed_mihomo import ManagedMihomo, ManagedMihomoError, SUPPORTED_PROTOCOLS
from src.outlookregister.proxy.proxy_pool_types import ProxyLease, ProxyRotationError, _declared_lease_state
from src.outlookregister.config.proxy_rotation_config import (
    parse_control_plane_url,
    parse_remote_control_plane_url,
    parse_remote_residential_control_url,
    validate_proxy_endpoint,
    validate_remote_proxy_endpoint,
    validate_rotation_token,
)
from src.outlookregister.config.identity_profiles import (
    is_valid_country_code,
    is_valid_timezone,
    select_identity_profile,
)


class _ProxyPoolConfig:
    def __init__(self, config):
        control_url = str(config.get("control_url") or "").strip()
        legacy_rotation_url = str(config.get("rotation_url") or "").strip()
        if control_url and legacy_rotation_url:
            raise ProxyRotationError(
                "proxy_rotation.control_url 与 rotation_url 不能同时配置"
            )
        self.declared_pool = bool(control_url)
        self.auto_identity = self.declared_pool or bool(legacy_rotation_url)
        control_plane_value = (
            legacy_rotation_url if legacy_rotation_url else config.get("base_url", "")
        )
        try:
            if self.declared_pool:
                control_plane = parse_remote_residential_control_url(control_url)
            elif self.auto_identity:
                control_plane = parse_remote_control_plane_url(control_plane_value)
            else:
                control_plane = parse_control_plane_url(control_plane_value)
        except ValueError as exc:
            raise ProxyRotationError(str(exc)) from exc
        if self.auto_identity and not control_plane.embedded_token:
            raise ProxyRotationError(
                "HX-ProxyGroup 住宅控制 URL 必须包含访问 token"
            )
        self.base_url = control_plane.origin
        self.control_path = (
            f"/ctl/{control_plane.embedded_token}" if self.declared_pool else ""
        )
        try:
            self.timeout = float(config.get("timeout_seconds", 10))
            self.max_rotate_retries = int(config.get("max_rotate_retries", 2))
            required_pool_size = int(config.get("required_pool_size", 0))
        except (TypeError, ValueError) as exc:
            raise ProxyRotationError("proxy_rotation 的数值配置无效") from exc
        if self.timeout <= 0 or self.max_rotate_retries < 0:
            raise ProxyRotationError("proxy_rotation 的数值配置无效")
        self.session_scoped = bool(config.get("session_scoped", True))
        self.post_registration_route = str(
            config.get("post_registration_route", "residential")
        ).strip().lower()
        self.check_proxy = bool(config.get("check_proxy", False))
        self.exit_ip_endpoint = str(
            config.get("exit_ip_endpoint", "https://api.ipify.org?format=json")
        )
        self.identity_endpoint = "https://ipwho.is/"
        self.verify_browser_exit_ip = bool(config.get("verify_browser_exit_ip", True))
        self.require_country_echo = bool(config.get("require_country_echo", False))
        self.country_code = str(config.get("country_code", "")).strip()
        self.required_pool_size = max(required_pool_size, 0)
        self._enforce_unique_exit_ip = bool(
            config.get("enforce_unique_exit_ip", self.check_proxy)
        )

        if self.declared_pool:
            self.listener = ""
            self.session_scoped = True
            self.post_registration_route = "residential"
            self.check_proxy = True
            self.verify_browser_exit_ip = True
            self.require_country_echo = True
            self._enforce_unique_exit_ip = True
            raw_entries = []
        elif self.auto_identity:
            # A pasted rotation URL is a complete, self-contained deployment.
            # These flags are intentionally fixed so a UI or stale config cannot
            # turn the browser flow into a direct or shared-proxy request.
            self.listener = ""
            self.session_scoped = True
            self.post_registration_route = "residential"
            self.check_proxy = True
            self.verify_browser_exit_ip = True
            self.require_country_echo = True
            self._enforce_unique_exit_ip = True
            raw_entries = [{
                "token": control_plane.embedded_token,
                "proxy": "",
                "country_code": "",
            }]
        else:
            self.listener = ""
            raw_entries = config.get("tokens", []) or []
        if not isinstance(raw_entries, list):
            raise ProxyRotationError("proxy_rotation.tokens 必须是数组")

        self.entries = []
        for index, entry in enumerate(raw_entries):
            if not isinstance(entry, dict):
                raise ProxyRotationError(
                    f"proxy_rotation.tokens[{index}] 必须是对象"
                )
            token = str(entry.get("token", "")).strip()
            proxy = str(entry.get("proxy", "")).strip()
            if not token and not proxy:
                continue
            try:
                token = validate_rotation_token(token)
                if proxy:
                    proxy = validate_proxy_endpoint(proxy)
                elif not self.auto_identity:
                    raise ValueError("代理入口不能为空")
            except ValueError as exc:
                raise ProxyRotationError(
                    f"proxy_rotation.tokens[{index}] 配置无效: {exc}"
                ) from exc
            entry_country = str(entry.get("country_code", "")).strip()
            if self.country_code and entry_country and entry_country.casefold() != self.country_code.casefold():
                raise ProxyRotationError(
                    "proxy_rotation.tokens 中的 country_code 必须与全局 country_code 一致"
                )
            self.entries.append({
                "token": token,
                "proxy": proxy,
                "country_code": entry_country,
            })

        if not self.entries and not self.declared_pool:
            raise ProxyRotationError("proxy_rotation.tokens 至少需要配置一个 {token, proxy} 渠道")
        if control_plane.embedded_token and not self.auto_identity:
            if len(self.entries) != 1 or self.entries[0]["token"] != control_plane.embedded_token:
                raise ProxyRotationError(
                    "完整 /rot/<token> URL 中的 token 必须与唯一渠道 token 一致"
                )
        if self.post_registration_route not in ("residential", "direct", "upstream"):
            raise ProxyRotationError(
                "proxy_rotation.post_registration_route 只支持 residential、direct 或 upstream"
            )

        self._session = requests.Session()
        self._session.trust_env = False
        self._lock = threading.Lock()
        self._allocation_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._declared_state = (
            _declared_lease_state(f"{self.base_url}{self.control_path}")
            if self.declared_pool
            else None
        )
        self._active_exit_ips: dict[str, tuple[str, str]] = (
            self._declared_state.active_exit_ips if self._declared_state else {}
        )
        self._leased_node_indexes: set[int] = (
            self._declared_state.leased_node_indexes if self._declared_state else set()
        )
        self._control_nodes_loaded = False
        self._next_index = 0
        self._local_data_plane = ManagedMihomo() if self.declared_pool else None

        if self.enforce_unique_exit_ip and not self.check_proxy:
            raise ProxyRotationError(
                "enforce_unique_exit_ip=true 时必须同时启用 check_proxy"
            )
        if self.enforce_unique_exit_ip and not self.session_scoped:
            raise ProxyRotationError(
                "enforce_unique_exit_ip=true 时必须同时启用 session_scoped"
            )
