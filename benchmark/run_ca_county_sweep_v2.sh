#!/usr/bin/env bash
# Driver B: deeper single-county CA sweep end-to-end. Modeled on
# run_fl_county_sweep_v2.sh with CA-specific query angles:
#
# - Davis-Stirling Common Interest Development (CA Civ Code §4000+) anchor
# - Mutual Benefit Corporation (CA-specific corp form)
# - California-specific publisher hosts (adamsstirling, davis-stirling.com,
#   echo-ca.org, cacm.org)
# - California top mgmt-co hosts (FirstService Residential CA, Action,
#   Powerstone, Keystone Pacific, Seabreeze, Walters, Action Property)
# - Annual Policy Statement / Annual Budget Report (Davis-Stirling §5300/§5310)
# - CC&R / Declaration of Restrictions (CA terminology)
# - Per-city governing-document indexes
#
# Usage:
#   benchmark/run_ca_county_sweep_v2.sh <CountyName> <City1> [City2 ...]

set -euo pipefail

COUNTY="${1:?county name required}"
shift
CITIES=("$@")

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SAFE="${COUNTY// /_}"
RESULTS="$ROOT/benchmark/results/ca_county_v2_${SAFE}"
QUERIES="$RESULTS/queries.txt"
SERPER_RUN="ca_county_v2_${SAFE}_1"
mkdir -p "$RESULTS"

cd "$ROOT"
source .venv/bin/activate
export GOOGLE_CLOUD_PROJECT=hoaware
export HOA_DISCOVERY_RESPECT_ROBOTS=1
export PYTHONUNBUFFERED=1

{
  echo "# CA v2 - $COUNTY County, generated $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  # County-level legal phrases.
  echo "\"$COUNTY County\" \"California\" \"Homeowners Association\" \"Declaration\""
  echo "\"$COUNTY County\" \"California\" \"Homeowners Association\" \"Bylaws\""
  echo "\"$COUNTY County\" \"California\" \"Declaration of Covenants\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Declaration of Restrictions\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"CC&R\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"CC&Rs\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Davis-Stirling\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Common Interest Development\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Mutual Benefit Corporation\" \"Homeowners Association\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Articles of Incorporation\" \"Homeowners Association\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Restated Declaration\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Restated Bylaws\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Amendment to Declaration\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Master Association\" \"Declaration\""
  # Condo / townhome / master.
  echo "\"$COUNTY County\" \"California\" \"Condominium Association\" \"Declaration\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Townhome Association\" \"Declaration\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Property Owners Association\" \"Declaration\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Annual Policy Statement\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Annual Budget Report\" filetype:pdf"
  # Architectural docs anchored to mandatory-HOA signal.
  echo "\"$COUNTY County\" \"California\" \"Architectural Standards\" \"Declaration of Covenants\" filetype:pdf"
  echo "\"$COUNTY County\" \"California\" \"Architectural Guidelines\" \"Declaration of Covenants\" filetype:pdf"
  for city in "${CITIES[@]}"; do
    # Per-city metro queries.
    echo "\"$city\" \"California\" \"Homeowners Association\" \"Declaration\""
    echo "\"$city\" \"California\" \"Homeowners Association\" \"Covenants\" filetype:pdf"
    echo "\"$city\" \"California\" \"Homeowners Association\" \"Bylaws\" filetype:pdf"
    echo "\"$city\" \"California\" homeowners association documents"
    echo "\"$city\" \"California\" \"CC&R\" filetype:pdf"
    echo "\"$city\" \"California\" \"Davis-Stirling\" \"Homeowners Association\" filetype:pdf"
    echo "\"$city\" \"California\" \"Declaration of Restrictions\" filetype:pdf"
    # Condo / townhome / master per city.
    echo "\"$city\" \"California\" \"Condominium Association\" \"Declaration\" filetype:pdf"
    echo "\"$city\" \"California\" \"Townhome Association\" \"Declaration\" filetype:pdf"
    echo "\"$city\" \"California\" \"Master Association\" \"Declaration\" filetype:pdf"
    # CA-specific Annual Policy Statement / Budget Report
    echo "\"$city\" \"California\" \"Annual Policy Statement\" filetype:pdf"
    # Architectural docs anchored to a mandatory-HOA signal.
    echo "\"$city\" \"California\" \"Architectural Guidelines\" \"Declaration of Covenants\" filetype:pdf"
    echo "\"$city\" \"California\" \"Architectural Standards\" \"Declaration of Covenants\" filetype:pdf"
    # Host-family per city (eNeighbors, hmsft, Cobalt, GoGladly, CDN PDFs).
    echo "site:eneighbors.com \"$city\""
    echo "site:cobaltreks.com \"$city\""
    echo "site:gogladly.com/connect/document \"$city\" California"
    echo "site:img1.wsimg.com \"$city\" \"Homeowners Association\" filetype:pdf"
    echo "site:nebula.wsimg.com \"$city\" \"Homeowners Association\" filetype:pdf"
    echo "site:static1.squarespace.com \"$city\" \"Homeowners Association\" filetype:pdf"
    echo "site:rackcdn.com \"$city\" \"Homeowners Association\" filetype:pdf"
    echo "site:s3.amazonaws.com \"$city\" \"California\" \"Homeowners Association\" filetype:pdf"
    # CA-specific publisher / law-firm public docs
    echo "site:adamsstirling.com \"$city\" California"
    echo "site:davis-stirling.com \"$city\""
    echo "site:berdingweil.com \"$city\""
    echo "site:echo-ca.org \"$city\""
    # CA top mgmt-co hosts
    echo "site:fsresidential.com \"$city\" California"
    echo "site:actionlife.com \"$city\" filetype:pdf"
    echo "site:powerstonepm.com \"$city\" filetype:pdf"
    echo "site:keystonepacific.com \"$city\" filetype:pdf"
    echo "site:seabreezemgmt.com \"$city\" filetype:pdf"
    echo "site:walters-management.com \"$city\" filetype:pdf"
    echo "site:euclidmgmt.com \"$city\" filetype:pdf"
    echo "site:associa.us \"$city\" California"
    # Generic portals heavy in CA
    echo "site:vantaca.com \"$city\" California"
    echo "site:frontsteps.com \"$city\" California"
    echo "site:cinc.io \"$city\" California"
    echo "site:smartwebs.net \"$city\" California"
    # Owned-domain document indexes per city.
    echo "\"$city\" \"California\" hoa documents inurl:.org"
    echo "\"$city\" \"California\" hoa documents inurl:.com"
    echo "\"$city\" \"California\" hoa governing-documents inurl:.org"
    echo "\"$city\" \"California\" \"Homeowners Association\" inurl:documents"
  done
} > "$QUERIES"

CITY_ARGS=()
for city in "${CITIES[@]}"; do
  CITY_ARGS+=( --city "$city" )
done

python benchmark/scrape_state_serper_docpages.py \
  --state CA --state-name California \
  --county "$COUNTY" "${CITY_ARGS[@]}" \
  --default-county "$COUNTY" \
  --run-id "$SERPER_RUN" \
  --queries-file "$QUERIES" \
  --max-queries 5000 --results-per-query 10 --pages-per-query 1 \
  --max-leads 5000 --min-score 5 --search-delay 0.25 \
  --include-direct-pdfs

LEADS="$ROOT/benchmark/results/ca_serper_docpages_${SERPER_RUN}/leads.jsonl"
AUDIT="$ROOT/benchmark/results/ca_serper_docpages_${SERPER_RUN}/audit.jsonl"

VAL="$RESULTS/validated.jsonl"
VAL_AUDIT="$RESULTS/validated_audit.json"
OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/openrouter_ks_planner.py validate-leads \
  "$LEADS" --output "$VAL" --audit "$VAL_AUDIT" \
  --state CA --county "$COUNTY" \
  --model deepseek/deepseek-v4-flash \
  --fallback-model moonshotai/kimi-k2.6 \
  --batch-size 20 || true

python3 - "$VAL" "$RESULTS/validated_clean.jsonl" "$COUNTY" <<'PY'
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
    r["county"] = county
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
  --max-pages 5 --max-output 250 --delay 0.4 || true

python3 - "$CLEAN" "$RESULTS/cleaned_dedup.jsonl" "$COUNTY" <<'PY'
import json, sys, re, glob
inp, out, county = sys.argv[1], sys.argv[2], sys.argv[3]
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
    u = (r.get("source_url") or "")
    if not u or u.strip() in seen:
        continue
    r["county"] = county
    clean.append(r)
with open(out, "w") as f:
    for r in clean:
        f.write(json.dumps(r, sort_keys=True) + "\n")
print(f"cleaned_in={len(rows)} cleaned_dedup={len(clean)}", file=sys.stderr)
PY

COMBINED="$RESULTS/probe_input.jsonl"
cat "$RESULTS/validated_clean.jsonl" "$RESULTS/cleaned_dedup.jsonl" 2>/dev/null > "$COMBINED" || true

if [ -s "$COMBINED" ]; then
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
  echo "no leads to probe for $COUNTY" >&2
fi
