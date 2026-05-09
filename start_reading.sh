#!/usr/bin/env bash
# start_reading.sh — Run weread-selenium-cli once, lock-protected, non-blocking.
# Usage:
#   start_reading.sh [trigger]      # blocks current shell, but caller usually
#                                   # invokes this with `&` or via Flask thread.
set -uo pipefail

TRIGGER="${1:-manual}"
DATA_DIR="${WEREAD_DATA_DIR:-/data/.weread}"
LOG_FILE="${DATA_DIR}/app.log"
LOCK_FILE="${DATA_DIR}/run.lock"
PID_FILE="${DATA_DIR}/run.pid"
STATE_FILE="${DATA_DIR}/last_run.json"
SCREENSHOT_KEEP="${SCREENSHOT_KEEP:-10}"

mkdir -p "$DATA_DIR"
touch "$LOG_FILE"

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { printf '[start_reading %s] %s\n' "$(ts)" "$*" >>"$LOG_FILE"; }

write_state() {
    # write_state <status> <extra-json-fields>
    local status="$1"
    local extra="${2:-}"
    local started_at="${STARTED_AT:-}"
    local ended_at="${ENDED_AT:-}"
    local exit_code="${EXIT_CODE:-}"
    {
        printf '{'
        printf '"status":"%s"' "$status"
        printf ',"trigger":"%s"' "$TRIGGER"
        [[ -n "$started_at" ]] && printf ',"started_at":"%s"' "$started_at"
        [[ -n "$ended_at"   ]] && printf ',"ended_at":"%s"' "$ended_at"
        [[ -n "$exit_code"  ]] && printf ',"exit_code":%s' "$exit_code"
        printf ',"duration_minutes":%s' "${WEREAD_DURATION:-68}"
        [[ -n "$extra" ]] && printf ',%s' "$extra"
        printf '}\n'
    } >"$STATE_FILE"
}

prune_screenshots() {
    # Keep only the most recent $SCREENSHOT_KEEP screenshot-*.png files.
    local count
    count=$(ls -1 "$DATA_DIR"/screenshot-*.png 2>/dev/null | wc -l)
    if [[ "$count" -gt "${SCREENSHOT_KEEP:-10}" ]]; then
        ls -1t "$DATA_DIR"/screenshot-*.png 2>/dev/null \
            | tail -n +"$((SCREENSHOT_KEEP + 1))" \
            | xargs -r rm -f
        log "pruned screenshots (kept $SCREENSHOT_KEEP)"
    fi
    # Cap selenium-logs-*.log to 5 newest as well.
    local lc
    lc=$(ls -1 "$DATA_DIR"/selenium-logs-*.log 2>/dev/null | wc -l)
    if [[ "$lc" -gt 5 ]]; then
        ls -1t "$DATA_DIR"/selenium-logs-*.log 2>/dev/null \
            | tail -n +6 \
            | xargs -r rm -f
    fi
}

# Acquire exclusive lock, non-blocking. fd 9 is closed automatically on exit.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "another reading run is in progress (lock held); skipping trigger=$TRIGGER"
    exit 0
fi

STARTED_AT="$(ts)"
echo $$ >"$PID_FILE"
write_state "running"
log "▶ starting weread-selenium-cli run (trigger=$TRIGGER, duration=${WEREAD_DURATION:-68}m)"

set +e
weread-selenium-cli run >>"$LOG_FILE" 2>&1
EXIT_CODE=$?
set -e

ENDED_AT="$(ts)"
if [[ "$EXIT_CODE" -eq 0 ]]; then
    write_state "completed"
    log "✓ run finished cleanly"
else
    write_state "failed"
    log "✗ run exited with code $EXIT_CODE"
fi

rm -f "$PID_FILE"
prune_screenshots
exit "$EXIT_CODE"
