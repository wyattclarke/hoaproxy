#!/usr/bin/env bash
# AZ end-to-end orchestration. Runs after the initial four-driver sweeps have
# been kicked off. This script:
#
#   1. Waits until all initial sweep probes are populated (or timeout).
#   2. Runs Driver A' multipass for Maricopa (covers all 25k subdpoly names).
#   3. Runs Driver A' multipass for Pima.
#   4. Runs OCR audit + slug-repair pass.
#   5. Runs state-mismatch reroute pass.
#   6. Runs geometry stack (ZIP centroid → subdpoly polygon).
#   7. Runs prepare_bank_for_ingest.py.
#
# Each step is idempotent and resumable.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
LOG="$ROOT/state_scrapers/az/notes/full_pipeline_$(date -u +%Y%m%dT%H%M%SZ).log"
mkdir -p "$(dirname "$LOG")"
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG"; }

cd "$ROOT"
# Activate venv and load settings.env without tripping `set -u` on incidental
# unset shell variables in those scripts.
set +u
source .venv/bin/activate
set -a
[ -f "$ROOT/settings.env" ] && source "$ROOT/settings.env" 2>/dev/null
set +a
set -u
export GOOGLE_CLOUD_PROJECT=hoaware
export PYTHONUNBUFFERED=1

# ---------------------------------------------------------------------------
# Step 1: wait for initial sweep probes
# ---------------------------------------------------------------------------
EXPECTED_PROBES=(
    "benchmark/results/az_mgmt_host/probe.jsonl"
    "benchmark/results/az_master_planned/probe.jsonl"
    "benchmark/results/az_subdpoly_maricopa/probe.jsonl"
    "benchmark/results/az_subdpoly_pima/probe.jsonl"
    "benchmark/results/az_county_v2_maricopa-phoenix-core/probe.jsonl"
    "benchmark/results/az_county_v2_maricopa-east-valley/probe.jsonl"
    "benchmark/results/az_county_v2_maricopa-west-valley/probe.jsonl"
    "benchmark/results/az_county_v2_maricopa-southeast/probe.jsonl"
    "benchmark/results/az_county_v2_pima-tucson-core/probe.jsonl"
    "benchmark/results/az_county_v2_pima-suburban/probe.jsonl"
    "benchmark/results/az_county_v2_pinal/probe.jsonl"
    "benchmark/results/az_county_v2_yavapai/probe.jsonl"
    "benchmark/results/az_county_v2_mohave/probe.jsonl"
    "benchmark/results/az_county_v2_yuma/probe.jsonl"
    "benchmark/results/az_county_v2_coconino/probe.jsonl"
    "benchmark/results/az_county_v2_cochise/probe.jsonl"
    "benchmark/results/az_county_v2_gila/probe.jsonl"
    "benchmark/results/az_county_v2_la-paz/probe.jsonl"
    "benchmark/results/az_county_v2_apache/probe.jsonl"
    "benchmark/results/az_county_v2_navajo/probe.jsonl"
    "benchmark/results/az_county_v2_greenlee/probe.jsonl"
    "benchmark/results/az_county_v2_graham/probe.jsonl"
    "benchmark/results/az_county_v2_santa-cruz/probe.jsonl"
)

wait_initial_sweeps() {
    log "Waiting for initial sweep probes (${#EXPECTED_PROBES[@]} expected)..."
    local start=$(date +%s)
    local timeout=21600  # 6h
    while true; do
        local done=0
        for p in "${EXPECTED_PROBES[@]}"; do
            [ -s "$p" ] && done=$((done + 1))
        done
        log "  initial sweeps: $done/${#EXPECTED_PROBES[@]} probes populated"
        if [ "$done" -ge "${#EXPECTED_PROBES[@]}" ]; then
            log "  all initial sweeps done"
            return 0
        fi
        local now=$(date +%s)
        if [ "$((now - start))" -gt "$timeout" ]; then
            log "  timeout reached, proceeding anyway with $done/${#EXPECTED_PROBES[@]}"
            return 0
        fi
        sleep 300
    done
}

# ---------------------------------------------------------------------------
# Step 2 & 3: Driver A' multipass (Maricopa and Pima)
# ---------------------------------------------------------------------------
run_multipass() {
    # Run Maricopa and Pima multipasses in PARALLEL (each multipass internally
    # sequences its passes; running both at once doubles aggregate throughput
    # without stretching Serper concurrency much — Pima has only 2 remaining
    # passes vs Maricopa's 9).
    # Start at pass 2 since the regular replenisher already ran pass 1 (offset=0,
    # names 0..2499) under benchmark/results/az_subdpoly_<slug>/.
    log "=== Driver A' multipass (parallel): Maricopa (25,201 names) + Pima (5,744 names) starting at pass 2 ==="
    bash "$ROOT/benchmark/run_az_subdpoly_multipass.sh" maricopa Maricopa 25201 2500 2 \
        > "$ROOT/state_scrapers/az/results/az_multipass_maricopa.log" 2>&1 &
    MULTIPASS_M_PID=$!
    bash "$ROOT/benchmark/run_az_subdpoly_multipass.sh" pima Pima 5744 2500 2 \
        > "$ROOT/state_scrapers/az/results/az_multipass_pima.log" 2>&1 &
    MULTIPASS_P_PID=$!
    log "  maricopa multipass pid=$MULTIPASS_M_PID, pima multipass pid=$MULTIPASS_P_PID"
    wait $MULTIPASS_M_PID
    log "  maricopa multipass exit=$?"
    wait $MULTIPASS_P_PID
    log "  pima multipass exit=$?"
}

# ---------------------------------------------------------------------------
# Step 4: OCR enrichment (audit then run)
# ---------------------------------------------------------------------------
run_ocr() {
    log "=== OCR audit ==="
    python state_scrapers/az/scripts/az_bank_ocr_enrich.py audit \
        --out state_scrapers/az/results/az_ocr_audit.json 2>&1 | tee -a "$LOG"
    log "=== OCR full run (max-cost-usd=200) ==="
    python state_scrapers/az/scripts/az_bank_ocr_enrich.py run --max-cost-usd 200 2>&1 | tee -a "$LOG"
}

# ---------------------------------------------------------------------------
# Step 5: state-mismatch reroute
# ---------------------------------------------------------------------------
run_reroute() {
    log "=== State-mismatch reroute ==="
    # Pass --apply explicitly when ready; default is dry-run
    python state_scrapers/az/scripts/az_reroute_state_mismatches.py 2>&1 | tee -a "$LOG"
}

# ---------------------------------------------------------------------------
# Step 6: geometry stack
# ---------------------------------------------------------------------------
run_geometry() {
    log "=== Geometry E4: ZIP centroid baseline ==="
    python state_scrapers/az/scripts/az_enrich_with_zip_centroid.py --apply 2>&1 | tee -a "$LOG"
    log "=== Geometry E3: OSM Nominatim (free, ~1.5 req/s) ==="
    # OSM is slow but yields polygons for known places; cap with --limit so we
    # don't run for 24h on a stale enrichment pass.
    python state_scrapers/az/scripts/az_enrich_manifests_with_osm.py --limit 10000 2>&1 | tee -a "$LOG" || log "OSM pass failed/timed out — continuing"
    log "=== Geometry E2: HERE address-precision (free 30k/mo) ==="
    if [ -n "${HERE_API_KEY:-}" ]; then
        python state_scrapers/az/scripts/az_enrich_with_here.py --audit 2>&1 | tee -a "$LOG"
        python state_scrapers/az/scripts/az_enrich_with_here.py --run --rate-per-sec 4 2>&1 | tee -a "$LOG" || log "HERE pass failed — continuing"
    else
        log "  HERE_API_KEY not set; skipping HERE pass"
    fi
    log "=== Geometry E1: subdpoly polygon cross-link (overwrites lower tiers) ==="
    python state_scrapers/az/scripts/az_enrich_with_subdpoly.py --apply 2>&1 | tee -a "$LOG"
    log "=== Pruning misrouted slugs (non-AZ counties) ==="
    python state_scrapers/az/scripts/az_prune_misrouted.py --apply 2>&1 | tee -a "$LOG" || log "prune failed/timed out — continuing"
}

# ---------------------------------------------------------------------------
# Step 7: prepare bank for ingest
# ---------------------------------------------------------------------------
run_prepare() {
    log "=== Prepare bank for ingest (state=AZ) ==="
    python scripts/prepare_bank_for_ingest.py \
        --state AZ \
        --limit 50000 \
        --max-docai-cost-usd 100 \
        --ledger "$ROOT/state_scrapers/az/results/az_prepared_ingest_ledger.jsonl" \
        --geo-cache "$ROOT/state_scrapers/az/results/az_prepared_ingest_geo_cache.json" \
        2>&1 | tee -a "$LOG"
}

# ---------------------------------------------------------------------------
# Step 8: import prepared bundles to live (calls /admin/ingest-ready-gcs in loop)
# ---------------------------------------------------------------------------
run_import_to_live() {
    log "=== Import prepared bundles to live ==="
    BASE_URL="${HOAPROXY_LIVE_BASE_URL:-https://hoaproxy.org}"
    # Prefer explicit HOAPROXY_ADMIN_BEARER, then fetch live JWT_SECRET via the
    # Render API (local settings.env JWT_SECRET differs from prod's), and only
    # fall back to local JWT_SECRET if neither path works.
    TOKEN="${HOAPROXY_ADMIN_BEARER:-}"
    if [ -z "$TOKEN" ] && [ -n "${RENDER_API_KEY:-}" ] && [ -n "${RENDER_SERVICE_ID:-}" ]; then
        log "  fetching prod JWT_SECRET via Render API..."
        TOKEN=$(curl -sS -H "Authorization: Bearer $RENDER_API_KEY" \
            "https://api.render.com/v1/services/$RENDER_SERVICE_ID/env-vars" \
            --max-time 30 | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    for env in data:
        e = env.get('envVar', env)
        if e.get('key') == 'JWT_SECRET':
            print(e.get('value') or '')
            break
except Exception:
    pass
" 2>/dev/null || echo "")
        if [ -n "$TOKEN" ]; then
            log "  got prod JWT_SECRET (len=${#TOKEN})"
        else
            log "  Render API fetch failed; falling back to local JWT_SECRET"
            TOKEN="${JWT_SECRET:-}"
        fi
    fi
    if [ -z "$TOKEN" ]; then
        log "  no admin token available; skipping import"
        return 0
    fi
    local imported_total=0
    for i in $(seq 1 200); do
        response=$(curl -sS -X POST "$BASE_URL/admin/ingest-ready-gcs?state=AZ&limit=50" \
            -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
            --max-time 900 --connect-timeout 30 || echo '{"error":"curl_failed"}')
        log "  import loop $i: $(echo "$response" | python3 -c 'import json,sys; r=json.loads(sys.stdin.read()); print(f"found={r.get(\"found\")} results={len(r.get(\"results\") or [])}")' 2>/dev/null || echo "[parse error]")"
        # Count imported in this loop
        imported_now=$(echo "$response" | python3 -c "
import json, sys
try:
    r = json.loads(sys.stdin.read())
    results = r.get('results') or []
    print(sum(1 for x in results if (x.get('status') or '').lower() == 'imported'))
except Exception:
    print(0)
" 2>/dev/null || echo 0)
        imported_total=$((imported_total + imported_now))
        # Stop if no more found
        found=$(echo "$response" | python3 -c "
import json, sys
try:
    r = json.loads(sys.stdin.read())
    print(r.get('found') or 0)
except Exception:
    print(0)
" 2>/dev/null || echo 0)
        if [ "$found" = "0" ]; then
            log "  import drain complete after $i loops; total imported=$imported_total"
            break
        fi
        sleep 5  # tiny gap between import calls
    done
}

# ---------------------------------------------------------------------------
# Step 9: Phase 10 close (LLM rename, hard-delete junk, audit)
# ---------------------------------------------------------------------------
run_phase10() {
    log "=== Phase 10 close ==="
    python scripts/phase10_close.py \
        --state AZ \
        --bbox-json '{"min_lat":31.3,"max_lat":37.0,"min_lon":-114.9,"max_lon":-109.0}' \
        --run-id "az_$(date -u +%Y%m%d_%H%M%S)" \
        2>&1 | tee -a "$LOG"
}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
log "=== AZ FULL PIPELINE START ==="
wait_initial_sweeps
run_multipass
run_ocr
run_reroute
run_geometry
run_prepare
run_import_to_live
run_phase10
log "=== AZ FULL PIPELINE COMPLETE ==="
