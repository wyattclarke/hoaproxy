#!/usr/bin/env bash
# Driver B — Arizona county-broad sweep. Per-county (or per-sub-county-cluster
# for Maricopa/Pima) Serper sweep with AZ-specific query angles:
#
# - Declaration of CC&Rs / Master Deed (A.R.S. §33-1801+ HOA, §33-1201+ condo)
# - Architectural Standards / Modification anchored to Declaration
# - Recorded "Restated Declaration" / "Master Deed" / Easements
# - Per-city legal-phrase queries
# - Host-family patterns (eNeighbors, hmsft-doc, gogladly, fsrconnect, wsimg,
#   squarespace, rackcdn, s3.amazonaws, nabrnetwork, connectresident)
# - AZ management-co host queries
# - Per-city governing-document indexes
#
# Usage:
#   benchmark/run_az_county_sweep_v2.sh <SweepSlug> <CountyDisplay> <City1> [City2 ...]
#
# Example:
#   benchmark/run_az_county_sweep_v2.sh maricopa-phoenix-core "Maricopa" Phoenix Glendale Tempe
#   benchmark/run_az_county_sweep_v2.sh pinal "Pinal" "Casa Grande" Florence Coolidge

set -euo pipefail

SWEEP_SLUG="${1:?sweep slug required (e.g. maricopa-phoenix-core or pinal)}"
COUNTY="${2:?county display name required (e.g. Maricopa)}"
shift 2
CITIES=("$@")

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RESULTS="$ROOT/benchmark/results/az_county_v2_${SWEEP_SLUG}"
QUERIES="$RESULTS/queries.txt"
SERPER_RUN="az_county_v2_${SWEEP_SLUG}_1"
mkdir -p "$RESULTS"

cd "$ROOT"
source .venv/bin/activate
export GOOGLE_CLOUD_PROJECT=hoaware
export HOA_DISCOVERY_RESPECT_ROBOTS=1
export PYTHONUNBUFFERED=1

{
  echo "# AZ v2 - sweep=$SWEEP_SLUG county=$COUNTY, generated $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  # County-level legal phrases.
  echo "\"$COUNTY County\" \"Arizona\" \"Homeowners Association\" \"Declaration\""
  echo "\"$COUNTY County\" \"Arizona\" \"Homeowners Association\" \"Bylaws\""
  echo "\"$COUNTY County\" \"Arizona\" \"Declaration of Covenants\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"non-profit corporation\" \"Homeowners Association\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"Homeowners Association\" \"Articles of Incorporation\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"Restated Declaration\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"Restated Bylaws\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"Amendment to Declaration\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"Master Association\" \"Declaration\""
  # Condo / townhome / master.
  echo "\"$COUNTY County\" \"Arizona\" \"Condominium Association\" \"Declaration\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"Condominium\" \"Master Deed\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"Townhome Association\" \"Declaration\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"Property Owners Association\" \"Declaration\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"POA\" \"Declaration of Covenants\" filetype:pdf"
  # AZ-specific statutory anchors.
  echo "\"$COUNTY County\" \"Arizona\" \"33-1801\" \"Homeowners\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"33-1201\" \"Condominium\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"planned community\" \"Declaration\" filetype:pdf"
  # Architectural docs anchored to a mandatory-HOA signal.
  echo "\"$COUNTY County\" \"Arizona\" \"Architectural Standards\" \"Declaration of Covenants\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"Architectural Guidelines\" \"Declaration of Covenants\" filetype:pdf"
  echo "\"$COUNTY County\" \"Arizona\" \"Architectural Review\" \"Restrictive Covenants\" filetype:pdf"

  for city in "${CITIES[@]}"; do
    # Per-city metro queries.
    echo "\"$city\" \"Arizona\" \"Homeowners Association\" \"Declaration\""
    echo "\"$city\" \"Arizona\" \"Homeowners Association\" \"Covenants\" filetype:pdf"
    echo "\"$city\" \"Arizona\" \"Homeowners Association\" \"Bylaws\" filetype:pdf"
    echo "\"$city\" \"Arizona\" homeowners association documents"
    echo "\"$city\" \"Arizona\" \"Condominium Association\" \"Declaration\" filetype:pdf"
    echo "\"$city\" \"Arizona\" \"Townhome Association\" \"Declaration\" filetype:pdf"
    echo "\"$city\" \"Arizona\" \"Master Association\" \"Declaration\" filetype:pdf"
    echo "\"$city\" \"Arizona\" \"planned community\" \"Declaration\" filetype:pdf"
    # Architectural anchored to declaration.
    echo "\"$city\" \"Arizona\" \"Architectural Guidelines\" \"Declaration of Covenants\" filetype:pdf"
    echo "\"$city\" \"Arizona\" \"Architectural Standards\" \"Declaration of Covenants\" filetype:pdf"
    echo "\"$city\" \"Arizona\" \"Architectural Review\" \"Restrictive Covenants\" filetype:pdf"
    # Host-family per city (state-agnostic CDNs / portal hosts).
    echo "site:eneighbors.com \"$city\""
    echo "site:eneighbors.com/p/ \"$city\""
    echo "site:cobaltreks.com \"$city\""
    echo "site:gogladly.com/connect/document \"$city\" Arizona"
    echo "site:img1.wsimg.com \"$city\" \"Homeowners Association\" filetype:pdf"
    echo "site:nebula.wsimg.com \"$city\" \"Homeowners Association\" filetype:pdf"
    echo "site:static1.squarespace.com \"$city\" \"Homeowners Association\" filetype:pdf"
    echo "site:rackcdn.com \"$city\" \"Homeowners Association\" filetype:pdf"
    echo "site:s3.amazonaws.com \"$city\" \"Arizona\" \"Homeowners Association\" filetype:pdf"
    echo "site:fsrconnect.com \"$city\" Arizona"
    echo "site:fsresidential.com \"$city\" Arizona"
    echo "site:nabrnetwork.com \"$city\" Arizona"
    echo "site:connectresident.com \"$city\" Arizona"
    echo "site:buurt.com \"$city\" Arizona"
    echo "inurl:hmsft-doc \"$city\""
    echo "inurl:/file/document-page/ \"$city\" Arizona"
    echo "inurl:/wp-content/uploads/ \"$city\" \"Homeowners Association\" filetype:pdf"
    # AZ-specific management-company host queries.
    echo "site:ccmcnet.com \"$city\" Arizona"
    echo "site:associatedasset.com \"$city\""
    echo "site:brownmanagement.com \"$city\""
    echo "site:visioncommunitymanagement.com \"$city\""
    echo "site:cadden.com \"$city\""
    echo "site:cityproperty.com \"$city\""
    echo "site:hoaliving.com \"$city\""
    echo "site:trestle-mg.com \"$city\""
    echo "site:pdsaz.com \"$city\""
    # Owned-domain document indexes per city.
    echo "\"$city\" \"Arizona\" hoa documents inurl:.org"
    echo "\"$city\" \"Arizona\" hoa documents inurl:.com"
    echo "\"$city\" \"Arizona\" hoa governing-documents inurl:.org"
    echo "\"$city\" \"Arizona\" \"Homeowners Association\" inurl:documents"
  done
} > "$QUERIES"

CITY_ARGS=()
for city in "${CITIES[@]}"; do
  CITY_ARGS+=( --city "$city" )
done

python benchmark/scrape_state_serper_docpages.py \
  --state AZ --state-name Arizona \
  --county "$COUNTY" "${CITY_ARGS[@]}" \
  --default-county "$COUNTY" \
  --run-id "$SERPER_RUN" \
  --queries-file "$QUERIES" \
  --max-queries 5000 --results-per-query 10 --pages-per-query 1 \
  --max-leads 0 --min-score 5 --search-delay 0.25 \
  --include-direct-pdfs

LEADS="$ROOT/benchmark/results/az_serper_docpages_${SERPER_RUN}/leads.jsonl"
AUDIT="$ROOT/benchmark/results/az_serper_docpages_${SERPER_RUN}/audit.jsonl"

VAL="$RESULTS/validated.jsonl"
VAL_AUDIT="$RESULTS/validated_audit.json"
OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/openrouter_ks_planner.py validate-leads \
  "$LEADS" --output "$VAL" --audit "$VAL_AUDIT" \
  --state AZ --county "$COUNTY" \
  --model deepseek/deepseek-v4-flash \
  --fallback-model moonshotai/kimi-k2.6 \
  --batch-size 20 || true

# Dedup validated leads against all prior AZ + cross-state results to avoid
# re-banking the same source URL across drivers / sub-county sweeps.
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
for path in glob.glob("benchmark/results/az_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/az_*/cleaned_dedup.jsonl") + \
            glob.glob("benchmark/results/fl_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/fl_*/cleaned_dedup.jsonl") + \
            glob.glob("benchmark/results/ga_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/ga_*/cleaned_dedup.jsonl"):
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
  --state AZ --state-name Arizona \
  --max-pages 5 --max-output 500 --delay 0.4 || true

python3 - "$CLEAN" "$RESULTS/cleaned_dedup.jsonl" "$COUNTY" <<'PY'
import json, sys, glob
inp, out, county = sys.argv[1], sys.argv[2], sys.argv[3]
seen = set()
for path in glob.glob("benchmark/results/az_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/az_*/cleaned_dedup.jsonl") + \
            glob.glob("benchmark/results/fl_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/fl_*/cleaned_dedup.jsonl") + \
            glob.glob("benchmark/results/ga_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/ga_*/cleaned_dedup.jsonl"):
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
  echo "no leads to probe for $SWEEP_SLUG ($COUNTY)" >&2
fi
