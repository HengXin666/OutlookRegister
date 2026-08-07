"""Background batch workflows started from the local dashboard."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from outlookregister.browser.patchright_controller import PatchrightController
from outlookregister.browser.playwright_controller import PlaywrightController
from outlookregister.config.config_store import (
    ConfigError,
    ConfigStore,
    validate_config,
)
from outlookregister.core.main import run_concurrent_flows
from outlookregister.dashboard.traffic_tracker import TrafficRecorder
from outlookregister.proxy.proxy_rotation import RotatingProxyPool


class WorkflowError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class WorkflowRunner:
    def __init__(
        self,
        project_root: Path | str,
        max_workers: int = 1,
        results_dir: Path | str | None = None,
    ):
        self.project_root = Path(project_root)
        self.results_dir = Path(results_dir or self.project_root / "Results")
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="dashboard-workflow",
        )
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def submit_registration(self, count: int, concurrency: int | None = None) -> dict[str, Any]:
        config = self._config()
        errors = validate_config(config, for_run=True)
        if errors:
            raise WorkflowError("配置不允许执行注册流程: " + "；".join(errors))
        recovery_config = config.get("recovery_email") or {}
        oauth_config = config.get("oauth2") or {}
        if recovery_config.get("enabled") is not True:
            raise WorkflowError("完全注册流程要求启用 recovery_email")
        if oauth_config.get("enable_oauth2") is not True:
            raise WorkflowError("完全注册流程要求启用 oauth2.enable_oauth2")
        if not str(oauth_config.get("client_id") or "").strip():
            raise WorkflowError("完全注册流程要求配置 oauth2.client_id")
        try:
            total = max(1, min(int(count), 10000))
            workers = max(1, min(int(concurrency or config.get("concurrent_flows", 1)), 64))
        except (TypeError, ValueError) as exc:
            raise WorkflowError("count 和 concurrency 必须是整数") from exc
        job_id = f"register-{uuid.uuid4().hex}"
        state = {
            "job_id": job_id,
            "kind": "register",
            "status": "queued",
            "total": total,
            "concurrency": workers,
            "message": "注册批量任务已排队",
            "updated_at": self._timestamp(),
        }
        with self._lock:
            self._jobs[job_id] = state
        self.executor.submit(self._run_registration, job_id, total, workers)
        return dict(state)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {job_id: dict(state) for job_id, state in self._jobs.items()}

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _run_registration(self, job_id: str, total: int, workers: int) -> None:
        self._set_state(job_id, "running", "注册流程正在执行")
        controller = None
        proxy_pool = None
        try:
            config = self._config()
            errors = validate_config(config, for_run=True)
            if errors:
                raise WorkflowError("配置在任务启动后变为无效: " + "；".join(errors))
            proxy_config = dict(config.get("proxy_rotation") or {})
            proxy_config["required_pool_size"] = workers
            proxy_pool = RotatingProxyPool(proxy_config)
            controller = self._controller(config)
            controller.results_dir = str(self.results_dir)
            controller.traffic = TrafficRecorder(self.results_dir)
            controller.hx_email.set_traffic_recorder(controller.traffic)
            result = run_concurrent_flows(
                controller,
                concurrent_flows=workers,
                max_tasks=total,
                proxy_pool=proxy_pool,
            )
            self._set_state(
                job_id,
                "succeeded",
                f"注册批量任务完成：成功 {result['succeeded']}，失败 {result['failed']}",
                result=result,
            )
        except Exception as exc:
            self._set_state(job_id, "failed", str(exc)[:500])
        finally:
            if controller is not None:
                try:
                    controller.clean_up(type="all_browser")
                except Exception:
                    pass

    def _set_state(
        self,
        job_id: str,
        status: str,
        message: str,
        **extra: Any,
    ) -> None:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                return
            state.update(
                {
                    "status": status,
                    "message": message,
                    "updated_at": self._timestamp(),
                    **extra,
                }
            )

    def _config(self) -> dict[str, Any]:
        try:
            return ConfigStore(self.project_root / "config.json").read()
        except ConfigError as exc:
            raise WorkflowError(str(exc)) from exc

    @staticmethod
    def _controller(config: dict[str, Any]):
        browser = str(config.get("choose_browser") or "").strip().casefold()
        if browser == "patchright":
            return PatchrightController()
        if browser == "playwright":
            return PlaywrightController()
        raise WorkflowError("choose_browser 必须是 patchright 或 playwright")

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat()
