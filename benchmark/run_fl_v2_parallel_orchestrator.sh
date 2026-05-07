#!/usr/bin/env bash
# Parallel orchestrator: run up to N county sweeps concurrently from
# benchmark/fl_top_counties_v2.txt. Idempotent — skips counties whose
# fl_county_v2_<safe>/probe.jsonl is already non-empty.
#
# Usage:
#   benchmark/run_fl_v2_parallel_orchestrator.sh -j 3 [<County1> <County2> ...]
#
# If positional county names are given, only those counties run, in given
# order. If none, walks the full file. County matching is case-insensitive.
#
# Rationale for explicit order: pass geographically-dispersed counties first
# so the active set of N parallel sweeps covers different metros, minimizing
# host-family overlap and probe-host rate-limit collisions.

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
LOG_DIR="$ROOT/benchmark/results/fl_v2_parallel_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOG_DIR"
INDEX="$LOG_DIR/index.log"

log() { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$INDEX"; }

log "log dir: $LOG_DIR"
log "parallelism: $PARALLELISM"

# Build queue from positional args (if any) or full file.
declare -a QUEUE
if [ "$#" -gt 0 ]; then
    for arg in "$@"; do
        line=$(awk -F'|' -v target="$(echo "$arg" | tr '[:upper:]' '[:lower:]')" \
            'BEGIN{IGNORECASE=1} tolower($1)==target {print; exit}' "$LIST")
        if [ -n "$line" ]; then
            QUEUE+=("$line")
        else
            log "WARN: no match for '$arg' in $LIST"
        fi
    done
else
    while IFS= read -r line; do
        case "$line" in ""|"#"*) continue ;; esac
        QUEUE+=("$line")
    done < "$LIST"
fi

log "queue size: ${#QUEUE[@]}"

# Filter: skip counties whose probe.jsonl is already populated.
declare -a PENDING
for line in "${QUEUE[@]}"; do
    county="${line%%|*}"
    safe="${county// /_}"
    probe="$ROOT/benchmark/results/fl_county_v2_${safe}/probe.jsonl"
    if [ -s "$probe" ]; then
        log "SKIP $county (probe.jsonl already populated)"
        continue
    fi
    PENDING+=("$line")
done
log "pending: ${#PENDING[@]}"

declare -A PIDS  # pid -> county

reap() {
    # Reap any dead children. Returns nothing; mutates PIDS.
    local pid county_done rc
    for pid in "${!PIDS[@]}"; do
        if ! kill -0 "$pid" 2>/dev/null; then
            county_done="${PIDS[$pid]}"
            wait "$pid" 2>/dev/null
            rc=$?
            log "$county_done exit=$rc (pid $pid)"
            unset 'PIDS[$pid]'
        fi
    done
}

for line in "${PENDING[@]}"; do
    # Throttle to PARALLELISM concurrent.
    while [ "${#PIDS[@]}" -ge "$PARALLELISM" ]; do
        sleep 30
        reap
    done

    county="${line%%|*}"
    cities_raw="${line#*|}"
    IFS=';' read -r -a cities <<< "$cities_raw"
    safe="${county// /_}"

    log "launching $county (${#cities[@]} cities)"
    bash "$ROOT/benchmark/run_fl_county_sweep_v2.sh" "$county" "${cities[@]}" \
        > "$LOG_DIR/county_${safe}.log" 2>&1 &
    pid=$!
    PIDS["$pid"]="$county"
done

# Drain remaining children.
while [ "${#PIDS[@]}" -gt 0 ]; do
    sleep 30
    reap
done

log "ALL DONE"
