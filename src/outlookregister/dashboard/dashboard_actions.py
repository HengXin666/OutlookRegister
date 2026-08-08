"""后台账号保活动作执行器。

实现按职责拆分为多个 mixin/常量模块，本文件仅做组合 + 公共名再导出，
保持旧导入路径（``from dashboard_actions import DashboardActionRunner`` 等）与
测试 patch 目标（``dashboard_actions.classify_outlook_page`` /
``dashboard_actions.TrafficRecorder``）可用。
"""

from __future__ import annotations

from outlookregister.browser.outlook_page_state import (
    classify_outlook_page,
)
from outlookregister.dashboard.dashboard_action_authorize import _AuthorizeActions
from outlookregister.dashboard.dashboard_action_capture import _CaptureActions
from outlookregister.dashboard.dashboard_action_constants import (  # noqa: F401
    ACTION_LOG_LIMIT,
    AUTHORIZE,
    HX_EMAIL_HANDOFF_DELAY_SECONDS,
    IMPORT_HX_EMAIL,
    KEEPALIVE,
    KEEPALIVE_STEP_INDEX,
    KEEPALIVE_STEP_LABELS,
    KEEPALIVE_STEP_ORDER,
    OAUTH_PAGE_DELAY_MS,
    SUCCESS_WINDOW_DELAY_MS,
    VALID_ACTIONS,
    AccountArtifactStore,
    DashboardActionError,
    ManualVerificationRequired,
)
from outlookregister.dashboard.dashboard_action_import import _ImportActions
from outlookregister.dashboard.dashboard_action_keepalive import _KeepaliveActions
from outlookregister.dashboard.dashboard_action_keepalive_completion import (
    _KeepaliveCompletionActions,
)
from outlookregister.dashboard.dashboard_action_keepalive_import import (
    _KeepaliveImportActions,
)
from outlookregister.dashboard.dashboard_action_keepalive_login import (
    _KeepaliveLoginActions,
)
from outlookregister.dashboard.dashboard_action_keepalive_state import (
    _RunnerKeepaliveState,
)
from outlookregister.dashboard.dashboard_action_login import _LoginActions
from outlookregister.dashboard.dashboard_action_login_loop import _LoginActionsLoop
from outlookregister.dashboard.dashboard_action_login_unlock import _LoginUnlockActions
from outlookregister.dashboard.dashboard_action_orchestration import _RunnerOrchestrator
from outlookregister.dashboard.dashboard_action_runner_base import _RunnerBase
from outlookregister.dashboard.dashboard_action_subroutines import _RunnerSubroutines
from outlookregister.dashboard.traffic_tracker import TrafficRecorder
from outlookregister.oauth.get_token import (
    get_access_token,
    refresh_oauth_token,
)


class DashboardActionRunner(
    _AuthorizeActions,
    _RunnerSubroutines,
    _LoginActions,
    _LoginActionsLoop,
    _LoginUnlockActions,
    _KeepaliveActions,
    _KeepaliveLoginActions,
    _KeepaliveCompletionActions,
    _KeepaliveImportActions,
    _CaptureActions,
    _ImportActions,
    _RunnerKeepaliveState,
    _RunnerOrchestrator,
    _RunnerBase,
):
    """账号授权/导入/保活后台执行器；具体实现拆散到 mixin 文件。"""
    pass


__all__ = [
    "AUTHORIZE",
    "IMPORT_HX_EMAIL",
    "KEEPALIVE",
    "VALID_ACTIONS",
    "OAUTH_PAGE_DELAY_MS",
    "HX_EMAIL_HANDOFF_DELAY_SECONDS",
    "SUCCESS_WINDOW_DELAY_MS",
    "ACTION_LOG_LIMIT",
    "AccountArtifactStore",
    "DashboardActionError",
    "DashboardActionRunner",
    "ManualVerificationRequired",
    "TrafficRecorder",
    "classify_outlook_page",
    "get_access_token",
    "refresh_oauth_token",
]
