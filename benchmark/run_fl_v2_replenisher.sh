#!/usr/bin/env bash
# Maintain N concurrent run_fl_county_sweep_v2.sh processes by drawing from
# a queue passed as positional args. When the running count drops below N,
# launch the next pending county. Idempotent: skips any county whose
# fl_county_v2_<safe>/probe.jsonl is already populated.
#
# Counties given on the command line. Cities are looked up from
# benchmark/fl_top_counties_v2.txt by exact county-name match (case-sens).
#
# Usage:
#   benchmark/run_fl_v2_replenisher.sh -j 3 <County1> <County2> ...

set -uo pipefail

PARALLELISM=3
while getopts "j:" opt; do
    case "$opt" in
        j) PARALLELISM="$OPTARG" ;;
    esac
done
shift $((OPTIND - 1))

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIST="$ROOT/benchmark/fl_top_counties_v2.txt"
LOG_DIR="$ROOT/benchmark/results/fl_v2_replenish_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOG_DIR"
INDEX="$LOG_DIR/index.log"
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$INDEX"; }

log "log dir: $LOG_DIR"
log "parallelism target: $PARALLELISM"
log "queue ($#): $*"

count_running() {
    pgrep -f "run_fl_county_sweep_v2.sh" | wc -l | tr -d ' '
}

for county in "$@"; do
    safe="${county// /_}"
    probe="$ROOT/benchmark/results/fl_county_v2_${safe}/probe.jsonl"
    if [ -s "$probe" ]; then
        log "SKIP $county (probe.jsonl populated)"
        continue
    fi
    # Wait until running count is below target.
    while [ "$(count_running)" -ge "$PARALLELISM" ]; do
        sleep 60
    done
    # Look up cities row.
    line=$(grep -i "^${county}|" "$LIST" || true)
    if [ -z "$line" ]; then
        log "WARN: no row for '$county' in $LIST"
        continue
    fi
    cities_raw="${line#*|}"
    IFS=';' read -r -a cities <<< "$cities_raw"
    log "launching $county (${#cities[@]} cities) [running=$(count_running)]"
    nohup bash "$ROOT/benchmark/run_fl_county_sweep_v2.sh" "$county" "${cities[@]}" \
        > "$LOG_DIR/county_${safe}.log" 2>&1 < /dev/null &
    disown
    sleep 5
done

# Wait for the last batch.
log "queue exhausted; waiting for remaining sweeps to finish"
while [ "$(count_running)" -gt 0 ]; do
    sleep 60
done
log "ALL DONE"
