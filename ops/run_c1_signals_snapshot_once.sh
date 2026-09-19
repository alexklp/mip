#!/bin/bash

set -Eeuo pipefail

REPO="$HOME/mip-signals-codex"
STATE_DIR="$HOME/.local/state/mip"
LOG_DIR="$STATE_DIR/log"
LOCK_DIR="$STATE_DIR/locks"
PYTHON="$HOME/mip/.venv/bin/python"

mkdir -p "$LOG_DIR" "$LOCK_DIR"

exec >> "$LOG_DIR/c1-signals-snapshot-$(date +%F).log" 2>&1

echo
echo "============================================================"
echo "C1 SIGNALS SNAPSHOT START $(date --iso-8601=seconds)"
echo "============================================================"

exec 9>"$LOCK_DIR/c1-signals-snapshot.lock"

if ! /usr/bin/flock -n 9; then
    echo "SKIP: previous C1 Signals generation is still running"
    exit 0
fi

cd "$REPO"
started_at=$(date +%s)

mapfile -t scope_info < <(
    env \
        PYTHONDONTWRITEBYTECODE=1 \
        PGHOST=/var/run/postgresql \
        PYTHONPATH="$REPO:$REPO/reporting" \
        "$PYTHON" - <<'PY'
import psycopg
from psycopg.rows import dict_row

from reporting.signals_data import require_utc
from reporting.signals_postgres import WATERMARK_SQL
from web.c1_scope import c1_analytical_gate_sql

groups = ["ru_space", "ua_space"]
gate = c1_analytical_gate_sql("a")

with psycopg.connect(
    dbname="mip_dev",
    row_factory=dict_row,
    autocommit=True,
    options="-c default_transaction_read_only=on",
) as conn:
    with conn.cursor() as cur:
        cur.execute(WATERMARK_SQL, {"groups": groups})
        watermarks = {
            row["source_group"]: require_utc(row["watermark"])
            for row in cur.fetchall()
        }

        print(min(watermarks.values()).isoformat())

        cur.execute(f"""
            SELECT DISTINCT a.object_id
            FROM content_contour_assignments a
            WHERE a.monitoring_contour_id = 1
              AND a.object_id IS NOT NULL
              AND a.object_id <> 1
              AND a.evidence_type = 'exact_reference'
              {gate}
            ORDER BY a.object_id
        """)

        for row in cur.fetchall():
            print(int(row["object_id"]))
PY
)

AS_OF="${scope_info[0]}"
OBJECT_IDS=("${scope_info[@]:1}")

COMMON_ARGS=(
    --as-of "$AS_OF"
    --database mip_dev
    --model-id 1
    --model BAAI/bge-m3@5617a9f61b028005a4858fdac845db406aefb181
    --dimension 1024
    --source-groups ru_space ua_space
    --strategy exact_current_24h
    --anchors 40
    --neighbours 10
    --pairs 20000
    --rows 5000
    --core-distance 0.18
    --merge-distance 0.19
    --merge-min-cross-links 2
    --related-distance 0.36
    --ann-probe-limit 100
    --display-limit 200
    --max-related-links 100
    --evidence-chars 600
    --max-evidence 200
    --statement-timeout-ms 15000
    --contour-id 1
)

run_snapshot() {
    local output="$1"
    shift

    env \
        PYTHONDONTWRITEBYTECODE=1 \
        PGHOST=/var/run/postgresql \
        PYTHONPATH="$REPO:$REPO/reporting" \
        "$PYTHON" -m reporting.signals_snapshot \
        "${COMMON_ARGS[@]}" \
        "$@" \
        --output "$output"
}

run_snapshot "$REPO/reporting/signals_c1.latest.json"

for object_id in "${OBJECT_IDS[@]}"; do
    run_snapshot \
        "$REPO/reporting/signals_c1.object_${object_id}.latest.json" \
        --object-id "$object_id"
done

elapsed=$(( $(date +%s) - started_at ))

echo "AS_OF: $AS_OF"
echo "OBJECTS: ${#OBJECT_IDS[@]}"
echo "C1 SIGNALS SNAPSHOT END $(date --iso-8601=seconds) duration=${elapsed}s"
