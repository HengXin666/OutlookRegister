"""Persistent config validation helpers and ``validate_config``.

The small merge/redact/number helpers live here so ``config_store`` can re-export
them. The larger validation blocks are split into ``config_validators`` to keep
this module under 300 lines; this module imports that one via a module alias so
both modules stay importable from ``config_store`` without import cycles.
"""

from __future__ import annotations

import copy
from typing import Any

from src.outlookregister.config.proxy_rotation_config import parse_control_plane_url

import src.outlookregister.config.config_validators as _v

CONFIGURED_VALUE = "__configured__"


class ConfigError(ValueError):
    """Raised when a configuration cannot be read, merged, or validated."""


_SECRET_KEYS = {
    "api_key",
    "access_token",
    "id_token",
    "password",
    "proxy",
    "proxy_url",
    "control_url",
    "rotation_url",
    "refresh_token",
    "token",
}


def _is_secret_key(key: str) -> bool:
    normalized = str(key or "").casefold()
    return normalized in _SECRET_KEYS or normalized.endswith("_secret")


def _merge_value(base: Any, patch: Any, key: str = "") -> Any:
    if _is_secret_key(key) and patch == CONFIGURED_VALUE:
        return copy.deepcopy(base)
    if isinstance(patch, dict):
        result = copy.deepcopy(base) if isinstance(base, dict) else {}
        for raw_key, value in patch.items():
            child_key = str(raw_key)
            result[child_key] = _merge_value(
                result.get(child_key), value, child_key
            )
        return result
    if isinstance(patch, list):
        base_items = base if isinstance(base, list) else []
        return [
            _merge_value(
                base_items[index] if index < len(base_items) else None,
                value,
            )
            for index, value in enumerate(patch)
        ]
    return copy.deepcopy(patch)


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    return _merge_value(base, patch)


def _redact(value: Any, key: str = "") -> Any:
    if key == "base_url" and isinstance(value, str):
        try:
            # A pasted /rot/<token> URL is a bearer credential. Keep only its
            # origin in dashboard responses; the token belongs in token fields.
            return parse_control_plane_url(value).origin
        except ValueError:
            pass
    if _is_secret_key(key):
        return CONFIGURED_VALUE if value not in (None, "") else ""
    if isinstance(value, dict):
        return {name: _redact(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    return value


def _number(value: Any, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是整数") from exc
    if parsed < minimum or parsed > maximum:
        raise ConfigError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


def validate_config(config: dict[str, Any], *, for_run: bool = False) -> list[str]:
    """Return stable, user-facing validation errors without exposing secrets."""
    errors = _v._validate_basic_fields(config)

    (
        rotation_errors,
        _rotation_endpoint,
        auto_identity,
        control_url,
        rotation_url,
        base_url,
        proxy_rotation,
    ) = _v._validate_proxy_rotation(config)
    errors.extend(rotation_errors)

    identity_errors, identity, require_dynamic = _v._validate_identity(
        config, auto_identity
    )
    errors.extend(identity_errors)

    errors.extend(
        _v._validate_runtime_dynamic(
            config,
            identity,
            proxy_rotation,
            auto_identity,
            require_dynamic,
            control_url,
            rotation_url,
            base_url,
            for_run,
        )
    )
    return errors
