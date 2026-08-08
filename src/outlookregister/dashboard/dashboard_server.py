"""FastAPI 数据服务与仪表盘静态入口。

实现按职责拆分为 constants/serializers/record_helpers/store/app/routes 子模块；
本文件仅做转发与公共名再导出，保持旧导入路径（``dashboard_server`` shim 与 uvicorn
加载的 ``dashboard_server:app``）可用。
"""

from __future__ import annotations

from outlookregister.dashboard import dashboard_routes  # noqa: F401  注册路由
from outlookregister.dashboard import (  # noqa: F401  注册手动代理路由
    dashboard_routes_manual_proxy,
)

# 兼容旧导入：从汇总 app 模块导出 app 与 runner 单例
from outlookregister.dashboard.dashboard_app import (  # noqa: F401
    ACTION_RUNNER,
    CONFIG_STORE,
    WORKFLOW_RUNNER,
    app,
)
from outlookregister.dashboard.dashboard_constants import (  # noqa: F401
    CHECKPOINTS_FILE,
    FAILURE_STAGES,
    PROJECT_ROOT,
    RECOVERY_FILE,
    REGISTERED_EVIDENCE,
    RESULTS_DIR,
    STAGE_DEFINITIONS,
    STAGE_LABELS,
    TRAFFIC_FILE,
    TRAFFIC_STAGE_LABELS,
)
from outlookregister.dashboard.dashboard_serializers import (  # noqa: F401
    _automatic_proxy_config,
    _email_from,
    _email_key,
    _human_bytes,
    _interactive_proxy_config,
    _number,
    _parse_timestamp,
    _read_jsonl,
    _round_seconds,
    _sanitize_detail,
    _timestamp_value,
    _traffic_stage_label,
)
from outlookregister.dashboard.dashboard_store import DashboardStore  # noqa: F401
