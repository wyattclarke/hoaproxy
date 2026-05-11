#!/usr/bin/env bash
# Driver A: per-county CA sweep using registry-anchored HOA seed names for
# high-precision targeted queries. Complement to Driver B
# (run_ca_county_sweep_v2.sh) which uses broad county/city phrases.
#
# Usage:
#   benchmark/run_ca_corp_county_sweep.sh <CountySlug> <CountyDisplayName>
#
# Example:
#   benchmark/run_ca_corp_county_sweep.sh los-angeles "Los Angeles"

set -euo pipefail

SLUG="${1:?county slug required (e.g. los-angeles)}"
DISPLAY="${2:?county display name required (e.g. Los Angeles)}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RESULTS="$ROOT/benchmark/results/ca_corp_${SLUG}"
QUERIES="$RESULTS/queries.txt"
SERPER_RUN="ca_corp_${SLUG}_1"
mkdir -p "$RESULTS"

cd "$ROOT"
source .venv/bin/activate
export GOOGLE_CLOUD_PROJECT=hoaware
export HOA_DISCOVERY_RESPECT_ROBOTS=1
export PYTHONUNBUFFERED=1

LEADS="$ROOT/benchmark/results/ca_serper_docpages_${SERPER_RUN}/leads.jsonl"
AUDIT="$ROOT/benchmark/results/ca_serper_docpages_${SERPER_RUN}/audit.jsonl"
VAL="$RESULTS/validated.jsonl"
VAL_AUDIT="$RESULTS/validated_audit.json"

# Stage 1: Serper discovery (idempotent — skip if leads.jsonl populated).
if [ -s "$LEADS" ]; then
  echo "[resume] $DISPLAY: leads.jsonl exists ($(wc -l < "$LEADS") leads); skip Serper" >&2
else
  # Build targeted per-HOA-name queries from the CA registry.
  python state_scrapers/ca/scripts/ca_build_corp_county_queries.py \
    --county "$SLUG" \
    --output "$QUERIES" \
    --max-queries-per-county 1500

  # Run Serper scraper with HOA-name-targeted settings.
  python benchmark/scrape_state_serper_docpages.py \
    --state CA --state-name California \
    --county "$DISPLAY" \
    --default-county "$DISPLAY" \
    --run-id "$SERPER_RUN" \
    --queries-file "$QUERIES" \
    --max-queries 1500 --results-per-query 5 --pages-per-query 1 \
    --max-leads 1500 --min-score 5 --search-delay 0.25 \
    --include-direct-pdfs
fi

# Stage 2: validate-leads (idempotent — skip if validated.jsonl populated).
if [ -s "$VAL" ]; then
  echo "[resume] $DISPLAY: validated.jsonl exists ($(wc -l < "$VAL") rows); skip validate" >&2
else
  OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/openrouter_ks_planner.py validate-leads \
    "$LEADS" --output "$VAL" --audit "$VAL_AUDIT" \
    --state CA --county "$DISPLAY" \
    --model deepseek/deepseek-v4-flash \
    --batch-size 20 || true
fi

# Dedup validated leads against all prior CA, FL, GA results.
python3 - "$VAL" "$RESULTS/validated_clean.jsonl" "$DISPLAY" <<'PY'
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
import os as _os
_out_abs = _os.path.abspath(out)
for path in (
    glob.glob("benchmark/results/ga_*/validated_clean.jsonl") +
    glob.glob("benchmark/results/ga_*/cleaned_dedup.jsonl") +
    glob.glob("benchmark/results/fl_*/validated_clean.jsonl") +
    glob.glob("benchmark/results/fl_*/cleaned_dedup.jsonl") +
    glob.glob("benchmark/results/ca_*/validated_clean.jsonl") +
    glob.glob("benchmark/results/ca_*/cleaned_dedup.jsonl")
):
    if _os.path.abspath(path) == _out_abs:
        continue  # self-dedup guard: skip current run's own output
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

python3 - "$CLEAN" "$RESULTS/cleaned_dedup.jsonl" "$DISPLAY" <<'PY'
import json, sys, re, glob
inp, out, county = sys.argv[1], sys.argv[2], sys.argv[3]
seen = set()
import os as _os
_out_abs = _os.path.abspath(out)
for path in (
    glob.glob("benchmark/results/ga_*/validated_clean.jsonl") +
    glob.glob("benchmark/results/ga_*/cleaned_dedup.jsonl") +
    glob.glob("benchmark/results/fl_*/validated_clean.jsonl") +
    glob.glob("benchmark/results/fl_*/cleaned_dedup.jsonl") +
    glob.glob("benchmark/results/ca_*/validated_clean.jsonl") +
    glob.glob("benchmark/results/ca_*/cleaned_dedup.jsonl")
):
    if _os.path.abspath(path) == _out_abs:
        continue  # self-dedup guard: skip current run's own output
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

if [ -s "$RESULTS/probe.jsonl" ]; then
  echo "[resume] $DISPLAY: probe.jsonl exists ($(wc -l < "$RESULTS/probe.jsonl") rows); skip probe" >&2
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
  echo "no leads to probe for $DISPLAY" >&2
fi
