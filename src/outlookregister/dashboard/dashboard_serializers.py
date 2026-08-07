"""纯函数：时间/JSONL 读取、流量阶段标签、代理配置与日志脱敏。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from outlookregister.dashboard.dashboard_constants import (
    _BEARER_PATTERN,
    _JWT_PATTERN,
    _QUERY_SECRET_PATTERN,
    _SENSITIVE_DETAIL_PATTERN,
    TRAFFIC_STAGE_LABELS,
)


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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


def _interactive_proxy_config(proxy_config: dict[str, Any]) -> dict[str, Any]:
    """Bound dashboard checks without changing the saved runtime retry policy."""
    try:
        timeout = float(proxy_config.get("timeout_seconds", 10))
    except (TypeError, ValueError):
        timeout = 10
    return {
        **proxy_config,
        "timeout_seconds": min(max(timeout, 1), 10),
        "max_rotate_retries": 0,
    }

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
