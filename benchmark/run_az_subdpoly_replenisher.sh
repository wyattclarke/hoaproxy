#!/usr/bin/env bash
# Replenisher for AZ Driver A' (subdpoly-anchored sweeps). Currently only
# Maricopa + Pima have subdpoly seeds, so this is a 2-county queue. Kept as a
# replenisher for symmetry with FL and to allow easy expansion if more AZ
# counties publish subdivision polygons later.

set -uo pipefail

PARALLELISM=2
while getopts "j:" opt; do
    case "$opt" in
        j) PARALLELISM="$OPTARG" ;;
    esac
done
shift $((OPTIND - 1))

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT/benchmark/results/az_subdpoly_replenish_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOG_DIR"
INDEX="$LOG_DIR/index.log"
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$INDEX"; }

log "log dir: $LOG_DIR"
log "parallelism target: $PARALLELISM"
log "queue ($#): $*"

count_running() {
    pgrep -f "run_az_subdpoly_county_sweep.sh" | wc -l | tr -d ' '
}

slug_to_display() {
    case "$1" in
        maricopa) echo "Maricopa" ;;
        pima)     echo "Pima" ;;
        *)        echo "" ;;
    esac
}

for slug in "$@"; do
    probe="$ROOT/benchmark/results/az_subdpoly_${slug}/probe.jsonl"
    if [ -s "$probe" ]; then
        log "SKIP $slug (probe.jsonl populated)"
        continue
    fi
    display=$(slug_to_display "$slug")
    if [ -z "$display" ]; then
        log "WARN: no display name for slug '$slug'"
        continue
    fi
    while [ "$(count_running)" -ge "$PARALLELISM" ]; do
        sleep 60
    done
    log "launching $slug ($display) [running=$(count_running)]"
    nohup bash "$ROOT/benchmark/run_az_subdpoly_county_sweep.sh" "$slug" "$display" 5000 \
        > "$LOG_DIR/sweep_${slug}.log" 2>&1 < /dev/null &
    disown
    sleep 5
done

log "queue exhausted; waiting for remaining sweeps to finish"
while [ "$(count_running)" -gt 0 ]; do
    sleep 60
done
log "ALL DONE"
