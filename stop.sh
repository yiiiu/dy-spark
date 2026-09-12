#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PORT=18101
if [ -f .env ]; then
  ENV_PORT=$(grep -E '^PORT=' .env | cut -d= -f2- | tr -d '\r\n')
  if [ -n "$ENV_PORT" ]; then
    PORT="$ENV_PORT"
  fi
fi

KILLED=0

# 1. 检查并停止 systemd 服务
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet douyin-spark; then
  echo "正在停止 systemd 服务 douyin-spark..."
  systemctl stop douyin-spark
  echo "[成功] 已停止 douyin-spark 服务"
  exit 0
fi

# 2. 检查 PID 文件
if [ -f "data/app.pid" ]; then
  PID=$(cat data/app.pid 2>/dev/null || true)
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    echo "[已终止] 依据 PID ($PID) 停止了服务"
    KILLED=1
  fi
  rm -f data/app.pid
fi

# 3. 检查并释放端口
if command -v fuser >/dev/null 2>&1; then
  if fuser "$PORT"/tcp >/dev/null 2>&1; then
    fuser -k "$PORT"/tcp >/dev/null 2>&1 || true
    echo "[已终止] 释放了端口 $PORT 上的进程"
    KILLED=1
  fi
fi

if [ "$KILLED" -eq 1 ]; then
  echo "=================================================="
  echo "  [成功] 抖音续火花助手服务已暂停 / 停止！"
  echo "=================================================="
else
  echo "=================================================="
  echo "  [提示] 未发现正在运行的抖音续火花助手服务 (端口: $PORT)"
  echo "=================================================="
fi
