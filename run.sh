#!/usr/bin/env bash
# 一条本地启动命令：重建演示库（含转科、丢码样例）→ 启动 Web。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${WARD_PORT:-8000}"

echo ">> 重建 SQLite 演示库（虚构数据，含转科与手机丢码样例）..."
python3 -m app.seed

echo ">> 证件遮罩样图：data/badges/*.svg"
ls -1 data/badges/ 2>/dev/null | sed 's/^/   - /'

echo ">> 启动病区陪护通行管理：http://localhost:${PORT}"
exec python3 -m app.server --port "${PORT}"
