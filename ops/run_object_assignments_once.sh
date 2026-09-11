#!/bin/bash

set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$HOME/.local/state/mip"
LOG_DIR="$STATE_DIR/log"
LOCK_DIR="$STATE_DIR/locks"
PYTHON="$HOME/mip/.venv/bin/python"

mkdir -p "$LOG_DIR" "$LOCK_DIR"

exec >> "$LOG_DIR/object-assignments-$(date +%F).log" 2>&1

echo
echo "============================================================"
echo "OBJECT ASSIGNMENTS START $(date --iso-8601=seconds)"
echo "============================================================"

exec 9>"$LOCK_DIR/object-assignments.lock"

if ! /usr/bin/flock -n 9; then
    echo "SKIP: object assignment worker is already running"
    exit 0
fi

cd "$REPO"

"$PYTHON" \
    experiments/content_contours/contour_assignment_worker.py \
    --c1-only \
    --write

echo "OBJECT ASSIGNMENTS END $(date --iso-8601=seconds)"
