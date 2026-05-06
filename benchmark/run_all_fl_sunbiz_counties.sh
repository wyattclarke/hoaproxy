#!/usr/bin/env bash
# Driver A loop: run run_fl_sunbiz_county_sweep.sh over the top 20 FL counties
# by Sunbiz HOA count. Concentrates spend where the seed data is densest.
#
# County order (descending HOA count from Sunbiz April 2026):
#   miami-dade 4,708 / palm-beach 3,252 / broward 3,201 / hillsborough 2,169 /
#   orange 2,091 / pinellas 2,074 / collier 2,001 / lee 1,865 / sarasota 1,230 /
#   brevard 1,181 / duval 1,002 / seminole 953 / manatee 934 / martin 745 /
#   polk 690 / volusia 638 / lake 627 / osceola 585 / charlotte 568 / flagler 504
#
# Usage:
#   benchmark/run_all_fl_sunbiz_counties.sh [start_index]
#   (start_index defaults to 1; use to resume after failure)

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT/benchmark/results/fl_sunbiz_all_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOG_DIR"
START="${1:-1}"

# Top-20 counties: slug|DisplayName
# Display names sourced from fl_top_counties_v2.txt where available.
COUNTIES=(
  "miami-dade|Miami-Dade"
  "palm-beach|Palm Beach"
  "broward|Broward"
  "hillsborough|Hillsborough"
  "orange|Orange"
  "pinellas|Pinellas"
  "collier|Collier"
  "lee|Lee"
  "sarasota|Sarasota"
  "brevard|Brevard"
  "duval|Duval"
  "seminole|Seminole"
  "manatee|Manatee"
  "martin|Martin"
  "polk|Polk"
  "volusia|Volusia"
  "lake|Lake"
  "osceola|Osceola"
  "charlotte|Charlotte"
  "flagler|Flagler"
)

i=0
for entry in "${COUNTIES[@]}"; do
  i=$((i + 1))
  if [ "$i" -lt "$START" ]; then
    continue
  fi

  slug="${entry%%|*}"
  display="${entry#*|}"

  echo "[$(date -u +%H:%M:%S)] [$i/20] $display ($slug) sunbiz sweep starting" \
    | tee -a "$LOG_DIR/index.log"

  bash "$ROOT/benchmark/run_fl_sunbiz_county_sweep.sh" "$slug" "$display" \
    > "$LOG_DIR/county_${slug}.log" 2>&1
  status=$?

  echo "[$(date -u +%H:%M:%S)] [$i/20] $display ($slug) exit=$status" \
    | tee -a "$LOG_DIR/index.log"
done

echo "Driver A all-counties sunbiz sweep done" | tee -a "$LOG_DIR/index.log"
