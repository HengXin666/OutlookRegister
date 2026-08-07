"""DashboardStore 内部账号聚合辅助。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import mean as _mean
from typing import Any

from outlookregister.dashboard.dashboard_constants import (
    TRAFFIC_FILE,
)
from outlookregister.dashboard.dashboard_serializers import (
    _email_from,
    _email_key,
    _human_bytes,
    _number,
    _parse_timestamp,
    _round_seconds,
    _traffic_stage_label,
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


def _duration_label(seconds):
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


def _averages(accounts):
    duration_values = [
        account["duration_seconds"]
        for account in accounts
        if account["duration_seconds"] is not None
    ]
    result = {
        "total": {
            "average_seconds": _round_seconds(_mean(duration_values)) if duration_values else None,
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
            "average_seconds": _round_seconds(_mean(values)) if values else None,
            "samples": len(values),
        }
    return result
