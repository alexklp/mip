#!/bin/bash

set -Eeuo pipefail

REPO="$HOME/mip"
STATE_DIR="$HOME/.local/state/mip"
LOG_DIR="$STATE_DIR/log"
LOCK_DIR="$STATE_DIR/locks"
UV="$HOME/.local/bin/uv"
RUNTIME_ENV="$HOME/.config/mip/runtime.env"

mkdir -p "$LOG_DIR" "$LOCK_DIR"

exec >> "$LOG_DIR/segment-pipeline-$(date +%F).log" 2>&1

echo
echo "============================================================"
echo "SEGMENT PIPELINE START $(date --iso-8601=seconds)"
echo "============================================================"

# Prevent overlapping segment-pipeline invocations.
# Mamay serialization itself remains inside segment_pipeline_once.py.
exec 9>"$LOCK_DIR/segment-pipeline.lock"

if ! /usr/bin/flock -n 9; then
    echo "SKIP: previous segment-pipeline cycle is still running"
    exit 0
fi

if [[ ! -f "$RUNTIME_ENV" ]]; then
    echo "ERROR: runtime environment file is missing"
    exit 1
fi

set -a
source "$RUNTIME_ENV"
set +a

cd "$REPO"

"$UV" run python \
    experiments/segment_pipeline/segment_pipeline_once.py \
    --occurrence-limit 2 \
    --claim-limit 2 \
    --max-claim-chars 4000

echo "SEGMENT PIPELINE END $(date --iso-8601=seconds)"
