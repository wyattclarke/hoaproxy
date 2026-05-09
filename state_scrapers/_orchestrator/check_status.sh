#!/bin/bash
# Quick status check for the overnight 9-state run.
# Usage: bash state_scrapers/_orchestrator/check_status.sh

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT" || exit 1

echo "=== ORCHESTRATOR STATUS ==="
PID_FILE="$ROOT/state_scrapers/_orchestrator/orchestrator.pid"
if [[ -f "$PID_FILE" ]]; then
  PID=$(grep -oE "[0-9]+" "$PID_FILE" | head -1)
  if kill -0 "$PID" 2>/dev/null; then
    echo "  RUNNING — PID $PID"
    ps -p "$PID" -o pid,etime,command | tail -1
  else
    echo "  NOT RUNNING (PID $PID exited)"
  fi
fi

echo ""
echo "=== TOP-LEVEL PROGRESS ==="
tail -20 "$ROOT/state_scrapers/_orchestrator/overnight.log" 2>/dev/null

echo ""
echo "=== STATE STATUS (status.json) ==="
if [[ -f "$ROOT/state_scrapers/_orchestrator/status.json" ]]; then
  python3 -c "
import json
with open('$ROOT/state_scrapers/_orchestrator/status.json') as f:
    s = json.load(f)
print(f\"Started: {s.get('started_at')}\")
print(f\"Halted:  {s.get('halted', False)}  reason: {s.get('halt_reason', '')}\")
print(f\"Finished: {s.get('finished_at', '(in progress)')}\")
print(f\"DocAI baseline: \${s.get('docai_baseline_usd')}\")
print()
for st, info in (s.get('states') or {}).items():
    if not isinstance(info, dict):
        continue
    completed = info.get('completed', False)
    rc = info.get('runner_rc', '?')
    live = info.get('final_live_count', '?')
    docai = info.get('docai_after_state', '?')
    raw = info.get('raw_manifests', '?')
    prepared = info.get('prepared_bundles', '?')
    print(f\"  {st}: completed={completed} runner_rc={rc} live={live} raw_manifests={raw} prepared={prepared} docai_total=\${docai}\")
"
fi

echo ""
echo "=== LIVE COUNTS ==="
for st in DC HI IA ID KY AL LA NV UT; do
  COUNT=$(curl -s "https://hoaproxy.org/hoas/summary?state=$st" 2>/dev/null | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('total', len(d.get('results',[]))) if isinstance(d, dict) else len(d))" 2>/dev/null || echo "?")
  printf "  %-3s: %s live HOAs\n" "$st" "$COUNT"
done

echo ""
echo "=== DOCAI COST ==="
TOKEN=$(grep -E '^JWT_SECRET=' settings.env 2>/dev/null | head -1 | sed -E 's/^JWT_SECRET=//; s/^"(.*)"$/\1/; s/^[^[:space:]]*[[:space:]]*//' )
if [[ -n "$TOKEN" ]]; then
  curl -s -H "Authorization: Bearer $TOKEN" "https://hoaproxy.org/admin/costs" 2>/dev/null | python3 -m json.tool 2>/dev/null | head -20
else
  echo "  (no JWT_SECRET in settings.env)"
fi
