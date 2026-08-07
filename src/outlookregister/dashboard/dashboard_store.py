"""Dashboard 聚合快照构建。"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from outlookregister.dashboard.dashboard_constants import (  # noqa: F401
    CHECKPOINTS_FILE,
    DEFAULT_RESULTS_DIR,
    FAILURE_STAGES,
    RECOVERY_FILE,
    REGISTERED_EVIDENCE,
    RESULTS_DIR,
    STAGE_DEFINITIONS,
    STAGE_LABELS,
    TRAFFIC_FILE,
    TRAFFIC_STAGE_LABELS,
)
from outlookregister.dashboard.dashboard_record_helpers import (  # noqa: F401
    _account_record,
    _add_account,
    _averages,
    _build_traffic,
    _duration_label,
    _event_time,
    _milestone_durations,
    _remember_identity_country,
    _time_delta,
)
from outlookregister.dashboard.dashboard_serializers import (  # noqa: F401
    _email_from,
    _email_key,
    _human_bytes,
    _number,
    _parse_timestamp,
    _read_jsonl,
    _round_seconds,
    _sanitize_detail,
    _timestamp_value,
    _traffic_stage_label,
)


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

        averages = _averages(account_rows)
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
            "generated_at": datetime.now(UTC).isoformat(),
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
                "average_duration_human": _duration_label(
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
                    "average_human": _duration_label(
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

    def _finalize_account(self, account: dict[str, Any]) -> dict[str, Any]:
        events = sorted(
            account["events"],
            key=lambda item: (item["_timestamp"] is None, item["_timestamp"] or datetime.min.replace(tzinfo=UTC), item["index"]),
        )
        recovery_events = sorted(
            account["recovery_events"],
            key=lambda item: (item["_timestamp"] is None, item["_timestamp"] or datetime.min.replace(tzinfo=UTC), item["index"]),
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
            "duration_human": _duration_label(total_duration),
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
