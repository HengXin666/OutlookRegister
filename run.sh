#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

# 调试模式：OUTLOOK_DEBUG=1 run.sh
# 调试模式下不使用住宅代理，改用本机代理/直连，便于本地调试。
if [[ "${OUTLOOK_DEBUG:-0}" == "1" ]]; then
  echo "[run.sh] 调试模式已启用：禁用住宅代理，使用本机网络/代理。"
  export OUTLOOK_DEBUG=1
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "npm 未安装，无法构建 dashboard 前端" >&2
  exit 1
fi

if [[ ! -d dashboard/dist ]]; then
  echo "正在构建 dashboard 前端..."
  (cd "$PROJECT_ROOT/dashboard" && npm run build)
else
  echo "dashboard 前端已构建，跳过（删除 dashboard/dist 可强制重建）"
fi

exec uv run uvicorn outlookregister.dashboard.dashboard_server:app --host 127.0.0.1 --port 8765
