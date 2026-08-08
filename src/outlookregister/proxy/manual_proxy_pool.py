"""A proxy pool backed by an operator-pasted list of proxy endpoints.

Each configured line is handed out exactly once: acquiring a lease pops the
first pending entry and atomically moves it into ``manual_proxy_pool.used`` so
a restart never replays a burnt proxy. Identity still comes from a real exit-IP
probe, so the browser locale/timezone contract matches the residential pool.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import requests

from outlookregister.config.proxy_rotation_config import parse_manual_proxy_lines
from outlookregister.proxy.proxy_pool_http import _ProxyPoolHTTPHelpers
from outlookregister.proxy.proxy_pool_types import ProxyLease, ProxyRotationError
from outlookregister.proxy.proxy_pool_verify import _ProxyPoolVerify

_DEFAULT_EXIT_IP_ENDPOINT = "https://api.ipify.org?format=json"
_DEFAULT_IDENTITY_ENDPOINT = "https://ipwho.is/"

# One lock per config file so concurrent flows in this process never hand out
# the same line, and never interleave read-modify-write on the pending list.
_CONSUME_LOCKS: dict[str, threading.Lock] = {}
_CONSUME_LOCKS_GUARD = threading.Lock()


def _consume_lock(config_path: Path) -> threading.Lock:
    key = str(Path(config_path).resolve())
    with _CONSUME_LOCKS_GUARD:
        lock = _CONSUME_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _CONSUME_LOCKS[key] = lock
        return lock


class ManualProxyPool(_ProxyPoolHTTPHelpers, _ProxyPoolVerify):
    """Hand out operator-supplied proxies one line at a time."""

    def __init__(self, config, *, config_path, required_pool_size=0, pending_override=None):
        self._config_path = Path(config_path)
        self._store = None
        self.required_pool_size = int(required_pool_size or 0)
        # Set for dashboard checks so an operator can probe a pasted list before
        # it is saved. Consumption always goes through the persisted store.
        self._pending_override = (
            parse_manual_proxy_lines(pending_override)
            if pending_override is not None
            else None
        )

        proxy_rotation = dict(config.get("proxy_rotation") or {})
        self.timeout = self._positive_number(
            proxy_rotation.get("timeout_seconds"), 10.0
        )
        try:
            self.max_rotate_retries = max(
                0, int(proxy_rotation.get("max_rotate_retries", 2))
            )
        except (TypeError, ValueError):
            self.max_rotate_retries = 2
        self.exit_ip_endpoint = str(
            proxy_rotation.get("exit_ip_endpoint") or _DEFAULT_EXIT_IP_ENDPOINT
        )
        self.identity_endpoint = _DEFAULT_IDENTITY_ENDPOINT

        # Identity is probed from the exit IP and every line is unique to one
        # flow, so the pool offers the same guarantees as residential mode.
        self.auto_identity = True
        self.post_registration_route = "residential"
        self.session_scoped = True
        self.check_proxy = True
        self.require_country_echo = False
        self.verify_browser_exit_ip = bool(
            proxy_rotation.get("verify_browser_exit_ip", True)
        )
        self.enforce_unique_exit_ip = bool(
            proxy_rotation.get("enforce_unique_exit_ip", True)
        )

        self._session = requests.Session()
        self._session.trust_env = False
        self._request_lock = threading.Lock()
        self._active_exit_ips: set[str] = set()
        self._exit_ip_lock = threading.Lock()

        if not self._read_pending():
            raise ProxyRotationError("manual_proxy_pool.pending 至少需要一行可用代理")

    @staticmethod
    def _positive_number(value, default):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    def _config_store(self):
        if self._store is None:
            from outlookregister.config.config_store import ConfigStore

            self._store = ConfigStore(self._config_path)
        return self._store

    def _read_pending(self) -> list[str]:
        if self._pending_override is not None:
            return list(self._pending_override)
        pool = self._config_store().read().get("manual_proxy_pool") or {}
        return parse_manual_proxy_lines(pool.get("pending"))

    def _consume_next(self) -> str:
        """Pop the first pending proxy and persist it as used, atomically."""
        with _consume_lock(self._config_path):
            store = self._config_store()
            pool = store.read().get("manual_proxy_pool") or {}
            pending = parse_manual_proxy_lines(pool.get("pending"))
            used = parse_manual_proxy_lines(pool.get("used"))
            if not pending:
                raise ProxyRotationError(
                    "手动代理列表已用尽，请在配置页补充新的代理行"
                )
            proxy = pending.pop(0)
            if proxy not in used:
                used.append(proxy)
            store.update({"manual_proxy_pool": {"pending": pending, "used": used}})
            return proxy

    def acquire_proxy(self, country_code=None):
        """Consume pending lines until one probes successfully."""
        requested = str(country_code or "").strip().upper()
        attempts = self.max_rotate_retries + 1
        last_error = None
        for _attempt in range(attempts):
            proxy = self._consume_next()
            label = self._proxy_label(proxy)
            try:
                # A plain user:pass proxy does not answer 407 to bad
                # credentials, so the Listener credential probe cannot apply.
                identity = self._verify_exit_identity(
                    proxy,
                    requested,
                    verify_listener_credentials=False,
                )
            except ProxyRotationError as exc:
                last_error = f"{label}: {exc}"
                print(f"[ManualProxy] 代理不可用，已跳过 - {last_error}")
                continue
            exit_ip = identity["exit_ip"]
            if not self._reserve_exit_ip(exit_ip):
                last_error = f"{label}: 出口 IP {exit_ip} 已被其他窗口占用"
                print(f"[ManualProxy] 出口 IP 重复，已跳过 - {last_error}")
                continue
            print(
                "[ManualProxy] 已分配手动代理 - "
                f"{label}, exit_ip={exit_ip}, country={identity['country_code']}"
            )
            return ProxyLease(
                proxy=proxy,
                token="manual",
                session_id=f"manual-{exit_ip}",
                session_scoped=True,
                exit_ip=exit_ip,
                country_code=identity["country_code"],
                browser_locale=identity["browser_locale"],
                timezone=identity["timezone"],
            )
        raise ProxyRotationError(
            f"手动代理列表连续 {attempts} 行均不可用: {last_error or '未知错误'}"
        )

    def _reserve_exit_ip(self, exit_ip):
        if not self.enforce_unique_exit_ip or not exit_ip:
            return True
        with self._exit_ip_lock:
            if exit_ip in self._active_exit_ips:
                return False
            self._active_exit_ips.add(exit_ip)
            return True

    def release(self, lease):
        """Free the exit-IP reservation; the line itself stays consumed."""
        exit_ip = str(getattr(lease, "exit_ip", "") or "").strip()
        if not exit_ip:
            return
        with self._exit_ip_lock:
            self._active_exit_ips.discard(exit_ip)

    def identity_profile_for_lease(self, lease):
        """Return the browser identity confirmed for a manual lease."""
        if not lease or not str(getattr(lease, "country_code", "")).strip():
            raise ProxyRotationError("手动代理会话没有可确认的国家代码")
        return {
            "country_code": str(lease.country_code).strip().upper(),
            "browser_locale": str(getattr(lease, "browser_locale", "") or "en-US"),
            "timezone": str(getattr(lease, "timezone", "") or "UTC"),
        }

    def verify_browser_page(self, page, lease):
        """Verify that a browser page uses the same exit IP as its lease."""
        if not self.verify_browser_exit_ip or not lease or not lease.exit_ip:
            return
        try:
            page.goto(
                self.exit_ip_endpoint,
                timeout=int(self.timeout * 1000),
                wait_until="domcontentloaded",
            )
            body = page.locator("body").inner_text(timeout=5000).strip()
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            browser_ip = self._parse_exit_ip(payload, body)
        except Exception as exc:
            raise ProxyRotationError(f"浏览器出口 IP 验证失败: {exc}") from exc
        if browser_ip != lease.exit_ip:
            raise ProxyRotationError(
                "浏览器出口 IP 与 lease 不一致: "
                f"expected={lease.exit_ip}, actual={browser_ip}"
            )
        print(
            "[ManualProxy] 浏览器出口 IP 验证通过 - "
            f"session_id={lease.session_id}, exit_ip={browser_ip}"
        )

    def switch_after_registration(self, lease):
        """No control plane to rotate; the next flow simply takes the next line."""
        return lease

    def check_connection(self):
        """Probe the first pending line without consuming it."""
        pending = self._read_pending()
        if not pending:
            raise ProxyRotationError("manual_proxy_pool.pending 至少需要一行可用代理")
        identity = self._verify_exit_identity(
            pending[0],
            "",
            verify_listener_credentials=False,
        )
        return {"proxy": self._proxy_label(pending[0]), "pending": len(pending), **identity}

    def close(self):
        try:
            self._session.close()
        except Exception:
            pass
