"""Build the proxy pool selected by ``proxy_source``.

Callers (batch registration, dashboard actions, the CLI entry point) share this
factory so a source switch never has to be repeated at each construction site.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from outlookregister.config.proxy_rotation_config import MANUAL_SOURCE, parse_proxy_source
from outlookregister.proxy.manual_proxy_pool import ManualProxyPool
from outlookregister.proxy.proxy_pool_types import ProxyRotationError
from outlookregister.proxy.proxy_rotation import RotatingProxyPool


def build_proxy_pool(
    config: dict[str, Any],
    required_pool_size: int = 0,
    config_path: Path | str | None = None,
):
    """Return the configured pool, or ``None`` when no proxy should be used.

    ``debug`` keeps its existing meaning: no pool at all, so the flow runs over
    the local network. Otherwise the source decides which implementation runs.
    """
    if config.get("debug"):
        return None

    try:
        source = parse_proxy_source(config.get("proxy_source"))
    except ValueError as exc:
        raise ProxyRotationError(str(exc)) from exc

    if source == MANUAL_SOURCE:
        if config_path is None:
            raise ProxyRotationError("手动代理列表模式需要 config.json 路径以记录消费")
        return ManualProxyPool(
            config,
            config_path=config_path,
            required_pool_size=required_pool_size,
        )

    proxy_rotation = dict(config.get("proxy_rotation") or {})
    if not proxy_rotation:
        return None
    auto_rotation = bool(
        str(
            proxy_rotation.get("control_url") or proxy_rotation.get("rotation_url") or ""
        ).strip()
    )
    if not (auto_rotation or proxy_rotation.get("enabled")):
        return None
    proxy_rotation["required_pool_size"] = max(0, int(required_pool_size or 0))
    return RotatingProxyPool(proxy_rotation)
