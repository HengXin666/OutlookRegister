"""Dashboard 共享常量与正则。"""
from __future__ import annotations

import os
import re
from pathlib import Path

from src.outlookregister import PROJECT_ROOT
from src.outlookregister.config.config_store import ConfigStore


DEFAULT_RESULTS_DIR = PROJECT_ROOT / "Results"
RESULTS_DIR = Path(os.getenv("OUTLOOK_RESULTS_DIR", str(DEFAULT_RESULTS_DIR))).expanduser()
CONFIG_STORE = ConfigStore(PROJECT_ROOT / "config.json")
CHECKPOINTS_FILE = "account_checkpoints.jsonl"
RECOVERY_FILE = "recovery_email_status.jsonl"
TRAFFIC_FILE = "traffic_usage.jsonl"

STAGE_DEFINITIONS = (
    ("registered", "已注册"),
    ("recovery_bound", "已绑定密保邮箱"),
    ("oauth_authorized", "已完成 OAuth 授权"),
    ("hx_email_imported", "已加入 HX-Email"),
)
STAGE_LABELS = dict(STAGE_DEFINITIONS)
TRAFFIC_STAGE_LABELS = {
    "residential_registration": "住宅 IP / 注册",
    "post_registration": "注册后邮箱初始化",
    "recovery_email": "密保邮箱验证",
    "oauth_browser": "OAuth 浏览器",
    "oauth_token_exchange": "OAuth Token 交换",
    "hx_email_import": "HX-Email 账号导入",
    "hx_email_api": "HX-Email API",
    "proxy_control": "代理控制面",
    "unknown": "未分类",
}
REGISTERED_EVIDENCE = {
    "registered",
    "registration_flow_completed",
    "post_registration_failed",
    "recovery_failed",
    "oauth_launch_failed",
    "oauth_failed",
    "oauth_success",
    "hx_email_import_failed",
    "hx_email_imported",
}
FAILURE_STAGES = {
    "navigation_failed",
    "registration_rejected",
    "captcha_unsupported",
    "registration_unconfirmed",
    "recovery_failed",
    "post_registration_failed",
    "oauth_launch_failed",
    "oauth_failed",
    "oauth_token_invalid",
    "hx_email_import_failed",
}

_SENSITIVE_DETAIL_PATTERN = re.compile(
    r"(?i)\b(password|passwd|refresh[-_ ]?token|access[-_ ]?token|id[-_ ]?token|"
    r"api[-_ ]?key|client[-_ ]?secret|authorization)\b"
    r"(?:\s*[:=]\s*|\s*[-_/]\s*|\s+)[^\s,;|]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_PATTERN = re.compile(
    r"\b[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"
)
_QUERY_SECRET_PATTERN = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|id_token|token|api_key|secret)=)"
    r"[^&#\s]+"
)
