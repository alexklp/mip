#!/bin/bash

set -Eeuo pipefail

REPO="$HOME/mip-signals-codex"
STATE_DIR="$HOME/.local/state/mip"
LOG_DIR="$STATE_DIR/log"
LOCK_DIR="$STATE_DIR/locks"
PYTHON="$HOME/mip/.venv/bin/python"

mkdir -p "$LOG_DIR" "$LOCK_DIR"

exec >> \
    "$LOG_DIR/c1-signals-snapshot-$(date +%F).log" \
    2>&1

echo
echo "============================================================"
echo "C1 SIGNALS 30D START $(date --iso-8601=seconds)"
echo "============================================================"

exec 9>"$LOCK_DIR/c1-signals-snapshot.lock"

if ! /usr/bin/flock -n 9; then
    echo "SKIP: previous C1 Signals generation is still running"
    exit 0
fi

cd "$REPO"

started_at=$(date +%s)

env \
    PYTHONDONTWRITEBYTECODE=1 \
    PGHOST=/var/run/postgresql \
    PYTHONPATH="$REPO:$REPO/reporting" \
    "$PYTHON" \
        -m reporting.signals_c1_30d \
        --database mip_dev \
        --all-objects

elapsed=$(( $(date +%s) - started_at ))

echo \
    "C1 SIGNALS 30D END $(date --iso-8601=seconds) " \
    "duration=${elapsed}s"
