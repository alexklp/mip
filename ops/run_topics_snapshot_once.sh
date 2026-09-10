#!/usr/bin/env bash
set -euo pipefail

ROOT="${MIP_TOPICS_ROOT:-$HOME/mip-signals-codex}"
PYTHON="${MIP_PYTHON:-$HOME/mip/.venv/bin/python}"

STATE_DIR="$HOME/.local/state/mip"
LOG_DIR="$STATE_DIR/log"
LOCK_FILE="$STATE_DIR/topics-snapshot.lock"
LOG_FILE="$LOG_DIR/topics-snapshot-$(date +%F).log"

mkdir -p "$LOG_DIR"

exec 9>"$LOCK_FILE"

if ! flock -n 9; then
    exit 0
fi

(
    echo
    echo "============================================================"
    echo "TOPICS SNAPSHOT START $(date --iso-8601=seconds)"
    echo "============================================================"

    cd "$ROOT"

    set +e
    nice -n 10 \
        "$PYTHON" \
        reporting/topics_snapshot.py \
        --output reporting/topics.latest.json
    rc=$?
    set -e

    echo "TOPICS SNAPSHOT END rc=$rc $(date --iso-8601=seconds)"
    exit "$rc"
) >>"$LOG_FILE" 2>&1
