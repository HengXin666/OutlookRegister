"""Best-effort, stage-aware network byte observation for registration tasks."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class TrafficRecorder:
    """Aggregate observed bytes per task and flush one JSONL record per bucket."""

    def __init__(self, results_dir: str | Path):
        self.results_dir = Path(results_dir)
        self.path = self.results_dir / "traffic_usage.jsonl"
        self._local = threading.local()
        self._file_lock = threading.Lock()
        self._pages: dict[int, dict[str, Any]] = {}
        self._pages_lock = threading.Lock()

    def start_task(self, outlook_email: str) -> None:
        self._local.email = str(outlook_email or "").strip()
        self._local.started_at = datetime.now(timezone.utc)
        self._local.buckets = defaultdict(lambda: {"bytes": 0, "bytes_sent": 0, "bytes_received": 0, "estimated": False})

    def has_task(self) -> bool:
        return bool(getattr(self._local, "email", "")) and hasattr(self._local, "buckets")

    @contextmanager
    def stage(self, stage: str, source: str) -> Iterator[None]:
        previous = getattr(self._local, "stage", None), getattr(self._local, "source", None)
        self._local.stage = stage
        self._local.source = source
        try:
            yield
        finally:
            self._local.stage, self._local.source = previous

    def record_http(
        self,
        stage: str,
        source: str,
        bytes_sent: int = 0,
        bytes_received: int = 0,
        estimated: bool = False,
        email: str | None = None,
    ) -> None:
        self.record(
            stage,
            source,
            bytes_sent=bytes_sent,
            bytes_received=bytes_received,
            estimated=estimated,
            email=email,
        )

    def record(
        self,
        stage: str | None = None,
        source: str | None = None,
        *,
        bytes_sent: int = 0,
        bytes_received: int = 0,
        estimated: bool = False,
        email: str | None = None,
    ) -> None:
        if not self.has_task():
            return
        stage = str(stage or getattr(self._local, "stage", "unknown")) or "unknown"
        source = str(source or getattr(self._local, "source", stage)) or stage
        sent = max(int(bytes_sent or 0), 0)
        received = max(int(bytes_received or 0), 0)
        total = sent + received
        if total <= 0:
            return
        bucket_key = (stage, source)
        bucket = self._local.buckets[bucket_key]
        bucket["bytes"] += total
        bucket["bytes_sent"] += sent
        bucket["bytes_received"] += received
        bucket["estimated"] = bucket["estimated"] or bool(estimated)

    def attach_page(self, page: Any, stage: str, source: str) -> None:
        """Use Chromium CDP encodedDataLength when available, with a header fallback."""
        email = getattr(self._local, "email", "")
        page_id = id(page)
        page_state = {"stage": stage, "source": source, "email": email}
        with self._pages_lock:
            self._pages[page_id] = page_state

        try:
            cdp = page.context.new_cdp_session(page)
            cdp.send("Network.enable")

            def on_loading_finished(event: dict[str, Any]) -> None:
                self.record(
                    page_state["stage"],
                    page_state["source"],
                    bytes_received=int(event.get("encodedDataLength") or 0),
                    estimated=False,
                    email=page_state["email"],
                )

            cdp.on("Network.loadingFinished", on_loading_finished)
            page_state["cdp"] = cdp
            return
        except Exception:
            # Patchright/browser variants without CDP support still expose response
            # headers, which gives a useful lower-fidelity measurement.
            pass

        def on_response(response: Any) -> None:
            try:
                content_length = response.headers.get("content-length")
                received = int(content_length or 0)
            except (AttributeError, TypeError, ValueError):
                received = 0
            self.record(
                page_state["stage"],
                page_state["source"],
                bytes_received=received,
                estimated=True,
                email=page_state["email"],
            )

        try:
            page.on("response", on_response)
            page_state["fallback_handler"] = on_response
        except Exception:
            pass

    def set_page_stage(self, page: Any, stage: str, source: str | None = None) -> None:
        with self._pages_lock:
            state = self._pages.get(id(page))
            if state is not None:
                state["stage"] = stage
                if source:
                    state["source"] = source

    def finish_task(self) -> None:
        if not self.has_task():
            return
        email = getattr(self._local, "email", "")
        started_at = getattr(self._local, "started_at", None)
        finished_at = datetime.now(timezone.utc)
        buckets = getattr(self._local, "buckets", {})
        records = []
        for (stage, source), bucket in buckets.items():
            if bucket["bytes"] <= 0:
                continue
            records.append(
                {
                    "timestamp": finished_at.isoformat(),
                    "outlook_email": email,
                    "stage": stage,
                    "source": source,
                    "bytes": bucket["bytes"],
                    "bytes_sent": bucket["bytes_sent"],
                    "bytes_received": bucket["bytes_received"],
                    "estimated": bucket["estimated"],
                    "task_started_at": started_at.isoformat() if started_at else None,
                }
            )
        if records:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            with self._file_lock:
                with self.path.open("a", encoding="utf-8") as traffic_file:
                    for record in records:
                        traffic_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        for attribute in ("email", "started_at", "buckets", "stage", "source"):
            if hasattr(self._local, attribute):
                delattr(self._local, attribute)


def stage_for_hx_email_path(path: str) -> str:
    if "temp-mail" in path or "temp-emails" in path:
        return "recovery_email"
    if (
        "/email-accounts" in path
        or "/mail-pool" in path
        or "/groups" in path
    ):
        return "hx_email_import"
    return "hx_email_api"
