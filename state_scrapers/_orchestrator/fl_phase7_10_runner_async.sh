#!/bin/bash
# Phase 7-10 runner: takes over after HERE recovery completes.
# Runs prepare -> import -> Phase 9 -> Phase 10.
#
# Activated by the presence of state_scrapers/fl/results/.../here.done
# AND state_scrapers/fl/results/.../subdpoly.done.

set +e
cd /Users/ngoshaliclarke/Documents/GitHub/hoaproxy

RUN_ID="fl_complete_20260510T025409Z"
RUN_DIR="state_scrapers/fl/results/$RUN_ID"
mkdir -p "$RUN_DIR"

set -a; source settings.env; set +a
LIVE_JWT_SECRET="$JWT_SECRET"

stamp() { date -u +%FT%TZ; }
log() { echo "[$(stamp)] $*" | tee -a "$RUN_DIR/orchestrator.log"; }

step() {
    local name="$1"; shift
    local flag="$RUN_DIR/${name}.done"
    if [ -f "$flag" ]; then
        log "SKIP $name (flag $flag exists)"
        return 0
    fi
    log "RUN  $name"
    if "$@"; then
        echo "$(stamp)" > "$flag"
        log "OK   $name"
        return 0
    else
        log "FAIL $name (continuing to next step)"
        return 0
    fi
}

# Wait for prerequisites: HERE recovery + subdpoly + audit.
log "phase7_10 runner: waiting for prerequisites"
while [ ! -f "$RUN_DIR/here.done" ] || [ ! -f "$RUN_DIR/subdpoly.done" ]; do
    sleep 60
done
log "prerequisites met (here.done + subdpoly.done present)"

# Final geometry normalization (idempotent).
run_normalize() {
    .venv/bin/python state_scrapers/fl/scripts/fl_normalize_geometry_for_prepare.py --apply \
        > /tmp/fl_normalize_final.log 2>&1
    cp /tmp/fl_normalize_final.log "$RUN_DIR/" 2>/dev/null
}
step normalize run_normalize

# Phase 7 — prepare.
run_prepare() {
    .venv/bin/python scripts/prepare_bank_for_ingest.py \
        --state FL \
        --limit 20000 \
        --max-docai-cost-usd 50 \
        --skip-geo-enrichment \
        --skip-page-one-review \
        --ledger "$RUN_DIR/prepared_ingest_ledger.jsonl" \
        --geo-cache "$RUN_DIR/prepared_ingest_geo_cache.json" \
        --bank-bucket hoaproxy-bank \
        --prepared-bucket hoaproxy-ingest-ready \
        > "$RUN_DIR/prepare.log" 2>&1
}
step prepare run_prepare

# Phase 8 — import loop (Phase 2 async; no pacing needed).
# Async cutover @ 2026-05-11T03:46Z: /admin/ingest-ready-gcs now enqueues
# into pending_ingest instead of running OCR/embedding synchronously, so
# the 75s pacing memo from the OOM era no longer applies. Worker drains
# at its own pace (~250ms/job observed in cutover smoke test).
run_import() {
    PASS=0
    while true; do
        PASS=$((PASS+1))
        RESULT=$(curl -sS -X POST "https://hoaproxy.org/admin/ingest-ready-gcs?state=FL&limit=50" \
            -H "Authorization: Bearer $LIVE_JWT_SECRET")
        if [ -z "$RESULT" ]; then
            log "import pass $PASS: empty response, retry in 10s"
            sleep 10
            continue
        fi
        # Phase 2 async response: {"async": true, "enqueued": N, "results": [...]}
        # Legacy fallback (if flag flipped back to 0): {"results": [...]} with sync content.
        # Both have .results array length = N enqueued or imported; both =0 means no ready bundles.
        ENQ=$(echo "$RESULT" | jq -r '.enqueued // (.results | length)' 2>/dev/null || echo 0)
        SKIPPED=$(echo "$RESULT" | jq -r '.skipped // 0 | if type=="array" then length else . end' 2>/dev/null || echo 0)
        log "import pass $PASS: enqueued=$ENQ skipped=$SKIPPED"
        echo "$RESULT" >> "$RUN_DIR/import_passes.jsonl"
        if [ "$ENQ" = "0" ]; then
            log "import: enqueued=0, exiting loop (worker will drain remaining queue)"
            break
        fi
        # Small backoff to avoid hammering — async enqueue is cheap but the
        # state-side list_ready_bundle_prefixes scans GCS, which costs $.
        sleep 5
    done
    # After exiting the enqueue loop, wait for the worker to drain.
    log "import: waiting for pending_ingest queue to empty for FL"
    DRAIN_PASS=0
    while true; do
        DRAIN_PASS=$((DRAIN_PASS+1))
        PENDING=$(curl -sS "https://hoaproxy.org/admin/ingest/queue-stats" \
            -H "Authorization: Bearer $LIVE_JWT_SECRET" \
            | jq -r '[.by_state[] | select(.state=="FL" and (.status=="pending" or .status=="in_progress")) | .n] | add // 0')
        log "import drain pass $DRAIN_PASS: FL pending+in_progress=$PENDING"
        if [ "$PENDING" = "0" ]; then
            log "import drain: FL queue empty"
            break
        fi
        if [ $DRAIN_PASS -gt 360 ]; then
            log "import drain: 30min cap hit; proceeding"
            break
        fi
        sleep 5
    done
}
step import run_import

# Phase 9 — location enrichment via OCR-text → HERE.
run_phase9() {
    .venv/bin/python scripts/enrich_locations_from_ocr_here.py \
        --state FL --apply \
        --bbox-json '{"min_lat":24.39,"max_lat":31.00,"min_lon":-87.65,"max_lon":-79.97}' \
        > "$RUN_DIR/phase9.log" 2>&1
}
step phase9 run_phase9

# Phase 10 — close.
run_phase10() {
    .venv/bin/python scripts/phase10_close.py \
        --state FL \
        --bbox-json '{"min_lat":24.39,"max_lat":31.00,"min_lon":-87.65,"max_lon":-79.97}' \
        --run-id "$RUN_ID" --apply \
        > "$RUN_DIR/phase10.log" 2>&1
}
step phase10 run_phase10

log "PHASE 7-10 RUNNER DONE"
