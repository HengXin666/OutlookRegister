"""Validation blocks for the proxy source and the HX-Email stage groups.

Split out of ``config_validators`` to keep each module focused and small.
``config_validators`` re-exports these names so ``config_validation`` can keep
reaching every ``_validate_*`` helper through a single module alias.
"""

from __future__ import annotations

from typing import Any

from outlookregister.config.proxy_rotation_config import (
    MANUAL_SOURCE,
    RESIDENTIAL_SOURCE,
    parse_manual_proxy_lines,
    parse_proxy_source,
)

# Group names end up as HX-Email group labels, so keep them short and visible.
_MAX_GROUP_NAME_LENGTH = 120


def _validate_proxy_source(
    config: dict[str, Any],
) -> tuple[list[str], str, list[str]]:
    """Validate ``proxy_source`` and the manual proxy list it selects."""
    errors: list[str] = []
    try:
        proxy_source = parse_proxy_source(config.get("proxy_source"))
    except ValueError as exc:
        errors.append(str(exc))
        proxy_source = (
            MANUAL_SOURCE if config.get("manual_proxy_pool") else RESIDENTIAL_SOURCE
        )

    manual_pool = config.get("manual_proxy_pool")
    if manual_pool is None:
        manual_pool = {}
    if not isinstance(manual_pool, dict):
        errors.append("manual_proxy_pool 必须是对象")
        return errors, proxy_source, []

    pending: list[str] = []
    for field in ("pending", "used"):
        try:
            entries = parse_manual_proxy_lines(manual_pool.get(field))
        except ValueError as exc:
            errors.append(
                str(exc).replace("manual_proxy_pool", f"manual_proxy_pool.{field}")
            )
            continue
        if field == "pending":
            pending = entries
    return errors, proxy_source, pending


def _validate_runtime_manual(
    config: dict[str, Any],
    manual_entries: list[str],
    require_dynamic: bool,
    for_run: bool,
) -> list[str]:
    """Runtime checks for the manual proxy list source.

    A manual list still yields one session-scoped proxy per flow with a
    verified exit IP, so the leak-prevention contract is unchanged; only the
    HX-ProxyGroup control plane is not involved.
    """
    errors: list[str] = []
    if not (for_run and require_dynamic and not config.get("debug", False)):
        return errors

    if not manual_entries:
        errors.append("manual_proxy_pool.pending 至少需要一行可用代理")
    if str(config.get("proxy") or "").strip():
        errors.append("手动代理列表模式禁止使用顶层静态 proxy")
    if config.get("prevent_direct_network_leaks", True) is not True:
        errors.append("prevent_direct_network_leaks=true 是手动代理列表运行的必需项")
    return errors


def _validate_hx_email_groups(config: dict[str, Any]) -> list[str]:
    """Validate the per-stage HX-Email group names and the isolation switch."""
    errors: list[str] = []
    hx_email = ((config.get("recovery_email") or {}).get("hx_email")) or {}
    if not isinstance(hx_email, dict):
        return ["recovery_email.hx_email 必须是对象"]

    for field in ("account_group", "register_account_group", "keepalive_account_group"):
        value = hx_email.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append(f"recovery_email.hx_email.{field} 必须是字符串")
            continue
        name = value.strip()
        if not name:
            errors.append(f"recovery_email.hx_email.{field} 不能是空白字符串")
        elif len(name) > _MAX_GROUP_NAME_LENGTH:
            errors.append(
                f"recovery_email.hx_email.{field} 不能超过 "
                f"{_MAX_GROUP_NAME_LENGTH} 个字符"
            )

    isolate = config.get("isolate_hx_email_group")
    if isolate is not None and not isinstance(isolate, bool):
        errors.append("isolate_hx_email_group 必须是布尔值")
    return errors


def _stage_group_name(config: dict[str, Any], stage: str) -> str:
    """Return the configured HX-Email group for ``register`` or ``keepalive``."""
    hx_email = ((config.get("recovery_email") or {}).get("hx_email")) or {}
    if not isinstance(hx_email, dict):
        return ""
    specific = str(hx_email.get(f"{stage}_account_group") or "").strip()
    if specific:
        return specific
    return str(hx_email.get("account_group") or "").strip()
