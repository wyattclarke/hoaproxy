#!/usr/bin/env bash
# Orchestrate the NY discovery drivers, launching independent drivers in
# parallel and Serper-using drivers serially with the N=2 concurrency cap.
#
# Drivers:
#   A — registry-name × per-county Serper (top 13 NY counties)
#   B — county-broad keyword Serper (top 10 NY counties, NYC-aware phrasing)
#   C — tri-state mgmt-co host sweep (NY+NJ+CT joint, post-OCR routing)
#   E — ACRIS direct PDF fetch (NYC 4 boroughs, no Serper, runs in parallel)
#
# Usage:
#   bash state_scrapers/ny/scripts/launch_ny_drivers.sh [--dry-run]
#
# All drivers log to state_scrapers/ny/results/ny_20260510_213944Z_claude/.

set -uo pipefail

DRY=""
if [ "${1:-}" = "--dry-run" ]; then
  DRY="echo [DRY] would run:"
fi

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
RUN_ID="ny_20260510_213944Z_claude"
RUN_DIR="$ROOT/state_scrapers/ny/results/$RUN_ID"
mkdir -p "$RUN_DIR"

INDEX="$RUN_DIR/launch.log"
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$INDEX"; }

log "run dir: $RUN_DIR"
log "starting NY driver launch"

# ---------------------------------------------------------------------------
# Driver A — top 13 NY counties (by registry-seed density)
# ---------------------------------------------------------------------------
DRIVER_A_QUEUE=(
  "Kings"            # Brooklyn — 2,354 seeds, 1,020 coops
  "New York"         # Manhattan — 2,835 seeds, 2,121 coops
  "Queens"           # 1,134 seeds
  "Richmond"         # Staten Island — 949 seeds (gets registry-only, no ACRIS)
  "Westchester"      # 760 seeds
  "Nassau"           # 756 seeds
  "Suffolk"          # 672 seeds
  "Bronx"            # 377 seeds
  "Rockland"         # 213 seeds
  "Monroe"           # 202 seeds (Rochester)
  "Albany"           # 198 seeds
  "Erie"             # 195 seeds (Buffalo)
  "Onondaga"         # 117 seeds (Syracuse)
)

# ---------------------------------------------------------------------------
# Driver B — top 10 NY counties (by total HOA density × population)
# ---------------------------------------------------------------------------
DRIVER_B_QUEUE=(
  "Kings"
  "New York"
  "Queens"
  "Richmond"
  "Westchester"
  "Nassau"
  "Suffolk"
  "Bronx"
  "Rockland"
  "Monroe"
)

log "Driver A queue (${#DRIVER_A_QUEUE[@]} counties): ${DRIVER_A_QUEUE[*]}"
log "Driver B queue (${#DRIVER_B_QUEUE[@]} counties): ${DRIVER_B_QUEUE[*]}"

# ---------------------------------------------------------------------------
# Launch Driver A replenisher (N=2 parallel)
# ---------------------------------------------------------------------------
log "launching Driver A replenisher (N=2 parallel)"
$DRY nohup bash "$ROOT/benchmark/run_ny_replenisher.sh" -j 2 -d registry \
    "${DRIVER_A_QUEUE[@]}" \
    > "$RUN_DIR/driver_a.log" 2>&1 < /dev/null &
DRIVER_A_PID=$!
disown
log "Driver A PID: $DRIVER_A_PID"

# ---------------------------------------------------------------------------
# Stagger 3min, then launch Driver C (one-shot tri-state mgmt-co sweep).
# This is light — ~200 queries total — so it shouldn't materially compete
# with Driver A's per-county slots.
# ---------------------------------------------------------------------------
sleep 180
log "launching Driver C (tri-state mgmt-co sweep)"
$DRY nohup bash "$ROOT/benchmark/run_ny_tristate_mgmt_host_sweep.sh" \
    > "$RUN_DIR/driver_c.log" 2>&1 < /dev/null &
DRIVER_C_PID=$!
disown
log "Driver C PID: $DRIVER_C_PID"

# ---------------------------------------------------------------------------
# Driver B will be launched AFTER Driver A finishes (or in a second phase).
# It uses the same Serper budget so running both concurrently doubles
# rate-limit pressure. Sequential is safer.
# ---------------------------------------------------------------------------

log "Driver A + C running; B queued for after A completes"
log "ACRIS Driver E runs separately via fetch_acris_pdf.py once that script ships"
log "monitor: ls state_scrapers/ny/results/$RUN_ID/  +  gsutil ls gs://hoaproxy-bank/v1/NY/"
