#!/usr/bin/env bash
# 一条本地命令启动图书馆阅读挑战系统（仅需 Python 3.10+，无第三方依赖）
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8000}"
exec python3 -m app "$PORT"
