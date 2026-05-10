#!/bin/bash
# Run full grading + delete for a sequence of states. Serialized to avoid
# overloading Render.
set -e
cd "$(dirname "$0")/../.."

STATES="$@"
[ -z "$STATES" ] && STATES="SD ND AR NH ID MS KY UT WY AL AK IA LA NV NM NE WV ME VT MT OK MS"

for STATE in $STATES; do
  STATE_LC=$(echo "$STATE" | tr '[:upper:]' '[:lower:]')
  OUTDIR="state_scrapers/${STATE_LC}/results/audit_2026_05_09"
  mkdir -p "$OUTDIR"
  GRADES="$OUTDIR/${STATE_LC}_grades.json"
  DELETE_OUT="$OUTDIR/${STATE_LC}_delete_outcome.json"

  echo "=========== $STATE ==========="
  date

  if [ -f "$GRADES" ]; then
    echo "$STATE: grade file exists, skipping grader"
  else
    .venv/bin/python scripts/audit/grade_hoa_text_quality.py \
      --state "$STATE" --out "$GRADES" \
      --workers 3 --with-docs-only \
      2>&1 | tee "$OUTDIR/grader.log" | tail -30
  fi

  python3 -c "
import json
d=json.load(open('$GRADES'))
print('$STATE counts:', d['verdict_counts'])
"

  echo "[delete dry-run]"
  .venv/bin/python scripts/audit/delete_junk_hoas.py --grades "$GRADES" 2>&1 | head -20

  # Auto-apply when junk count is sane
  JUNK=$(python3 -c "import json; d=json.load(open('$GRADES')); print(d['verdict_counts'].get('junk',0))")
  echo "$STATE junk count to delete: $JUNK"

  if [ "$JUNK" -gt 0 ]; then
    .venv/bin/python scripts/audit/delete_junk_hoas.py \
      --grades "$GRADES" --apply --out "$DELETE_OUT" 2>&1 | tail -20
  fi
  echo "$STATE: done."
done
