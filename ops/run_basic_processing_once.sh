#!/bin/bash

set -Eeuo pipefail

REPO="$HOME/mip"
STATE_DIR="$HOME/.local/state/mip"
LOG_DIR="$STATE_DIR/log"
LOCK_DIR="$STATE_DIR/locks"
UV="$HOME/.local/bin/uv"

mkdir -p "$LOG_DIR" "$LOCK_DIR"

exec >> "$LOG_DIR/basic-processing-$(date +%F).log" 2>&1

echo
echo "============================================================"
echo "BASIC PROCESSING START $(date --iso-8601=seconds)"
echo "============================================================"

exec 9>"$LOCK_DIR/basic-processing.lock"

if ! /usr/bin/flock -n 9; then
    echo "SKIP: previous basic-processing cycle is still running"
    exit 0
fi

cd "$REPO"

echo "--- EMBEDDINGS ---"
"$UV" run python collectors/embed_worker.py

echo "--- ROUTING ---"
"$UV" run python experiments/content_routing/routing_worker.py

echo "BASIC PROCESSING END $(date --iso-8601=seconds)"
