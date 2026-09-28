#!/bin/bash

set -Eeuo pipefail

REPO="$HOME/mip-signals-codex"
STATE_DIR="$HOME/.local/state/mip"
LOG_DIR="$STATE_DIR/log"
LOCK_DIR="$STATE_DIR/locks"
PYTHON="$HOME/mip/.venv/bin/python"

DRY_RUN=false

if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
elif [[ $# -ne 0 ]]; then
    echo "Usage: $0 [--dry-run]" >&2
    exit 2
fi

mkdir -p "$LOG_DIR" "$LOCK_DIR"

exec >> "$LOG_DIR/signal-theses-$(date +%F).log" 2>&1

echo
echo "============================================================"
echo "SIGNAL THESES START $(date --iso-8601=seconds)"
echo "============================================================"

exec 9>"$LOCK_DIR/mamay.lock"

if ! /usr/bin/flock -n 9; then
    echo "SKIP: another Mamay workload is running"
    exit 0
fi

if [[ "$DRY_RUN" == false ]]; then
    if ! /usr/bin/curl -fsS \
        --max-time 5 \
        http://127.0.0.1:8080/health \
        >/dev/null
    then
        echo "SKIP: Mamay endpoint is unavailable"
        exit 0
    fi
fi

cd "$REPO"

started_at=$(date +%s)

ARGS=()

if [[ "$DRY_RUN" == true ]]; then
    ARGS+=(--dry-run)
fi

if env \
    PYTHONDONTWRITEBYTECODE=1 \
    PGHOST=/var/run/postgresql \
    "$PYTHON" \
    -m reporting.signal_theses_worker \
    "${ARGS[@]}"
then
    elapsed=$(( $(date +%s) - started_at ))

    if [[ "$DRY_RUN" == true ]]; then
        echo "SIGNAL THESES DRY RUN END $(date --iso-8601=seconds) duration=${elapsed}s"
    else
        echo "SIGNAL THESES END $(date --iso-8601=seconds) duration=${elapsed}s"
    fi
else
    rc=$?
    echo "SIGNAL THESES FAIL $(date --iso-8601=seconds) rc=$rc"
    exit "$rc"
fi
