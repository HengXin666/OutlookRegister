#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm 未安装，无法构建 dashboard 前端" >&2
  exit 1
fi

echo "正在构建 dashboard 前端..."
(cd "$PROJECT_ROOT/dashboard" && npm run build)

cd "$PROJECT_ROOT"
exec uv run uvicorn dashboard_server:app --host 127.0.0.1 --port 8765
