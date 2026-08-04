"""FastAPI data service and static host for the Outlook registration dashboard."""

from __future__ import annotations

import json
import asyncio
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from dashboard_actions import DashboardActionError, DashboardActionRunner
from config_store import ConfigError, ConfigStore
from proxy_rotation import ProxyRotationError, RotatingProxyPool
from workflow_runner import WorkflowError, WorkflowRunner


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = PROJECT_ROOT / "Results"
RESULTS_DIR = Path(os.getenv("OUTLOOK_RESULTS_DIR", str(DEFAULT_RESULTS_DIR))).expanduser()
CONFIG_STORE = ConfigStore(PROJECT_ROOT / "config.json")
CHECKPOINTS_FILE = "account_checkpoints.jsonl"
RECOVERY_FILE = "recovery_email_status.jsonl"
TRAFFIC_FILE = "traffic_usage.jsonl"

STAGE_DEFINITIONS = (
    ("registered", "已注册"),
    ("recovery_bound", "已绑定密保邮箱"),
    ("oauth_authorized", "已完成 OAuth 授权"),
    ("hx_email_imported", "已加入 HX-Email"),
)
STAGE_LABELS = dict(STAGE_DEFINITIONS)
TRAFFIC_STAGE_LABELS = {
    "residential_registration": "住宅 IP / 注册",
    "post_registration": "注册后邮箱初始化",
    "recovery_email": "密保邮箱验证",
    "oauth_browser": "OAuth 浏览器",
    "oauth_token_exchange": "OAuth Token 交换",
    "hx_email_import": "HX-Email 账号导入",
    "hx_email_api": "HX-Email API",
    "proxy_control": "代理控制面",
    "unknown": "未分类",
}
REGISTERED_EVIDENCE = {
    "registered",
    "registration_flow_completed",
    "post_registration_failed",
    "recovery_failed",
    "oauth_launch_failed",
    "oauth_failed",
    "oauth_success",
    "hx_email_import_failed",
    "hx_email_imported",
}
FAILURE_STAGES = {
    "navigation_failed",
    "registration_rejected",
    "captcha_unsupported",
    "registration_unconfirmed",
    "recovery_failed",
    "post_registration_failed",
    "oauth_launch_failed",
    "oauth_failed",
    "oauth_token_invalid",
    "hx_email_import_failed",
}


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_value(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    records: list[dict[str, Any]] = []
    invalid_lines = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines += 1
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            invalid_lines += 1
    return records, invalid_lines


def _email_from(record: dict[str, Any]) -> str:
    return str(record.get("outlook_email") or record.get("email") or "").strip()


def _email_key(email: str) -> str:
    return email.casefold().strip()


def _number(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(parsed, 0.0)


def _round_seconds(value: float | None) -> float | None:
    if value is None:
        return None
    return round(max(value, 0.0), 1)


def _human_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    if value < 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    return f"{value / (1024 * 1024 * 1024):.2f} GB"


def _traffic_stage_label(stage: str) -> str:
    return TRAFFIC_STAGE_LABELS.get(stage, stage.replace("_", " ").title())


def _automatic_proxy_config(
    control_url: str,
    current_config: dict[str, Any],
) -> dict[str, Any]:
    """Build the internal proxy config from the one user-facing URL."""
    current_rotation = current_config.get("proxy_rotation") or {}
    if not isinstance(current_rotation, dict):
        current_rotation = {}
    return {
        "control_url": control_url,
        "timeout_seconds": current_rotation.get("timeout_seconds", 10),
        "max_rotate_retries": current_rotation.get("max_rotate_retries", 2),
        "required_pool_size": 0,
    }


_SENSITIVE_DETAIL_PATTERN = re.compile(
    r"(?i)\b(password|passwd|refresh[-_ ]?token|access[-_ ]?token|id[-_ ]?token|"
    r"api[-_ ]?key|client[-_ ]?secret|authorization)\b"
    r"(?:\s*[:=]\s*|\s*[-_/]\s*|\s+)[^\s,;|]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_PATTERN = re.compile(
    r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"
)
_QUERY_SECRET_PATTERN = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|id_token|token|api_key|secret)=)"
    r"[^&#\s]+"
)


def _sanitize_detail(value: Any) -> str:
    """Keep event context useful without exposing credential-like values."""
    detail = str(value or "")
    if not detail:
        return ""
    detail = _BEARER_PATTERN.sub("Bearer [redacted]", detail)
    detail = _JWT_PATTERN.sub("[redacted]", detail)
    detail = _QUERY_SECRET_PATTERN.sub(r"\1[redacted]", detail)
    return _SENSITIVE_DETAIL_PATTERN.sub(
        lambda match: f"{match.group(1)}=[redacted]", detail
    )


def _account_record(email: str) -> dict[str, Any]:
    return {
        "email": email,
        "events": [],
        "recovery_events": [],
        "identity_countries": [],
        "first_seen": None,
        "last_seen": None,
        "stage_timestamps": {},
        "traffic_events": [],
    }


def _add_account(accounts: dict[str, dict[str, Any]], email: str) -> dict[str, Any]:
    key = _email_key(email)
    if not key:
        return _account_record("")
    if key not in accounts:
        accounts[key] = _account_record(email)
    return accounts[key]


def _remember_identity_country(
    account: dict[str, Any],
    record: dict[str, Any],
) -> None:
    country = str(
        record.get("identity_country_code")
        or record.get("proxy_country_code")
        or ""
    ).strip()
    if country and country not in account["identity_countries"]:
        account["identity_countries"].append(country)


def _event_time(record: dict[str, Any], index: int) -> tuple[datetime | None, int]:
    return _parse_timestamp(record.get("timestamp")), index


def _time_delta(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _milestone_durations(milestones: dict[str, datetime | None]) -> dict[str, float | None]:
    registered = milestones.get("registered")
    recovery = milestones.get("recovery_bound")
    oauth = milestones.get("oauth_authorized")
    hx_imported = milestones.get("hx_email_imported")
    return {
        "registration": _round_seconds(
            _time_delta(milestones.get("generated"), registered)
        ),
        "recovery": _round_seconds(_time_delta(registered, recovery)),
        "oauth": _round_seconds(_time_delta(recovery or registered, oauth)),
        "hx_email": _round_seconds(_time_delta(oauth, hx_imported)),
    }


def _build_traffic(records: list[dict[str, Any]], accounts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_stage: defaultdict[str, int] = defaultdict(int)
    by_source: defaultdict[str, int] = defaultdict(int)
    by_account: defaultdict[str, int] = defaultdict(int)
    by_account_stage: defaultdict[tuple[str, str], int] = defaultdict(int)
    estimated_by_stage: defaultdict[str, bool] = defaultdict(bool)
    valid_records = 0

    for index, record in enumerate(records):
        email = _email_from(record)
        stage = str(record.get("stage") or "unknown").strip() or "unknown"
        source = str(record.get("source") or stage).strip() or stage
        byte_count = _number(record.get("bytes"))
        if byte_count <= 0:
            byte_count = _number(record.get("bytes_sent")) + _number(
                record.get("bytes_received")
            )
        if byte_count <= 0:
            byte_count = _number(record.get("encoded_data_length"))
        if byte_count <= 0:
            continue
        byte_count_int = int(round(byte_count))
        valid_records += 1
        by_stage[stage] += byte_count_int
        by_source[source] += byte_count_int
        key = _email_key(email)
        if key:
            by_account[key] += byte_count_int
            by_account_stage[(key, stage)] += byte_count_int
            account = _add_account(accounts, email)
            _remember_identity_country(account, record)
            account["traffic_events"].append(
                {
                    "index": index,
                    "stage": stage,
                    "source": source,
                    "bytes": byte_count_int,
                    "estimated": bool(record.get("estimated", False)),
                    "timestamp": record.get("timestamp"),
                }
            )
        estimated_by_stage[stage] = estimated_by_stage[stage] or bool(
            record.get("estimated", False)
        )

    def metric_items(values: dict[str, int]) -> list[dict[str, Any]]:
        return [
            {
                "key": key,
                "label": _traffic_stage_label(key),
                "bytes": value,
                "human": _human_bytes(value),
                "estimated": estimated_by_stage.get(key, False),
            }
            for key, value in sorted(values.items(), key=lambda item: item[1], reverse=True)
        ]

    for key, account in accounts.items():
        account_stage_values: dict[str, int] = {}
        for (account_key, stage), value in by_account_stage.items():
            if account_key == key:
                account_stage_values[stage] = value
        account["traffic"] = {
            "available": bool(account_stage_values),
            "total_bytes": by_account.get(key, 0),
            "human": _human_bytes(by_account.get(key, 0)),
            "by_stage": metric_items(account_stage_values),
        }

    total_bytes = sum(by_stage.values())
    return {
        "available": valid_records > 0,
        "file": TRAFFIC_FILE,
        "sample_count": valid_records,
        "total_bytes": total_bytes,
        "human": _human_bytes(total_bytes),
        "by_stage": metric_items(dict(by_stage)),
        "by_source": [
            {
                "key": key,
                "label": _traffic_stage_label(key),
                "bytes": value,
                "human": _human_bytes(value),
            }
            for key, value in sorted(by_source.items(), key=lambda item: item[1], reverse=True)
        ],
        "note": (
            "只统计已观测到的网络字节；浏览器上行请求头等部分可能未包含。"
            if valid_records
            else "历史检查点没有流量记录，后续运行开始后才会显示。"
        ),
    }


class DashboardStore:
    def __init__(self, results_dir: Path | str = RESULTS_DIR):
        self.results_dir = Path(results_dir)

    def snapshot(self) -> dict[str, Any]:
        checkpoints, invalid_checkpoints = _read_jsonl(
            self.results_dir / CHECKPOINTS_FILE
        )
        recovery_records, invalid_recovery = _read_jsonl(
            self.results_dir / RECOVERY_FILE
        )
        traffic_records, invalid_traffic = _read_jsonl(
            self.results_dir / TRAFFIC_FILE
        )
        accounts: dict[str, dict[str, Any]] = {}

        for index, record in enumerate(checkpoints):
            email = _email_from(record)
            if not email:
                continue
            account = _add_account(accounts, email)
            _remember_identity_country(account, record)
            timestamp = _parse_timestamp(record.get("timestamp"))
            stage = str(record.get("stage") or "unknown").strip() or "unknown"
            account["events"].append(
                {
                    "index": index,
                    "stage": stage,
                    "detail": _sanitize_detail(record.get("detail")),
                    "timestamp": _timestamp_value(timestamp),
                    "_timestamp": timestamp,
                }
            )

        for index, record in enumerate(recovery_records):
            email = _email_from(record)
            if not email:
                continue
            account = _add_account(accounts, email)
            _remember_identity_country(account, record)
            timestamp = _parse_timestamp(record.get("timestamp"))
            account["recovery_events"].append(
                {
                    "index": index,
                    "bound": bool(record.get("bound", False)),
                    "recovery_email": str(record.get("recovery_email") or ""),
                    "reason": str(record.get("reason") or ""),
                    "detail": _sanitize_detail(record.get("detail")),
                    "timestamp": _timestamp_value(timestamp),
                    "_timestamp": timestamp,
                }
            )

        traffic = _build_traffic(traffic_records, accounts)
        account_rows = [self._finalize_account(account) for account in accounts.values()]
        account_rows.sort(
            key=lambda account: (
                account.get("first_seen") is None,
                account.get("first_seen") or "",
                account["email"].casefold(),
            ),
            reverse=True,
        )

        averages = self._averages(account_rows)
        counts = {
            stage: sum(1 for account in account_rows if account["stages"][stage]["ok"])
            for stage, _label in STAGE_DEFINITIONS
        }
        latest = [
            account.get("last_seen")
            for account in account_rows
            if account.get("last_seen")
        ]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "results_dir": str(self.results_dir),
                "checkpoints": CHECKPOINTS_FILE,
                "recovery": RECOVERY_FILE,
                "traffic": TRAFFIC_FILE,
                "invalid_lines": {
                    "checkpoints": invalid_checkpoints,
                    "recovery": invalid_recovery,
                    "traffic": invalid_traffic,
                },
            },
            "summary": {
                "total": len(account_rows),
                "registered": counts["registered"],
                "recovery_bound": counts["recovery_bound"],
                "oauth_authorized": counts["oauth_authorized"],
                "hx_email_imported": counts["hx_email_imported"],
                "fully_complete": sum(
                    1
                    for account in account_rows
                    if all(item["ok"] for item in account["stages"].values())
                ),
                "average_duration_seconds": averages["total"]["average_seconds"],
                "average_duration_human": self._duration_label(
                    averages["total"]["average_seconds"]
                ),
                "last_seen": max(latest) if latest else None,
            },
            "stages": [
                {
                    "key": stage,
                    "label": label,
                    "completed": counts[stage],
                    "total": len(account_rows),
                    "average_seconds": averages[stage]["average_seconds"],
                    "average_human": self._duration_label(
                        averages[stage]["average_seconds"]
                    ),
                    "samples": averages[stage]["samples"],
                }
                for stage, label in STAGE_DEFINITIONS
            ],
            "duration_averages": averages,
            "traffic": traffic,
            "accounts": account_rows,
        }

    @staticmethod
    def _duration_label(seconds: float | None) -> str | None:
        if seconds is None:
            return None
        seconds = int(round(seconds))
        if seconds < 60:
            return f"{seconds} 秒"
        minutes, remainder = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes} 分 {remainder:02d} 秒"
        hours, minutes = divmod(minutes, 60)
        return f"{hours} 小时 {minutes:02d} 分"

    def _finalize_account(self, account: dict[str, Any]) -> dict[str, Any]:
        events = sorted(
            account["events"],
            key=lambda item: (item["_timestamp"] is None, item["_timestamp"] or datetime.min.replace(tzinfo=timezone.utc), item["index"]),
        )
        recovery_events = sorted(
            account["recovery_events"],
            key=lambda item: (item["_timestamp"] is None, item["_timestamp"] or datetime.min.replace(tzinfo=timezone.utc), item["index"]),
        )
        timestamp_values = [
            item["_timestamp"]
            for item in (*events, *recovery_events)
            if item["_timestamp"] is not None
        ]
        first_seen = min(timestamp_values) if timestamp_values else None
        last_seen = max(timestamp_values) if timestamp_values else None
        first_generated = next(
            (item["_timestamp"] for item in events if item["stage"] == "generated"),
            None,
        )
        registered_at = next(
            (
                item["_timestamp"]
                for item in events
                if item["stage"] in REGISTERED_EVIDENCE and item["_timestamp"]
            ),
            None,
        )
        latest_recovery = recovery_events[-1] if recovery_events else None
        recovery_bound = bool(latest_recovery and latest_recovery["bound"])
        recovery_at = (
            latest_recovery["_timestamp"]
            if recovery_bound and latest_recovery
            else None
        )
        oauth_at = next(
            (item["_timestamp"] for item in events if item["stage"] == "oauth_success"),
            None,
        )
        hx_at = next(
            (
                item["_timestamp"]
                for item in events
                if item["stage"] == "hx_email_imported"
            ),
            None,
        )
        stage_timestamps = {
            "generated": first_generated,
            "registered": registered_at,
            "recovery_bound": recovery_at,
            "oauth_authorized": oauth_at,
            "hx_email_imported": hx_at,
        }
        milestones = {
            key: value
            for key, value in stage_timestamps.items()
            if key in {"generated", "registered", "recovery_bound", "oauth_authorized", "hx_email_imported"}
        }
        durations = _milestone_durations(milestones)
        final_milestone = next(
            (
                value
                for value in (hx_at, oauth_at, recovery_at, registered_at, last_seen)
                if value is not None
            ),
            None,
        )
        total_duration = _round_seconds(_time_delta(first_generated or first_seen, final_milestone))
        latest_stage = events[-1]["stage"] if events else "pending"
        if hx_at:
            current_stage = "hx_email_imported"
        elif oauth_at:
            current_stage = "oauth_authorized"
        elif recovery_bound:
            current_stage = "recovery_bound"
        elif registered_at:
            current_stage = "registered"
        else:
            current_stage = latest_stage
        failed = latest_stage in FAILURE_STAGES
        stages = {
            "registered": {"ok": bool(registered_at), "at": _timestamp_value(registered_at)},
            "recovery_bound": {"ok": recovery_bound, "at": _timestamp_value(recovery_at)},
            "oauth_authorized": {"ok": bool(oauth_at), "at": _timestamp_value(oauth_at)},
            "hx_email_imported": {"ok": bool(hx_at), "at": _timestamp_value(hx_at)},
        }
        clean_events = [
            {key: value for key, value in event.items() if not key.startswith("_")}
            for event in events
        ]
        clean_recovery_events = [
            {key: value for key, value in event.items() if not key.startswith("_")}
            for event in recovery_events
        ]
        return {
            "email": account["email"],
            "identity_countries": list(account.get("identity_countries", [])),
            "status": "complete" if all(item["ok"] for item in stages.values()) else "failed" if failed else "incomplete",
            "current_stage": current_stage,
            "current_stage_label": STAGE_LABELS.get(current_stage, current_stage),
            "first_seen": _timestamp_value(first_seen),
            "last_seen": _timestamp_value(last_seen),
            "duration_seconds": total_duration,
            "duration_human": self._duration_label(total_duration),
            "stage_durations": durations,
            "stage_timestamps": {
                key: _timestamp_value(value) for key, value in stage_timestamps.items()
            },
            "stages": stages,
            "latest_detail": events[-1]["detail"] if events else "",
            "recovery": {
                "bound": recovery_bound,
                "email": latest_recovery["recovery_email"] if latest_recovery else "",
                "reason": latest_recovery["reason"] if latest_recovery else "not_recorded",
                "detail": latest_recovery["detail"] if latest_recovery else "",
            },
            "events": clean_events,
            "recovery_events": clean_recovery_events,
            "traffic": account.get(
                "traffic",
                {
                    "available": False,
                    "total_bytes": 0,
                    "human": "未采集",
                    "by_stage": [],
                },
            ),
        }

    @staticmethod
    def _averages(accounts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        duration_values = [
            account["duration_seconds"]
            for account in accounts
            if account["duration_seconds"] is not None
        ]
        result: dict[str, dict[str, Any]] = {
            "total": {
                "average_seconds": _round_seconds(mean(duration_values)) if duration_values else None,
                "samples": len(duration_values),
            }
        }
        stage_duration_names = {
            "registered": "registration",
            "recovery_bound": "recovery",
            "oauth_authorized": "oauth",
            "hx_email_imported": "hx_email",
        }
        for stage, duration_name in stage_duration_names.items():
            values = [
                account["stage_durations"].get(duration_name)
                for account in accounts
                if account["stage_durations"].get(duration_name) is not None
            ]
            result[stage] = {
                "average_seconds": _round_seconds(mean(values)) if values else None,
                "samples": len(values),
            }
        return result


app = FastAPI(title="Outlook Register Dashboard", version="1.0.0")
ACTION_RUNNER = DashboardActionRunner(PROJECT_ROOT, RESULTS_DIR)
WORKFLOW_RUNNER = WorkflowRunner(PROJECT_ROOT, results_dir=RESULTS_DIR)


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
        pool = RotatingProxyPool(proxy_config)
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
def resume_account_action(email: str, action: str) -> dict[str, Any]:
    normalized_action = action.replace("-", "_").strip().casefold()
    try:
        state = ACTION_RUNNER.resume_verification(email, normalized_action)
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
