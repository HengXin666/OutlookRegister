"""Persistent, redacted configuration access for the local dashboard."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from src.outlookregister.config.config_validation import (
    CONFIGURED_VALUE,
    ConfigError,
    _is_secret_key,
    _merge,
    _merge_value,
    _number,
    _redact,
    _SECRET_KEYS,
    validate_config,
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"未找到配置文件: {path.name}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("config.json 不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise ConfigError("config.json 顶层必须是对象")
    return value


class ConfigStore:
    """Read and atomically update config.json while preserving write-only secrets."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self._lock:
            value = _read_json(self.path)
            if isinstance(value, dict) and os.environ.get("OUTLOOK_DEBUG", "").strip() in {"1", "true", "yes", "on"}:
                value = {**value, "debug": True}
            return value

    def revision(self) -> str:
        try:
            stat = self.path.stat()
        except OSError:
            return "missing"
        return f"{stat.st_mtime_ns}:{stat.st_size}"

    def public(self) -> dict[str, Any]:
        with self._lock:
            value = self.read()
            return {
                "revision": self.revision(),
                "config": _redact(value),
                "validation_errors": validate_config(value),
                "runtime_validation_errors": validate_config(value, for_run=True),
            }

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ConfigError("配置更新必须是对象")
        with self._lock:
            current = self.read()
            updated = _merge(current, patch)
            errors = validate_config(updated)
            if errors:
                raise ConfigError("；".join(errors))
            self._atomic_write(updated)
            return self.public()

    def _atomic_write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, indent=4)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise
