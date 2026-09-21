#!/bin/bash

set -Eeuo pipefail

REPO="$HOME/mip-signals-codex"
STATE_DIR="$HOME/.local/state/mip"
LOG_DIR="$STATE_DIR/log"
LOCK_DIR="$STATE_DIR/locks"
PYTHON="$HOME/mip/.venv/bin/python"
OUTPUT="$REPO/reporting/signals.latest.json"

mkdir -p "$LOG_DIR" "$LOCK_DIR"

exec >> "$LOG_DIR/signals-snapshot-$(date +%F).log" 2>&1

echo
echo "============================================================"
echo "SIGNALS SNAPSHOT START $(date --iso-8601=seconds)"
echo "============================================================"

exec 9>"$LOCK_DIR/signals-snapshot.lock"

if ! /usr/bin/flock -n 9; then
    echo "SKIP: previous signals snapshot generation is still running"
    exit 0
fi

cd "$REPO"

started_at=$(date +%s)

if env \
    PYTHONDONTWRITEBYTECODE=1 \
    PGPASSFILE=/dev/null \
    PGHOST=/var/run/postgresql \
    "$PYTHON" -m reporting.signals_snapshot \
        --as-of common --database mip_dev \
        --model-id 1 \
        --model BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181 \
        --dimension 1024 \
        --source-groups ru_space ua_space \
        --strategy bounded_ann \
        --anchors 20000 \
        --neighbours 16 \
        --pairs 200000 \
        --rows 100000 \
        --core-distance 0.18 \
        --related-distance 0.36 \
        --ann-probe-limit 20000 \
        --display-limit 200 \
        --max-related-links 100 \
        --evidence-chars 600 \
        --max-evidence 12 \
        --statement-timeout-ms 90000 \
        --output "$OUTPUT"
then
    elapsed=$(( $(date +%s) - started_at ))
    echo "OUTPUT: $(stat -c 'modified=%y size=%s file=%n' "$OUTPUT")"
    echo "SIGNALS SNAPSHOT END $(date --iso-8601=seconds) duration=${elapsed}s"
else
    rc=$?
    echo "SIGNALS SNAPSHOT FAIL $(date --iso-8601=seconds) rc=$rc"
    exit "$rc"
fi
