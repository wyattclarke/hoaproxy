#!/usr/bin/env bash
# NY Driver A: per-county registry-name × Serper sweep.
# Adapted from benchmark/run_fl_sunbiz_county_sweep.sh.
#
# Usage:
#   benchmark/run_ny_registry_county_sweep.sh <CountyName>
#
# Example:
#   benchmark/run_ny_registry_county_sweep.sh "Kings"
#   benchmark/run_ny_registry_county_sweep.sh "New York"
#
# County names are NY legal county names (Kings/Queens/New York/Richmond/Bronx
# for NYC boroughs).

set -euo pipefail

COUNTY="${1:?county name required (e.g. Kings, New York, Queens)}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SAFE="${COUNTY// /_}"
RESULTS="$ROOT/benchmark/results/ny_registry_${SAFE}"
QUERIES="$RESULTS/queries.txt"
SERPER_RUN="ny_registry_${SAFE}_1"
mkdir -p "$RESULTS"

cd "$ROOT"
source .venv/bin/activate
export GOOGLE_CLOUD_PROJECT=hoaware
export HOA_DISCOVERY_RESPECT_ROBOTS=1
export PYTHONUNBUFFERED=1

# Build per-name × per-county queries.
python state_scrapers/ny/scripts/ny_build_county_queries.py \
  --county "$COUNTY" \
  --output "$QUERIES" \
  --max-queries-per-county 1500

if [ ! -s "$QUERIES" ]; then
  echo "no queries built for $COUNTY (no seeds?)" >&2
  exit 0
fi

# Serper sweep. 5 results/query — name-anchored, no need for more.
python benchmark/scrape_state_serper_docpages.py \
  --state NY --state-name "New York" \
  --county "$COUNTY" \
  --default-county "$COUNTY" \
  --run-id "$SERPER_RUN" \
  --queries-file "$QUERIES" \
  --max-queries 1500 --results-per-query 5 --pages-per-query 1 \
  --max-leads 1500 --min-score 5 --search-delay 0.30 \
  --include-direct-pdfs

LEADS="$ROOT/benchmark/results/ny_serper_docpages_${SERPER_RUN}/leads.jsonl"
AUDIT="$ROOT/benchmark/results/ny_serper_docpages_${SERPER_RUN}/audit.jsonl"

VAL="$RESULTS/validated.jsonl"
VAL_AUDIT="$RESULTS/validated_audit.json"
OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/openrouter_ks_planner.py validate-leads \
  "$LEADS" --output "$VAL" --audit "$VAL_AUDIT" \
  --state NY --county "$COUNTY" \
  --model deepseek/deepseek-v4-flash \
  --batch-size 20 || true

# Dedup validated leads against all prior NY/NJ/CT (tri-state metro)
# and FL/GA results that may share name patterns.
python3 - "$VAL" "$RESULTS/validated_clean.jsonl" "$COUNTY" <<'PY'
import json, sys, re, glob
inp, out, county = sys.argv[1], sys.argv[2], sys.argv[3]
JUNK = re.compile(
    r"(temporary_breach|/newsletter|/minutes|/agenda|/budget|/financial|/audit|"
    r"/rental|/lease|pool[-_ ]rules|pool[-_ ]pass|directory|roster|violation|"
    r"estoppel|closing|coupon|listing|for[-_ ]sale|reminder|history|"
    r"estate[-_ ]sale|application[-_ ]form|payment|invoice|fee[-_ ]schedule|"
    r"election|nomination|/Legislation/|/AgendaCenter/|/Council/|/Planning/|"
    r"caionline|hoaleader|ag\.ny\.gov/(?!real-estate))",
    re.I,
)
seen = set()
for pattern in (
    "benchmark/results/ny_*_clean*.jsonl",
    "benchmark/results/ny_*/validated_clean.jsonl",
    "benchmark/results/ny_*/cleaned_dedup.jsonl",
    "benchmark/results/nj_*/validated_clean.jsonl",
    "benchmark/results/nj_*/cleaned_dedup.jsonl",
    "benchmark/results/ct_*/validated_clean.jsonl",
    "benchmark/results/ct_*/cleaned_dedup.jsonl",
):
    for path in glob.glob(pattern):
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
  --state NY --state-name "New York" \
  --max-pages 5 --max-output 250 --delay 0.4 || true

# Dedup direct-PDF leads against all prior NY/NJ/CT results.
python3 - "$CLEAN" "$RESULTS/cleaned_dedup.jsonl" "$COUNTY" <<'PY'
import json, sys, re, glob
inp, out, county = sys.argv[1], sys.argv[2], sys.argv[3]
seen = set()
for pattern in (
    "benchmark/results/ny_*/validated_clean.jsonl",
    "benchmark/results/ny_*/cleaned_dedup.jsonl",
    "benchmark/results/nj_*/validated_clean.jsonl",
    "benchmark/results/nj_*/cleaned_dedup.jsonl",
    "benchmark/results/ct_*/validated_clean.jsonl",
    "benchmark/results/ct_*/cleaned_dedup.jsonl",
):
    for path in glob.glob(pattern):
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
