#!/bin/bash

set -Eeuo pipefail

REPO="$HOME/mip"
ENV_FILE="$HOME/.config/mip/runtime.env"
STATE_DIR="$HOME/.local/state/mip"
LOG_DIR="$STATE_DIR/log"
LOCK_DIR="$STATE_DIR/locks"
UV="$HOME/.local/bin/uv"

mkdir -p "$LOG_DIR" "$LOCK_DIR"

exec >> "$LOG_DIR/ingest-$(date +%F).log" 2>&1

echo
echo "============================================================"
echo "INGEST START $(date --iso-8601=seconds)"
echo "============================================================"

exec 9>"$LOCK_DIR/ingest.lock"

if ! /usr/bin/flock -n 9; then
    echo "SKIP: previous ingestion cycle is still running"
    exit 0
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: runtime env not found: $ENV_FILE"
    exit 1
fi

set -a
source "$ENV_FILE"
set +a

if [[ -z "${MIP_RSS_PROXY_URL:-}" ]]; then
    echo "ERROR: MIP_RSS_PROXY_URL is empty"
    exit 1
fi

cd "$REPO"

echo "--- RSS ---"
"$UV" run python collectors/rss_worker.py --once

echo "--- TELEGRAM ---"
"$UV" run python collectors/tg_web_worker.py --once

echo "INGEST END $(date --iso-8601=seconds)"
