"""FastAPI app 实例与后台 runner 单例。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI

from src.outlookregister import PROJECT_ROOT
from src.outlookregister.config.config_store import ConfigError, ConfigStore
from src.outlookregister.dashboard.dashboard_constants import RESULTS_DIR, CONFIG_STORE
from src.outlookregister.proxy.proxy_rotation import ProxyRotationError, RotatingProxyPool
from src.outlookregister.dashboard.dashboard_actions import DashboardActionError, DashboardActionRunner
from src.outlookregister.dashboard.workflow_runner import WorkflowError, WorkflowRunner
from src.outlookregister.dashboard.dashboard_constants import RESULTS_DIR  # noqa: F401

app = FastAPI(title="Outlook Register Dashboard", version="1.0.0")
ACTION_RUNNER = DashboardActionRunner(PROJECT_ROOT, RESULTS_DIR)
WORKFLOW_RUNNER = WorkflowRunner(PROJECT_ROOT, results_dir=RESULTS_DIR)
