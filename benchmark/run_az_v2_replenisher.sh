#!/usr/bin/env bash
# Replenisher for AZ Driver B (county/sub-county broad sweeps).
# Maintains N concurrent run_az_county_sweep_v2.sh processes from a queue
# of sweep slugs (one of: maricopa-phoenix-core, pima-suburban, pinal, ...).
# City list looked up from benchmark/az_top_counties_v2.txt by exact slug.
#
# Idempotent: skips sweeps whose probe.jsonl is populated.
#
# Usage:
#   benchmark/run_az_v2_replenisher.sh -j 3 maricopa-phoenix-core pima-tucson-core pinal yavapai

set -uo pipefail

PARALLELISM=3
while getopts "j:" opt; do
    case "$opt" in
        j) PARALLELISM="$OPTARG" ;;
    esac
done
shift $((OPTIND - 1))

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIST="$ROOT/benchmark/az_top_counties_v2.txt"
LOG_DIR="$ROOT/benchmark/results/az_v2_replenish_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOG_DIR"
INDEX="$LOG_DIR/index.log"
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$INDEX"; }

log "log dir: $LOG_DIR"
log "parallelism target: $PARALLELISM"
log "queue ($#): $*"

count_running() {
    pgrep -f "run_az_county_sweep_v2.sh" | wc -l | tr -d ' '
}

for slug in "$@"; do
    probe="$ROOT/benchmark/results/az_county_v2_${slug}/probe.jsonl"
    if [ -s "$probe" ]; then
        log "SKIP $slug (probe.jsonl populated)"
        continue
    fi
    line=$(grep "^${slug}|" "$LIST" || true)
    if [ -z "$line" ]; then
        log "WARN: no row for slug '$slug' in $LIST"
        continue
    fi
    rest="${line#*|}"  # strip slug
    county="${rest%%|*}"  # county display
    cities_raw="${rest#*|}"
    IFS=';' read -r -a cities <<< "$cities_raw"

    while [ "$(count_running)" -ge "$PARALLELISM" ]; do
        sleep 60
    done
    log "launching $slug (county=$county, ${#cities[@]} cities) [running=$(count_running)]"
    nohup bash "$ROOT/benchmark/run_az_county_sweep_v2.sh" "$slug" "$county" "${cities[@]}" \
        > "$LOG_DIR/sweep_${slug}.log" 2>&1 < /dev/null &
    disown
    sleep 5
done

log "queue exhausted; waiting for remaining sweeps to finish"
while [ "$(count_running)" -gt 0 ]; do
    sleep 60
done
log "ALL DONE"
