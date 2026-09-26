#!/usr/bin/env bash
set -euo pipefail

ROOT="${MIP_TOPICS_ROOT:-$HOME/mip-signals-codex}"
PYTHON="${MIP_PYTHON:-$HOME/mip/.venv/bin/python}"

STATE_DIR="$HOME/.local/state/mip"
LOG_DIR="$STATE_DIR/log"
LOCK_FILE="$STATE_DIR/topics-snapshot.lock"
LOG_FILE="$LOG_DIR/topics-snapshot-$(date +%F).log"

mkdir -p "$LOG_DIR"

# NLP (stanza/torch) + BLAS інакше беруть усі ядра і б'ються з рештою пайплайну.
# Ізольований замір 25.09: 1 потік швидше за 24 (18.5 с проти 23.2 с на об'єкт).
export OMP_NUM_THREADS=4
export OPENBLAS_NUM_THREADS=4
export MKL_NUM_THREADS=4
export NUMEXPR_NUM_THREADS=4

exec 9>"$LOCK_FILE"

if ! flock -n 9; then
    echo "TOPICS SNAPSHOT SKIP (lock busy, попередній прогон ще виконується) $(date --iso-8601=seconds)" >>"$LOG_FILE" 2>&1
    exit 0
fi

(
    echo
    echo "============================================================"
    echo "TOPICS SNAPSHOT START $(date --iso-8601=seconds)"
    echo "============================================================"

    cd "$ROOT"

    set +e

    echo "TOPICS CACHE START $(date --iso-8601=seconds)"

    PYTHONUNBUFFERED=1 \
    PYTHONPATH=reporting:. \
        nice -n 10 \
        "$PYTHON" \
        reporting/build_topics_nlp_cache.py \
        --since-hours 48 \
        --workers 4 \
        --chunk 25

    cache_rc=$?

    echo "TOPICS CACHE END rc=$cache_rc $(date --iso-8601=seconds)"

    echo "TOPICS SNAPSHOT BUILD $(date --iso-8601=seconds)"

    nice -n 10 \
        "$PYTHON" \
        reporting/topics_snapshot.py \
        --output reporting/topics.latest.json

    global_rc=$?

    echo "C1 TOPICS SNAPSHOT BUILD $(date --iso-8601=seconds)"

    PYTHONPATH=reporting:. \
        nice -n 10 \
        "$PYTHON" \
        reporting/contour_topics_c1.py \
        --all-objects

    c1_rc=$?

    if [ "$global_rc" -ne 0 ]; then
        rc="$global_rc"
    else
        rc="$c1_rc"
    fi

    set -e

    echo "TOPICS SNAPSHOT END global_rc=$global_rc c1_rc=$c1_rc rc=$rc $(date --iso-8601=seconds)"
    exit "$rc"
) >>"$LOG_FILE" 2>&1
