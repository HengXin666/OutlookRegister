"""手动代理列表相关路由（注册到 dashboard_app.app）。

与住宅控制 URL 的校验路由分开，便于各自演进；两者都只写入本机 config.json。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from outlookregister.config.config_store import ConfigError
from outlookregister.config.proxy_rotation_config import (
    MANUAL_SOURCE,
    parse_manual_proxy_lines,
)
from outlookregister.dashboard.dashboard_app import CONFIG_STORE, app
from outlookregister.proxy.manual_proxy_pool import ManualProxyPool
from outlookregister.proxy.proxy_rotation import ProxyRotationError


@app.post("/api/manual-proxy/check")
def check_manual_proxy(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a pasted proxy list, probe its first line, then enable the source."""
    try:
        pending = parse_manual_proxy_lines(
            payload.get("pending") or payload.get("proxies")
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not pending:
        raise HTTPException(status_code=422, detail="请至少填写一行代理")

    try:
        current_config = CONFIG_STORE.read()
        probe_config = dict(current_config)
        probe_config["manual_proxy_pool"] = {"pending": pending, "used": []}
        # Probe the pasted list before it is saved; check_connection only reads
        # the override, so nothing is consumed and re-checks are free.
        pool = ManualProxyPool(
            probe_config,
            config_path=CONFIG_STORE.path,
            pending_override=pending,
        )
        try:
            verification = pool.check_connection()
        finally:
            pool.close()
        used = parse_manual_proxy_lines(
            (current_config.get("manual_proxy_pool") or {}).get("used")
        )
        updated = CONFIG_STORE.update({
            "proxy": "",
            "proxy_source": MANUAL_SOURCE,
            "prevent_direct_network_leaks": True,
            "identity": {
                "country_selection": "proxy",
                "country_code": "",
                "browser_locale": "",
                "timezone": "",
            },
            "manual_proxy_pool": {"pending": pending, "used": used},
        })
    except (ConfigError, ProxyRotationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "ok": True,
        "pending": len(pending),
        "exit_ip": verification["exit_ip"],
        "country_code": verification["country_code"],
        "browser_locale": verification["browser_locale"],
        "timezone": verification["timezone"],
        "config": updated,
    }


@app.post("/api/manual-proxy/recycle")
def recycle_manual_proxy(payload: dict[str, Any]) -> dict[str, Any]:
    """Restore used proxy lines back to pending, or clear the recycle bin."""
    action = str(payload.get("action") or "").strip().casefold()
    if action not in {"restore", "clear"}:
        raise HTTPException(status_code=422, detail="action 只能是 restore 或 clear")
    try:
        pool = CONFIG_STORE.read().get("manual_proxy_pool") or {}
        pending = parse_manual_proxy_lines(pool.get("pending"))
        used = parse_manual_proxy_lines(pool.get("used"))
        if action == "restore":
            pending = pending + [line for line in used if line not in pending]
        updated = CONFIG_STORE.update({
            "manual_proxy_pool": {"pending": pending, "used": []}
        })
    except (ConfigError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ok": True,
        "restored": len(used) if action == "restore" else 0,
        "cleared": len(used) if action == "clear" else 0,
        "config": updated,
    }
