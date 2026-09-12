#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# 读取 .env 中的端口配置
PORT=18101
if [ -f .env ]; then
  ENV_PORT=$(grep -E '^PORT=' .env | cut -d= -f2- | tr -d '\r\n')
  if [ -n "$ENV_PORT" ]; then
    PORT="$ENV_PORT"
  fi
fi

# 检查 Python 解释器
PYTHON="python3"
if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
fi

# 如果由 systemctl 管理
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet douyin-spark; then
  echo "[提示] 检测到 systemd 服务 douyin-spark 已在运行中"
  exit 0
fi

# 检查端口是否已被占用
if command -v lsof >/dev/null 2>&1 && lsof -i :"$PORT" >/dev/null 2>&1; then
  echo "[提示] 端口 $PORT 已在监听中，服务已在运行"
  exit 0
fi

mkdir -p data/logs
nohup "$PYTHON" app.py > data/logs/console.log 2>&1 &
PID=$!
echo "$PID" > data/app.pid

echo "=================================================="
echo "  [成功] 抖音续火花助手已在后台启动！"
echo "  PID: $PID"
echo "  端口: $PORT"
echo "  日志: data/logs/console.log"
echo "  如需暂停/停止服务，请执行 ./stop.sh"
echo "=================================================="
