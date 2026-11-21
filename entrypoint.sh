#!/bin/sh
# 容器入口：先确保 raw 基线存在（幂等），再启动 API。
set -eu

: "${RECOVERY_DATA_DIR:=/data}"
: "${BASELINE_SIZE:=16MiB}"

python -m app.init_baseline --data-dir "$RECOVERY_DATA_DIR" --size "$BASELINE_SIZE"

exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1
