#!/usr/bin/env bash
# Iterate every county in benchmark/ga_counties_to_sweep.txt and run
# benchmark/run_ga_county_sweep.sh on it. Logs per-county outcome to
# benchmark/results/ga_all_counties_<run-id>.log so we can resume.
#
# Usage:
#   benchmark/run_all_ga_counties.sh [start_index]
# Defaults to start at line 1 (skip blank/comment lines).

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIST="$ROOT/benchmark/ga_counties_to_sweep.txt"
LOG_DIR="$ROOT/benchmark/results/ga_all_counties_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOG_DIR"
START="${1:-1}"

i=0
while IFS= read -r line; do
  case "$line" in
    ""|"#"*) continue ;;
  esac
  i=$((i + 1))
  if [ "$i" -lt "$START" ]; then
    continue
  fi
  county="${line%%|*}"
  cities_raw="${line#*|}"
  IFS=';' read -r -a cities <<< "$cities_raw"

  echo "[$(date -u +%H:%M:%S)] [$i] $county (${#cities[@]} cities)" | tee -a "$LOG_DIR/index.log"
  bash "$ROOT/benchmark/run_ga_county_sweep.sh" "$county" "${cities[@]}" \
    > "$LOG_DIR/county_${county// /_}.log" 2>&1
  status=$?
  echo "[$(date -u +%H:%M:%S)] [$i] $county exit=$status" | tee -a "$LOG_DIR/index.log"
done < "$LIST"
echo "all-counties driver done" | tee -a "$LOG_DIR/index.log"
