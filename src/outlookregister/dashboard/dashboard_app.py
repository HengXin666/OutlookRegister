"""FastAPI app 实例与后台 runner 单例。"""

from __future__ import annotations

from fastapi import FastAPI

from outlookregister import PROJECT_ROOT
from outlookregister.dashboard.dashboard_actions import (
    DashboardActionRunner,
)
from outlookregister.dashboard.dashboard_constants import (
    CONFIG_STORE,  # noqa: F401
    RESULTS_DIR,
)
from outlookregister.dashboard.workflow_runner import WorkflowRunner

app = FastAPI(title="Outlook Register Dashboard", version="1.0.0")
ACTION_RUNNER = DashboardActionRunner(PROJECT_ROOT, RESULTS_DIR)
WORKFLOW_RUNNER = WorkflowRunner(PROJECT_ROOT, results_dir=RESULTS_DIR)
