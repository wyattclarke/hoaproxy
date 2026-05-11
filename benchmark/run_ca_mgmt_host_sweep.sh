#!/usr/bin/env bash
# Driver C: CA management-company host expansion sweep.
#
# Builds site:domain queries against the CA top mgmt-co list. ~12 hosts
# × 7 patterns = ~84 queries. Trivial Serper spend, high yield because CA
# portals serve thousands of HOAs from one host.
#
# Usage:
#   benchmark/run_ca_mgmt_host_sweep.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RESULTS="$ROOT/benchmark/results/ca_mgmt_host_sweep"
QUERIES="$RESULTS/queries.txt"
SERPER_RUN="ca_mgmt_host_1"
mkdir -p "$RESULTS"

cd "$ROOT"
source .venv/bin/activate
export GOOGLE_CLOUD_PROJECT=hoaware
export HOA_DISCOVERY_RESPECT_ROBOTS=1
export PYTHONUNBUFFERED=1

# Top CA mgmt-co hosts (megaclusters first, mid-tier follows).
HOSTS=(
  # Mega CA mgmt cos (verified via web reconnaissance, not RA-counts which
  # filed under third parties)
  "fsresidential.com"
  "associa.us"
  "actionlife.com"
  "seabreezemgmt.com"
  "powerstonepm.com"
  "keystonepacific.com"
  "euclidmgmt.com"
  "walters-management.com"
  "spectrumamg.com"
  "commonareas.com"
  # Mid-tier from RA-name analysis (data/ca_top_management_companies.json)
  "regentrealestate.com"
  "thehelsinggroup.com"
  "prescottcompanies.com"
  "powerstonepm.com"
  "cardinalpm.com"
  "diversifiedca.com"
  # CA-specific publisher / law-firm public docs
  "adamsstirling.com"
  "davis-stirling.com"
  "berdingweil.com"
  "echo-ca.org"
  # Generic portals heavy in CA
  "vantaca.com"
  "frontsteps.com"
  "cinc.io"
  "smartwebs.net"
  "nabrnetwork.com"
)

{
  echo "# CA Driver C — mgmt-co host sweep, generated $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  for host in "${HOSTS[@]}"; do
    echo "site:${host} \"California\" \"Declaration\" filetype:pdf"
    echo "site:${host} \"California\" \"CC&R\" filetype:pdf"
    echo "site:${host} \"California\" \"Bylaws\" filetype:pdf"
    echo "site:${host} \"California\" \"Homeowners Association\" filetype:pdf"
    echo "site:${host} \"California\" \"Davis-Stirling\" filetype:pdf"
    echo "site:${host} \"California\" governing documents"
    echo "site:${host} California condominium declaration"
  done
} > "$QUERIES"

QUERY_COUNT=$(grep -cv '^#' "$QUERIES" || true)
echo "Generated $QUERY_COUNT queries across ${#HOSTS[@]} hosts" >&2

LEADS="$ROOT/benchmark/results/ca_serper_docpages_${SERPER_RUN}/leads.jsonl"
AUDIT="$ROOT/benchmark/results/ca_serper_docpages_${SERPER_RUN}/audit.jsonl"
VAL="$RESULTS/validated.jsonl"
VAL_AUDIT="$RESULTS/validated_audit.json"

if [ -s "$LEADS" ]; then
  echo "[resume] mgmt-host: leads.jsonl exists ($(wc -l < "$LEADS") leads); skip Serper" >&2
else
  python benchmark/scrape_state_serper_docpages.py \
    --state CA --state-name California \
    --county "Statewide" \
    --default-county "Statewide" \
    --run-id "$SERPER_RUN" \
    --queries-file "$QUERIES" \
    --max-queries 500 --results-per-query 10 --pages-per-query 1 \
    --max-leads 5000 --min-score 5 --search-delay 0.25 \
    --include-direct-pdfs
fi

if [ -s "$VAL" ]; then
  echo "[resume] mgmt-host: validated.jsonl exists ($(wc -l < "$VAL") rows); skip validate" >&2
else
  OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/openrouter_ks_planner.py validate-leads \
    "$LEADS" --output "$VAL" --audit "$VAL_AUDIT" \
    --state CA --county "Statewide" \
    --model deepseek/deepseek-v4-flash \
    --batch-size 20 || true
fi

# Cleanup + dedup against prior CA/FL/GA results.
python3 - "$VAL" "$RESULTS/validated_clean.jsonl" "Statewide" <<'PY'
import json, sys, re, glob
inp, out, county = sys.argv[1], sys.argv[2], sys.argv[3]
JUNK = re.compile(
    r"(temporary_breach|/newsletter|/minutes|/agenda|/budget|/financial|/audit|"
    r"/rental|/lease|pool[-_ ]rules|pool[-_ ]pass|directory|roster|violation|"
    r"estoppel|closing|coupon|listing|for[-_ ]sale|reminder|history|"
    r"estate[-_ ]sale|application[-_ ]form|payment|invoice|fee[-_ ]schedule|"
    r"election|nomination|/Legislation/|hoa\.texas\.gov|sfmohcd|/AgendaCenter/|"
    r"/Council/|/Planning/|tn\.gov|caionline|hoaleader)",
    re.I,
)
seen = set()
for path in (
    glob.glob("benchmark/results/ga_*/validated_clean.jsonl") +
    glob.glob("benchmark/results/ga_*/cleaned_dedup.jsonl") +
    glob.glob("benchmark/results/fl_*/validated_clean.jsonl") +
    glob.glob("benchmark/results/fl_*/cleaned_dedup.jsonl") +
    glob.glob("benchmark/results/ca_*/validated_clean.jsonl") +
    glob.glob("benchmark/results/ca_*/cleaned_dedup.jsonl")
):
    try:
        with open(path) as f:
            for line in f:
                r = json.loads(line)
                for u in r.get("pre_discovered_pdf_urls", []) + [
                    r.get("source_url") or "", r.get("website") or "",
                ]:
                    if u: seen.add(u.strip())
    except FileNotFoundError:
        pass
try:
    rows = [json.loads(l) for l in open(inp) if l.strip()]
except FileNotFoundError:
    rows = []
clean = []
for r in rows:
    u = (r.get("source_url") or r.get("website") or "")
    if not u or u.strip() in seen or JUNK.search(u):
        continue
    if u.lower().split("?", 1)[0].split("#", 1)[0].endswith(".pdf") or "format=pdf" in u.lower():
        r["pre_discovered_pdf_urls"] = [u]
        r["website"] = None
    else:
        r.setdefault("website", u)
        r["pre_discovered_pdf_urls"] = []
    # Don't tag a county here; leave for OCR-time slug+county derivation.
    # Bank infrastructure handles unknown-county prefix.
    clean.append(r)
with open(out, "w") as f:
    for r in clean:
        f.write(json.dumps(r, sort_keys=True) + "\n")
print(f"validated_in={len(rows)} validated_clean={len(clean)}", file=sys.stderr)
PY

CLEAN="$RESULTS/cleaned.jsonl"
CLEAN_REJ="$RESULTS/cleaned_rejects.jsonl"
python benchmark/clean_direct_pdf_leads.py "$LEADS" \
  --audit "$AUDIT" \
  --output "$CLEAN" --rejects "$CLEAN_REJ" \
  --state CA --state-name California \
  --max-pages 5 --max-output 500 --delay 0.4 || true

COMBINED="$RESULTS/probe_input.jsonl"
cat "$RESULTS/validated_clean.jsonl" "$CLEAN" 2>/dev/null > "$COMBINED" || true

if [ -s "$RESULTS/probe.jsonl" ]; then
  echo "[resume] mgmt-host: probe.jsonl exists ($(wc -l < "$RESULTS/probe.jsonl") rows); skip probe" >&2
elif [ -s "$COMBINED" ]; then
  python benchmark/probe_leads_with_pre_discovered.py "$COMBINED" \
    --output "$RESULTS/probe.jsonl" \
    --timeout 180 --max-pdfs 12 --delay 1.0 || true
  python3 - "$RESULTS/probe.jsonl" <<'PY'
import json, sys
banked=skipped=errs=0
with open(sys.argv[1]) as f:
    for line in f:
        r = json.loads(line)
        if "error" in r: errs += 1; continue
        res = r.get("result", {})
        banked += res.get("documents_banked", 0)
        skipped += res.get("documents_skipped", 0)
print(f"banked={banked} skipped={skipped} errs={errs}", file=sys.stderr)
PY
else
  echo "no leads to probe for mgmt-host sweep" >&2
fi
