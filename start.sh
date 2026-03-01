#!/bin/bash
# ═══════════════════════════════════════════════
# ARKAINBRAIN — Single-container entrypoint
# Starts web server + background job worker
# Works with or without Redis
# ═══════════════════════════════════════════════

set -e

PORT=${PORT:-8080}
LOG_DIR=${LOG_DIR:-./logs}
mkdir -p "$LOG_DIR" output data/regulations/us_states

echo "═══════════════════════════════════════════════"
echo "  ARKAINBRAIN v6 — Starting"
echo "  PORT=$PORT"
echo "  DB=${DATABASE_URL:+PostgreSQL}${DATABASE_URL:-SQLite}"
echo "  Queue=${REDIS_URL:+Redis}${REDIS_URL:-Subprocess}"
echo "═══════════════════════════════════════════════"

# Suppress CrewAI tracing prompts
export CREWAI_TELEMETRY_OPT_OUT=true
export OTEL_SDK_DISABLED=true
export CREWAI_TRACING_ENABLED=false
export DO_NOT_TRACK=1

# ── If Redis is available, start an RQ worker in background ──
if [ -n "$REDIS_URL" ]; then
    echo "[start] Redis detected — launching RQ worker..."
    python3 -m rq.cli worker default \
        --url "$REDIS_URL" \
        --path /app \
        --with-scheduler \
        >> "$LOG_DIR/rq_worker.log" 2>&1 &
    echo "[start] RQ worker PID=$!"
else
    echo "[start] No Redis — using subprocess workers"
    echo "[start] Worker logs → $LOG_DIR/worker_*.log"
fi

# ── Cleanup handler: kill background jobs on shutdown ──
cleanup() {
    echo "[start] Shutting down..."
    kill $(jobs -p) 2>/dev/null || true
    wait
}
trap cleanup SIGTERM SIGINT

# ── Start gunicorn (foreground — keeps container alive) ──
echo "[start] Launching gunicorn on port $PORT..."
exec gunicorn web_app:app \
    --bind "0.0.0.0:$PORT" \
    --workers 1 \
    --threads 8 \
    --timeout 900 \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --max-requests 500 \
    --max-requests-jitter 50 \
    --access-logfile - \
    --error-logfile -
