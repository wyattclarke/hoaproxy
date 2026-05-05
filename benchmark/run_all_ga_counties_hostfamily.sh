#!/usr/bin/env bash
# Loop benchmark/run_ga_county_hostfamily.sh over the top GA counties
# in benchmark/ga_top_counties_hostfamily.txt.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIST="$ROOT/benchmark/ga_top_counties_hostfamily.txt"
LOG_DIR="$ROOT/benchmark/results/ga_hf_all_$(date -u +%Y%m%dT%H%M%SZ)"
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

  echo "[$(date -u +%H:%M:%S)] [$i] $county hf (${#cities[@]} cities)" | tee -a "$LOG_DIR/index.log"
  bash "$ROOT/benchmark/run_ga_county_hostfamily.sh" "$county" "${cities[@]}" \
    > "$LOG_DIR/county_${county// /_}.log" 2>&1
  status=$?
  echo "[$(date -u +%H:%M:%S)] [$i] $county hf exit=$status" | tee -a "$LOG_DIR/index.log"
done < "$LIST"
echo "all-counties hostfamily driver done" | tee -a "$LOG_DIR/index.log"
