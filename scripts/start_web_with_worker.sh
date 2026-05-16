#!/bin/sh
# Container entrypoint: uvicorn web + N ingest workers in one process tree.
#
# `INGEST_WORKER_COUNT` controls fan-out (default 1). With the v2 prepared
# bundle path live, per-doc compute on the server is near-zero (~50 ms);
# the bottleneck is GCS sidecar fetch + SQLite write. Multiple workers
# pipeline the GCS step while serializing on the SQLite write lock under
# WAL mode. Empirical sweet spot for the CCX23 (4 vCPU) is N=4.
#
# Safety: `db.claim_next_pending_ingest` does a guarded UPDATE WHERE
# status='pending' so racing workers never claim the same job. GCS-side
# claim uses if_generation_match CAS on each bundle's status.json.
#
# Container restart on child exit is preserved — if any worker crashes,
# the script returns its exit code so docker-compose restarts the
# container (and all siblings come back).

set -e

WORKER_PIDS=""

cleanup() {
    echo "[start_web_with_worker] received signal, shutting down children"
    for pid in $WORKER_PIDS; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    kill -TERM "$WEB_PID" 2>/dev/null || true
    for pid in $WORKER_PIDS; do
        wait "$pid" 2>/dev/null || true
    done
    wait "$WEB_PID" 2>/dev/null || true
    exit 0
}
trap cleanup TERM INT

PORT="${PORT:-8000}"
WORKER_COUNT="${INGEST_WORKER_COUNT:-1}"

if [ "${INGEST_WORKER_ENABLED:-1}" = "1" ]; then
    i=1
    while [ "$i" -le "$WORKER_COUNT" ]; do
        echo "[start_web_with_worker] starting hoaware.ingest_worker #$i"
        INGEST_WORKER_ID="$i" python -m hoaware.ingest_worker &
        WORKER_PIDS="$WORKER_PIDS $!"
        i=$((i + 1))
    done
else
    echo "[start_web_with_worker] INGEST_WORKER_ENABLED=0 — skipping workers"
fi

echo "[start_web_with_worker] starting uvicorn on port ${PORT}"
# --proxy-headers + --forwarded-allow-ips makes uvicorn trust X-Forwarded-For
# from Caddy on 127.0.0.1. Caddy rewrites X-Forwarded-For to the real
# CF-Connecting-IP (see deploy/Caddyfile), so request.client.host ends up as
# the actual visitor IP. Without these flags it stays 127.0.0.1 for every
# request and the per-IP rate limiter in api/main.py treats the whole internet
# as a single user, locking out random visitors with HTTP 429.
uvicorn api.main:app --host 0.0.0.0 --port "${PORT}" \
    --proxy-headers --forwarded-allow-ips="127.0.0.1" &
WEB_PID=$!

# If any child exits, propagate the exit code so docker-compose restarts.
# `wait -n` returns when the *first* child exits; we treat that as fatal.
wait -n "$WEB_PID" $WORKER_PIDS 2>/dev/null || wait "$WEB_PID"
EXIT_CODE=$?
cleanup
exit "$EXIT_CODE"
