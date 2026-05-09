#!/usr/bin/env bash
# entrypoint.sh — Boot Xvfb, prepare data dir, launch Flask, kick off first reading.
set -euo pipefail

DATA_DIR="${WEREAD_DATA_DIR:-/data/.weread}"
DISPLAY_NUM="${DISPLAY:-:99}"
PORT="${PORT:-7860}"
LOG_FILE="${DATA_DIR}/app.log"
ENTRYPOINT_LOG="${DATA_DIR}/entrypoint.log"

log() {
    printf '[entrypoint %(%Y-%m-%dT%H:%M:%S%z)T] %s\n' -1 "$*" | tee -a "$ENTRYPOINT_LOG" >&2
}

# 1. Prepare data dir (HF Persistent Storage mounts at /data).
mkdir -p "$DATA_DIR"
touch "$LOG_FILE" "$ENTRYPOINT_LOG"

log "data dir = $DATA_DIR"
log "display  = $DISPLAY_NUM"
log "port     = $PORT"

# 2. Start Xvfb on $DISPLAY (e.g. :99).
SCREEN_GEOM="${XVFB_SCREEN:-1920x1080x24}"
Xvfb "$DISPLAY_NUM" -screen 0 "$SCREEN_GEOM" -nolisten tcp -ac >>"$ENTRYPOINT_LOG" 2>&1 &
XVFB_PID=$!
log "Xvfb started (pid=$XVFB_PID, screen=$SCREEN_GEOM)"

# Wait for the X server to be ready.
for i in $(seq 1 30); do
    if xdpyinfo -display "$DISPLAY_NUM" >/dev/null 2>&1; then
        log "Xvfb is ready"
        break
    fi
    sleep 0.5
    if [[ $i -eq 30 ]]; then
        log "WARNING: xdpyinfo never returned, continuing anyway"
    fi
done

# 3. Kick off first reading in the background (non-blocking, lock-protected).
log "scheduling initial reading run"
(
    sleep 5
    /app/start_reading.sh "initial" >>"$LOG_FILE" 2>&1 || true
) &

# 4. Launch Flask in the foreground (PID 1's child) — keeps the container alive.
log "launching Flask on 0.0.0.0:$PORT"
exec python3 /app/app.py
