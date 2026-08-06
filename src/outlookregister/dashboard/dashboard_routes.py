"""HTTP 路由处理器（注册到 dashboard_app.app）。"""
from __future__ import annotations

import json
import asyncio
from queue import Empty
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src.outlookregister import PROJECT_ROOT
from src.outlookregister.config.config_store import ConfigError
from src.outlookregister.proxy.proxy_rotation import ProxyRotationError, RotatingProxyPool
from src.outlookregister.dashboard.dashboard_actions import DashboardActionError
from src.outlookregister.dashboard.workflow_runner import WorkflowError
from src.outlookregister.dashboard.dashboard_app import app, ACTION_RUNNER, WORKFLOW_RUNNER, CONFIG_STORE
from src.outlookregister.dashboard.dashboard_constants import RESULTS_DIR
from src.outlookregister.dashboard.dashboard_serializers import _automatic_proxy_config, _interactive_proxy_config
from src.outlookregister.dashboard.dashboard_store import DashboardStore


@app.get("/api/dashboard")
def get_dashboard() -> dict[str, Any]:
    return DashboardStore().snapshot()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "results_dir": str(RESULTS_DIR),
        "config_revision": CONFIG_STORE.revision(),
        "config_runtime_validation_errors": CONFIG_STORE.public().get(
            "runtime_validation_errors", []
        ),
    }


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    try:
        return CONFIG_STORE.public()
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.put("/api/config")
def update_config(payload: dict[str, Any]) -> dict[str, Any]:
    patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else payload
    try:
        return CONFIG_STORE.update(patch)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/proxy-rotation/check")
def check_proxy_rotation(payload: dict[str, Any]) -> dict[str, Any]:
    """Verify one pasted HX URL, then persist the safe automatic-mode defaults."""
    control_url = str(
        payload.get("control_url") or payload.get("url") or ""
    ).strip()
    if not control_url:
        raise HTTPException(status_code=422, detail="HX-ProxyGroup 住宅控制 URL 不能为空")

    try:
        current_config = CONFIG_STORE.read()
        proxy_config = _automatic_proxy_config(control_url, current_config)
        pool = RotatingProxyPool(_interactive_proxy_config(proxy_config))
        verification = pool.check_connection()
        updated = CONFIG_STORE.update({
            "proxy": "",
            "strict_isolation": True,
            "isolate_hx_email_group": True,
            "prevent_direct_network_leaks": True,
            "identity": {
                "country_selection": "proxy",
                "country_code": "",
                "browser_locale": "",
                "timezone": "",
                "require_dynamic_residential_ip": True,
            },
            "proxy_rotation": {
                "enabled": True,
                "control_url": control_url,
                "rotation_url": "",
                "base_url": "",
                "listener": "",
                "timeout_seconds": proxy_config["timeout_seconds"],
                "max_rotate_retries": proxy_config["max_rotate_retries"],
                "session_scoped": True,
                "post_registration_route": "residential",
                "check_proxy": True,
                "enforce_unique_exit_ip": True,
                "verify_browser_exit_ip": True,
                "require_country_echo": True,
                "exit_ip_endpoint": "https://api.ipify.org?format=json",
                "tokens": [],
            },
        })
    except (ConfigError, ProxyRotationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "ok": True,
        "exit_ip": verification["exit_ip"],
        "country_code": verification["country_code"],
        "browser_locale": verification["browser_locale"],
        "timezone": verification["timezone"],
        "config": updated,
    }


@app.get("/api/config/stream")
async def config_stream(request: Request):
    async def events():
        previous = ""
        while not await request.is_disconnected():
            revision = CONFIG_STORE.revision()
            if revision != previous:
                previous = revision
                yield f"event: config\ndata: {json.dumps({'revision': revision})}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/account-actions")
def get_account_actions() -> dict[str, Any]:
    return {"accounts": ACTION_RUNNER.snapshot()}


@app.get("/api/account-actions/stream")
async def account_actions_stream(request: Request):
    subscriber = ACTION_RUNNER.subscribe()

    async def events():
        try:
            yield f"event: account-snapshot\ndata: {json.dumps({'accounts': ACTION_RUNNER.snapshot()}, ensure_ascii=False)}\n\n"
            while not await request.is_disconnected():
                try:
                    payload = await asyncio.to_thread(subscriber.get, True, 15)
                except Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: account-action\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            ACTION_RUNNER.unsubscribe(subscriber)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/workflows")
def get_workflows() -> dict[str, Any]:
    return {"jobs": WORKFLOW_RUNNER.snapshot()}


@app.post("/api/workflows/register", status_code=202)
def submit_registration_workflow(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        state = WORKFLOW_RUNNER.submit_registration(
            payload.get("count", 1),
            payload.get("concurrency"),
        )
    except WorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"job": state}


@app.post("/api/workflows/keepalive", status_code=202)
def submit_keepalive_workflow(payload: dict[str, Any]) -> dict[str, Any]:
    requested = payload.get("emails") or []
    if not isinstance(requested, list):
        raise HTTPException(status_code=422, detail="emails 必须是数组")
    if requested:
        emails = [str(email).strip() for email in requested if str(email).strip()]
    else:
        emails = [
            account["email"]
            for account in DashboardStore().snapshot().get("accounts", [])
            if account.get("stages", {}).get("registered", {}).get("ok")
        ]
    states = []
    auth_mode = str(payload.get("auth_mode") or "password").strip().casefold()
    for email in emails[:1000]:
        try:
            states.append(ACTION_RUNNER.submit(email, "keepalive", {"auth_mode": auth_mode}))
        except DashboardActionError as exc:
            states.append({"email": email, "status": "failed", "message": str(exc)})
    return {"states": states, "submitted": len(states)}


@app.post("/api/accounts/{email}/actions/{action}", status_code=202)
def run_account_action(
    email: str,
    action: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_action = action.replace("-", "_").strip().casefold()
    try:
        state = ACTION_RUNNER.submit(email, normalized_action, payload or {})
    except DashboardActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"action": state}


@app.post("/api/accounts/{email}/actions/{action}/resume", status_code=202)
def resume_account_action(
    email: str,
    action: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_action = action.replace("-", "_").strip().casefold()
    try:
        state = ACTION_RUNNER.resume(
            email,
            normalized_action,
            str((payload or {}).get("step") or "").strip() or None,
        )
    except DashboardActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"action": state}


@app.post("/api/accounts/{email}/actions/{action}/pause", status_code=202)
def pause_account_action(
    email: str,
    action: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_action = action.replace("-", "_").strip().casefold()
    try:
        state = ACTION_RUNNER.pause(
            email,
            normalized_action,
            str((payload or {}).get("step") or "").strip() or None,
        )
    except DashboardActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"action": state}


@app.get("/", include_in_schema=False)
def get_index():
    index_path = PROJECT_ROOT / "dashboard" / "dist" / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse(
        "<h1>Dashboard frontend is not built</h1>"
        "<p>Run <code>npm install && npm run build</code> in dashboard/.</p>",
        status_code=503,
    )

dashboard_dist = PROJECT_ROOT / "dashboard" / "dist"
if (dashboard_dist / "assets").exists():
    app.mount(
        "/assets",
        StaticFiles(directory=dashboard_dist / "assets"),
        name="dashboard-assets",
    )
