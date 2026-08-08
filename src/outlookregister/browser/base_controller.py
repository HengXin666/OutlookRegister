"""Common browser controller contract and proxy URL normalization."""

from __future__ import annotations

from abc import ABC, abstractmethod
from urllib.parse import unquote, urlsplit, urlunsplit

from outlookregister.browser.base_controller_config import _BaseControllerConfig
from outlookregister.browser.base_controller_interaction import (
    _BaseControllerInteraction,
)
from outlookregister.browser.base_controller_recovery import _BaseRecovery
from outlookregister.browser.base_controller_recovery_challenge import (
    _BaseRecoveryChallenge,
)
from outlookregister.browser.base_controller_registration import _BaseRegistration
from outlookregister.browser.base_controller_resources import _BaseControllerResources
from outlookregister.browser.base_controller_unlock import _BaseUnlockChallenge
from outlookregister.dashboard.traffic_tracker import TrafficRecorder  # noqa: F401
from outlookregister.email.hx_email_client import HXEmailClient  # noqa: F401


def build_browser_proxy_settings(proxy: str | None) -> dict[str, str] | None:
    """Convert a proxy URL into Playwright server and credential fields."""
    normalized = str(proxy or "").strip()
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if not parsed.scheme or not parsed.hostname:
        return {"server": normalized, "bypass": "localhost,127.0.0.1,[::1]"}
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    settings: dict[str, str] = {
        "server": urlunsplit(
            (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
        ),
        "bypass": "localhost,127.0.0.1,[::1]",
    }
    if parsed.username is not None:
        settings["username"] = unquote(parsed.username)
    if parsed.password is not None:
        settings["password"] = unquote(parsed.password)
    return settings


class BaseBrowserController(
    _BaseControllerConfig,
    _BaseControllerInteraction,
    _BaseControllerResources,
    _BaseRecovery,
    _BaseRecoveryChallenge,
    _BaseRegistration,
    _BaseUnlockChallenge,
    ABC,
):
    """Shared browser behavior used by Playwright and Patchright adapters."""

    @abstractmethod
    def launch_browser(self, proxy: str | None = None, playwright=None):
        """Return the runtime and browser created for a flow."""

    @abstractmethod
    def handle_captcha(self, page):
        """Handle the site captcha for the concrete browser adapter."""

    @abstractmethod
    def clean_up(self, page=None, type="all_browser"):
        """Release one page or all resources created by the adapter."""

    @abstractmethod
    def get_thread_page(self):
        """Return the page owned by the current worker thread."""
