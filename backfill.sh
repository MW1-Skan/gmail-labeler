#!/bin/bash
# Runs the labeler in backfill mode repeatedly until the whole date range is
# labeled ("Nothing to do."). Output streams to the terminal and backfill.log.
#
# Usage: ./backfill.sh [AFTER_DATE] [CHUNK_SIZE]
#   AFTER_DATE  start of the backfill window, YYYY/MM/DD (default 2025/10/05 = 300 days)
#   CHUNK_SIZE  emails per run (default 500)
set -u
cd "$(dirname "$0")"

AFTER="${1:-2025/10/05}"
CHUNK="${2:-500}"
PY=.venv/bin/python
LOG=backfill.log

run=1
while :; do
    echo "=== Run $run — $(date '+%Y-%m-%d %H:%M:%S') — after $AFTER, max $CHUNK ==="
    tmp=$(mktemp)
    "$PY" labeler.py --backfill --after "$AFTER" --max "$CHUNK" --apply 2>&1 | tee "$tmp"
    status=${PIPESTATUS[0]}
    cat "$tmp" >> "$LOG"

    if [ "$status" -ne 0 ]; then
        rm -f "$tmp"
        echo "=== labeler.py exited with status $status — stopping. Fix the error, then re-run ./backfill.sh ==="
        exit "$status"
    fi

    if grep -q "Nothing to do." "$tmp"; then
        rm -f "$tmp"
        echo "=== Backfill complete after $run run(s). ==="
        exit 0
    fi

    fails=$(grep -c "LLM call failed" "$tmp")
    rm -f "$tmp"
    if [ "$fails" -gt 0 ]; then
        # Likely a subscription rate limit. Those emails were labeled Other and
        # won't be revisited; pause so the rest of the backfill isn't mislabeled.
        echo "!!! $fails failed LLM call(s) this run — pausing 60 minutes before continuing."
        sleep 3600
    else
        sleep 5
    fi
    run=$((run + 1))
done
