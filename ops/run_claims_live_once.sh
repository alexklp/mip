#!/bin/bash

set -Eeuo pipefail

REPO="$HOME/mip"
STATE_DIR="$HOME/.local/state/mip"
LOG_DIR="$STATE_DIR/log"
LOCK_DIR="$STATE_DIR/locks"
UV="$HOME/.local/bin/uv"

mkdir -p "$LOG_DIR" "$LOCK_DIR"

exec >> "$LOG_DIR/claims-live-$(date +%F).log" 2>&1

echo
echo "============================================================"
echo "CLAIMS LIVE START $(date --iso-8601=seconds)"
echo "============================================================"

exec 9>"$LOCK_DIR/mamay.lock"

if ! /usr/bin/flock -n 9; then
    echo "SKIP: another Mamay workload is running"
    exit 0
fi

if ! /usr/bin/curl -fsS \
    --max-time 5 \
    http://127.0.0.1:8080/health \
    >/dev/null
then
    echo "SKIP: Mamay endpoint is unavailable"
    exit 0
fi

cd "$REPO"

"$UV" run python \
    experiments/claim_extraction/claim_extract_worker.py \
    --limit 15 \
    --decision analyze \
    --order newest

echo "CLAIMS LIVE END $(date --iso-8601=seconds)"
