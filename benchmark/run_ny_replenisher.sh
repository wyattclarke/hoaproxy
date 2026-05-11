#!/usr/bin/env bash
# Parallel replenisher for NY Driver A. Maintains N concurrent county sweeps
# from a queue, idempotent on existing probe.jsonl files. Adapted from
# run_fl_v2_replenisher.sh for NY.
#
# Default parallelism is N=2 (vs FL's N=3) so CA/AZ scrapes running in
# parallel have Serper rate-limit headroom.
#
# Usage:
#   benchmark/run_ny_replenisher.sh -j 2 "Kings" "Queens" "New York" "Bronx" ...

set -uo pipefail

PARALLELISM=2
DRIVER="registry"
while getopts "j:d:" opt; do
    case "$opt" in
        j) PARALLELISM="$OPTARG" ;;
        d) DRIVER="$OPTARG" ;;
    esac
done
shift $((OPTIND - 1))

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT/benchmark/results/ny_${DRIVER}_replenish_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOG_DIR"
INDEX="$LOG_DIR/index.log"
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$INDEX"; }

case "$DRIVER" in
    registry) SWEEP="$ROOT/benchmark/run_ny_registry_county_sweep.sh" ; PROBE_PREFIX="ny_registry" ;;
    broad)    SWEEP="$ROOT/benchmark/run_ny_county_sweep_v2.sh"        ; PROBE_PREFIX="ny_county_v2" ;;
    *) echo "unknown driver: $DRIVER (registry|broad)" >&2 ; exit 2 ;;
esac

if [ ! -x "$SWEEP" ] && ! [ -r "$SWEEP" ]; then
    echo "sweep script missing: $SWEEP" >&2
    exit 2
fi

log "log dir: $LOG_DIR"
log "driver: $DRIVER  sweep: $SWEEP"
log "parallelism target: $PARALLELISM"
log "queue ($#): $*"

count_running() {
    pgrep -f "$(basename "$SWEEP")" | wc -l | tr -d ' '
}

for county in "$@"; do
    safe="${county// /_}"
    probe="$ROOT/benchmark/results/${PROBE_PREFIX}_${safe}/probe.jsonl"
    if [ -s "$probe" ]; then
        log "SKIP $county (probe.jsonl populated)"
        continue
    fi
    while [ "$(count_running)" -ge "$PARALLELISM" ]; do
        sleep 60
    done
    log "launching $county [running=$(count_running)]"
    nohup bash "$SWEEP" "$county" \
        > "$LOG_DIR/county_${safe}.log" 2>&1 < /dev/null &
    disown
done

log "all $# counties dispatched; waiting for outstanding sweeps to finish"
while [ "$(count_running)" -gt 0 ]; do
    sleep 60
done
log "replenisher done"
