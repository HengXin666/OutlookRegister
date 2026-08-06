"""FastAPI 数据服务与仪表盘静态入口。

实现按职责拆分为 constants/serializers/record_helpers/store/app/routes 子模块；
本文件仅做转发与公共名再导出，保持旧导入路径（``dashboard_server`` shim 与 uvicorn
加载的 ``dashboard_server:app``）可用。
"""

from __future__ import annotations

# 兼容旧导入：从汇总 app 模块导出 app 与 runner 单例
from src.outlookregister.dashboard.dashboard_app import (  # noqa: F401
    app, ACTION_RUNNER, WORKFLOW_RUNNER, CONFIG_STORE,
)
from src.outlookregister.dashboard.dashboard_store import DashboardStore  # noqa: F401
from src.outlookregister.dashboard.dashboard_serializers import (  # noqa: F401
    _interactive_proxy_config, _automatic_proxy_config, _sanitize_detail,
    _parse_timestamp, _timestamp_value, _read_jsonl, _email_from, _email_key,
    _number, _round_seconds, _human_bytes, _traffic_stage_label,
)
from src.outlookregister.dashboard.dashboard_constants import (  # noqa: F401
    PROJECT_ROOT, RESULTS_DIR, CHECKPOINTS_FILE, RECOVERY_FILE, TRAFFIC_FILE,
    STAGE_DEFINITIONS, STAGE_LABELS, TRAFFIC_STAGE_LABELS,
    REGISTERED_EVIDENCE, FAILURE_STAGES,
)
from src.outlookregister.dashboard import dashboard_routes  # noqa: F401  注册路由
