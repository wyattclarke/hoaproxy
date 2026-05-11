#!/bin/sh
# Phase 2 entrypoint — see docs/scaling-proposal.md §Phase 2.
#
# Runs the uvicorn web server AND the background ingest worker in the same
# Render container. The two processes share the persistent disk (Render
# doesn't support cross-service disk mounts as of 2026) but have separate
# Python heaps, so the OCR / embedding RAM spike in the worker can't OOM
# the request-serving process.
#
# When ASYNC_INGEST_ENABLED is unset/false, the worker still polls but the
# pending_ingest table stays empty (the web routes still run synchronously),
# so the worker just no-ops.
#
# To split into a true separate Render service later (e.g. after the Neon
# Postgres migration in Phase 4), remove the trap+wait below, replace this
# CMD with `uvicorn ...` only, and add a second Render `type: worker`
# service running `python -m hoaware.ingest_worker`.

set -e

cleanup() {
    echo "[start_web_with_worker] received signal, shutting down children"
    kill -TERM "$WORKER_PID" 2>/dev/null || true
    kill -TERM "$WEB_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
    wait "$WEB_PID" 2>/dev/null || true
    exit 0
}
trap cleanup TERM INT

PORT="${PORT:-8000}"

if [ "${INGEST_WORKER_ENABLED:-1}" = "1" ]; then
    echo "[start_web_with_worker] starting hoaware.ingest_worker"
    python -m hoaware.ingest_worker &
    WORKER_PID=$!
else
    echo "[start_web_with_worker] INGEST_WORKER_ENABLED=0 — skipping worker"
    WORKER_PID=""
fi

echo "[start_web_with_worker] starting uvicorn on port ${PORT}"
uvicorn api.main:app --host 0.0.0.0 --port "${PORT}" &
WEB_PID=$!

# If either child exits, propagate the exit code so Render restarts the
# container. The worker should never exit on its own (it loops forever),
# but if it crashes we want to know.
wait -n "$WEB_PID" "$WORKER_PID" 2>/dev/null || wait "$WEB_PID"
EXIT_CODE=$?
cleanup
exit "$EXIT_CODE"
