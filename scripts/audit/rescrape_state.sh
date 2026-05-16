#!/bin/bash
# After grading + deletion, optionally rescrape a state with the current
# playbook. Usage:
#   rescrape_state.sh ri  state_scrapers/ri/leads/ri_sos_associations.jsonl  "Rhode Island"
#
# Calls namelist_discover.py (with skip-existing OFF so junk-only entities
# get a fresh chance) → prepare_bank_for_ingest.py → /admin/ingest-ready-gcs
# → phase10_close.py.
set -e
cd "$(dirname "$0")/../.."

STATE_LC=$1
SEED=$2
STATE_NAME=$3
RUN_ID="${STATE_LC}_audit_$(date +%Y%m%d_%H%M)"
OUTDIR="state_scrapers/${STATE_LC}/results/${RUN_ID}"

if [ -z "$STATE_LC" ] || [ -z "$SEED" ] || [ -z "$STATE_NAME" ]; then
  echo "usage: $0 <state-lc> <seed.jsonl> <\"State Name\">"
  exit 2
fi
STATE_UC=$(echo "$STATE_LC" | tr '[:lower:]' '[:upper:]')

mkdir -p "$OUTDIR"
echo "=== rescrape state=$STATE_UC seed=$SEED run_id=$RUN_ID ==="

# Phase 2 — name-binding discovery (no skip-existing — re-discover with new filters)
echo "[1/4] namelist_discover (apply)"
.venv/bin/python state_scrapers/_orchestrator/namelist_discover.py \
  --seed "$SEED" \
  --state "$STATE_UC" \
  --state-name "$STATE_NAME" \
  --ledger "${OUTDIR}/namelist_ledger.jsonl" \
  --workers 4 \
  --apply 2>&1 | tee "${OUTDIR}/namelist.log"

# Phase 7 — prepare bundles
echo "[2/4] prepare_bank_for_ingest"
.venv/bin/python scripts/prepare_bank_for_ingest.py \
  --state "$STATE_UC" \
  --max-docai-cost-usd 10 \
  --ledger "${OUTDIR}/prepared_ingest_ledger.jsonl" \
  --geo-cache "${OUTDIR}/geo_cache.json" \
  --bank-bucket hoaproxy-bank \
  --prepared-bucket hoaproxy-ingest-ready 2>&1 | tee "${OUTDIR}/prepare.log"

# Phase 8 — drain prepared bundles into live (looped 50/call)
echo "[3/4] drain ingest-ready-gcs"
.venv/bin/python -c "
from dotenv import load_dotenv
load_dotenv('settings.env')
import os, requests, time, json
def token():
    return os.environ.get('HOAPROXY_ADMIN_BEARER') or os.environ.get('JWT_SECRET')
t = token()
total_imported = 0
for i in range(60):
    r = requests.post('https://hoaproxy.org/admin/ingest-ready-gcs?state=$STATE_UC&limit=50',
                      headers={'Authorization': f'Bearer {t}'}, timeout=600)
    if r.status_code != 200:
        print(f'iter {i}: http {r.status_code}')
        break
    body = r.json()
    results = body.get('results') or []
    if not results:
        print('drained empty; stopping')
        break
    n_imp = sum(1 for x in results if x.get('status') == 'imported')
    total_imported += n_imp
    print(f'iter {i}: imported {n_imp}/{len(results)} (total {total_imported})')
    time.sleep(75)  # /upload pacing rule: 75s gap
print(f'FINAL imported: {total_imported}')
" 2>&1 | tee "${OUTDIR}/drain.log"

# Phase 10 — close (rename + delete + audit)
echo "[4/4] phase10_close"
.venv/bin/python scripts/phase10_close.py \
  --state "$STATE_UC" \
  --run-id "$RUN_ID" \
  --apply 2>&1 | tee "${OUTDIR}/phase10.log" || true

echo "=== rescrape done; outputs at $OUTDIR ==="
