#!/usr/bin/env bash
# Host-family per-county sweep. Same shape as run_ga_county_sweep.sh but
# the queries target known-productive hosts (eneighbors, hmsft-doc,
# cobalt, gogladly, fieldstone, fsresidential, wsimg/squarespace/rackcdn
# CDN PDFs) inside one county's cities.
#
# Usage:
#   benchmark/run_ga_county_hostfamily.sh <CountyName> <City1> [City2 ...]

set -euo pipefail

COUNTY="${1:?county name required}"
shift
CITIES=("$@")

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RESULTS="$ROOT/benchmark/results/ga_hf_${COUNTY// /_}"
QUERIES="$RESULTS/queries.txt"
SERPER_RUN="ga_hf_${COUNTY// /_}_1"
mkdir -p "$RESULTS"

cd "$ROOT"
source .venv/bin/activate
export GOOGLE_CLOUD_PROJECT=hoaware
export HOA_DISCOVERY_RESPECT_ROBOTS=1

{
  echo "# GA host-family - $COUNTY County, generated $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  for city in "${CITIES[@]}"; do
    echo "site:eneighbors.com \"$city\" HOA"
    echo "site:eneighbors.com/p/ \"$city\""
    echo "site:cobaltreks.com \"$city\""
    echo "site:gogladly.com/connect/document \"$city\" Georgia"
    echo "site:img1.wsimg.com \"$city\" \"Homeowners Association\" filetype:pdf"
    echo "site:nebula.wsimg.com \"$city\" \"Homeowners Association\" filetype:pdf"
    echo "site:static1.squarespace.com \"$city\" \"Homeowners Association\" filetype:pdf"
    echo "site:rackcdn.com \"$city\" \"Homeowners Association\" filetype:pdf"
    echo "site:s3.amazonaws.com \"$city\" \"Georgia\" \"Homeowners Association\" filetype:pdf"
    echo "site:fieldstonerp.com \"$city\" filetype:pdf"
    echo "site:fsresidential.com \"$city\" Georgia"
    echo "site:thekeymanagers.com \"$city\" filetype:pdf"
    echo "inurl:hmsft-doc \"$city\""
    echo "inurl:/file/document-page/ \"$city\" Georgia"
    echo "inurl:/wp-content/uploads/ \"$city\" \"Homeowners Association\" filetype:pdf"
    echo "\"$city\" \"Georgia\" \"Architectural Guidelines\" \"Homeowners Association\" filetype:pdf"
    echo "\"$city\" \"Georgia\" \"Rules and Regulations\" \"Homeowners Association\" filetype:pdf"
  done
  echo "\"$COUNTY County\" \"Georgia\" \"non-profit corporation\" \"Homeowners Association\" filetype:pdf"
  echo "\"$COUNTY County\" \"Georgia\" \"Articles of Incorporation\" \"Homeowners Association\" filetype:pdf"
  echo "\"$COUNTY County\" \"Georgia\" \"Restated Bylaws\" \"Homeowners Association\" filetype:pdf"
} > "$QUERIES"

CITY_ARGS=()
for city in "${CITIES[@]}"; do
  CITY_ARGS+=( --city "$city" )
done
python benchmark/scrape_state_serper_docpages.py \
  --state GA --state-name Georgia \
  --county "$COUNTY" "${CITY_ARGS[@]}" \
  --default-county "$COUNTY" \
  --run-id "$SERPER_RUN" \
  --queries-file "$QUERIES" \
  --max-queries 60 --results-per-query 10 --pages-per-query 1 \
  --max-leads 150 --min-score 5 --search-delay 0.25 \
  --include-direct-pdfs

LEADS="$ROOT/benchmark/results/ga_serper_docpages_${SERPER_RUN}/leads.jsonl"
AUDIT="$ROOT/benchmark/results/ga_serper_docpages_${SERPER_RUN}/audit.jsonl"

VAL="$RESULTS/validated.jsonl"
VAL_AUDIT="$RESULTS/validated_audit.json"
OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/openrouter_ks_planner.py validate-leads \
  "$LEADS" --output "$VAL" --audit "$VAL_AUDIT" \
  --state GA --county "$COUNTY" \
  --model deepseek/deepseek-v4-flash \
  --batch-size 10 || true

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
for path in glob.glob("benchmark/results/ga_*_clean*.jsonl") + \
            glob.glob("benchmark/results/ga_*_validated_1_clean.jsonl") + \
            glob.glob("benchmark/results/ga_*_repaired.jsonl") + \
            glob.glob("benchmark/results/ga_county_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/ga_county_*/cleaned_dedup.jsonl") + \
            glob.glob("benchmark/results/ga_hf_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/ga_hf_*/cleaned_dedup.jsonl"):
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
  --state GA --state-name Georgia \
  --max-pages 5 --max-output 200 --delay 0.4 || true

python3 - "$CLEAN" "$RESULTS/cleaned_dedup.jsonl" "$COUNTY" <<'PY'
import json, sys, re, glob
inp, out, county = sys.argv[1], sys.argv[2], sys.argv[3]
seen = set()
for path in glob.glob("benchmark/results/ga_*_clean*.jsonl") + \
            glob.glob("benchmark/results/ga_*_validated_1_clean.jsonl") + \
            glob.glob("benchmark/results/ga_*_repaired.jsonl") + \
            glob.glob("benchmark/results/ga_county_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/ga_county_*/cleaned_dedup.jsonl") + \
            glob.glob("benchmark/results/ga_hf_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/ga_hf_*/cleaned_dedup.jsonl"):
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
