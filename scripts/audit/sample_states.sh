#!/bin/bash
# Sample-grade a list of states to identify RI-style junk-content issues.
# Runs serially (one state at a time) to avoid hammering Render.
set -e
cd "$(dirname "$0")/../.."

STATES_HIGH="IL SD ND AR"
STATES_MED="NH ID MS KY UT WY"
STATES_LOW_CHECK="HI GA TN KS DE MT OK NM NV NE AL IA LA AK WV ME VT"

OUTDIR="state_scrapers/_orchestrator/quality_audit_2026_05_09/samples"
mkdir -p "$OUTDIR"

run_sample() {
  local state=$1
  local n=$2
  echo "=== $state (sample $n) ==="
  .venv/bin/python scripts/audit/grade_hoa_text_quality.py \
    --state "$state" \
    --out "${OUTDIR}/${state}_sample.json" \
    --sample "$n" --workers 2 --with-docs-only
  python3 -c "
import json
d=json.load(open('${OUTDIR}/${state}_sample.json'))
print('${state} verdicts:', d.get('verdict_counts'))
"
}

case "${1:-all}" in
  high) for s in $STATES_HIGH; do run_sample "$s" 25; done ;;
  med)  for s in $STATES_MED;  do run_sample "$s" 20; done ;;
  low)  for s in $STATES_LOW_CHECK; do run_sample "$s" 15; done ;;
  all)
    for s in $STATES_HIGH; do run_sample "$s" 25; done
    for s in $STATES_MED; do run_sample "$s" 20; done
    for s in $STATES_LOW_CHECK; do run_sample "$s" 15; done
    ;;
  *) run_sample "$1" "${2:-20}" ;;
esac
