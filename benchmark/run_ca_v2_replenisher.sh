#!/usr/bin/env bash
# CA parallel replenisher: maintain N concurrent run_ca_county_sweep_v2.sh
# processes by drawing from a queue passed as positional args. Modeled on
# run_fl_v2_replenisher.sh.
#
# Counties on command line. Cities looked up from
# benchmark/ca_top_counties_v2.txt by exact county-name match.
#
# Usage:
#   benchmark/run_ca_v2_replenisher.sh -j 3 <County1> <County2> ...
#   benchmark/run_ca_v2_replenisher.sh -j 4 -d corp <County1> <County2> ...
#     (-d corp uses run_ca_corp_county_sweep.sh instead of v2; pass <slug>)

set -uo pipefail

PARALLELISM=3
DRIVER="v2"  # v2 = Driver B; corp = Driver A
while getopts "j:d:" opt; do
    case "$opt" in
        j) PARALLELISM="$OPTARG" ;;
        d) DRIVER="$OPTARG" ;;
    esac
done
shift $((OPTIND - 1))

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIST="$ROOT/benchmark/ca_top_counties_v2.txt"

case "$DRIVER" in
    v2)   SCRIPT="run_ca_county_sweep_v2.sh"; PROBE_PREFIX="ca_county_v2_" ;;
    corp) SCRIPT="run_ca_corp_county_sweep.sh"; PROBE_PREFIX="ca_corp_" ;;
    *) echo "ERROR: unknown driver '$DRIVER' (expected v2|corp)"; exit 1 ;;
esac

LOG_DIR="$ROOT/benchmark/results/ca_${DRIVER}_replenish_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOG_DIR"
INDEX="$LOG_DIR/index.log"
log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$INDEX"; }

log "log dir: $LOG_DIR"
log "driver: $DRIVER  script: $SCRIPT  probe-prefix: $PROBE_PREFIX"
log "parallelism target: $PARALLELISM"
log "queue ($#): $*"

count_running() {
    pgrep -f "$SCRIPT" | wc -l | tr -d ' '
}

for county in "$@"; do
    safe="${county// /_}"
    probe="$ROOT/benchmark/results/${PROBE_PREFIX}${safe}/probe.jsonl"
    if [ -s "$probe" ]; then
        log "SKIP $county (probe.jsonl populated)"
        continue
    fi
    while [ "$(count_running)" -ge "$PARALLELISM" ]; do
        sleep 60
    done
    if [ "$DRIVER" = "v2" ]; then
        line=$(grep -i "^${county}|" "$LIST" || true)
        if [ -z "$line" ]; then
            log "WARN: no row for '$county' in $LIST (skipping)"
            continue
        fi
        cities_raw="${line#*|}"
        IFS=';' read -r -a cities <<< "$cities_raw"
        log "launching $county (${#cities[@]} cities) [running=$(count_running)]"
        nohup bash "$ROOT/benchmark/$SCRIPT" "$county" "${cities[@]}" \
            > "$LOG_DIR/county_${safe}.log" 2>&1 < /dev/null &
        disown
    else
        # corp driver: needs slug (lowercase-hyphenated) and display name.
        slug=$(echo "$county" | tr '[:upper:] ' '[:lower:]-')
        log "launching $county slug=$slug [running=$(count_running)]"
        nohup bash "$ROOT/benchmark/$SCRIPT" "$slug" "$county" \
            > "$LOG_DIR/county_${safe}.log" 2>&1 < /dev/null &
        disown
    fi
    sleep 5
done

log "queue exhausted; waiting for remaining sweeps to finish"
while [ "$(count_running)" -gt 0 ]; do
    sleep 60
done
log "ALL DONE"
