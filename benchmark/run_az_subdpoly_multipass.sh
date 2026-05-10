#!/usr/bin/env bash
# Driver A' multi-pass: sweep all unique subdpoly names for one AZ county by
# running successive passes of run_az_subdpoly_county_sweep.sh with --offset.
#
# Each pass writes to az_subdpoly_<slug>_pass<N>/ to avoid the idempotency
# skip. The build_subdpoly_county_queries.py script supports --offset N to
# resume from a name index. We use --max-queries=5000 per pass (= 2500 names),
# which keeps Serper batches a manageable size and lets us fail-fast per-pass.
#
# Usage:
#   benchmark/run_az_subdpoly_multipass.sh <county-slug> <CountyDisplay>
#                                          <unique-name-count> [pass-size]
#
# Example:
#   benchmark/run_az_subdpoly_multipass.sh maricopa Maricopa 25201
#   benchmark/run_az_subdpoly_multipass.sh pima Pima 5744

set -euo pipefail

SLUG="${1:?county slug required}"
DISPLAY="${2:?county display required}"
UNIQUE_NAMES="${3:?unique name count required}"
PASS_SIZE="${4:-2500}"  # names per pass

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOTAL_PASSES=$(( (UNIQUE_NAMES + PASS_SIZE - 1) / PASS_SIZE ))
LOG_DIR="$ROOT/benchmark/results/az_subdpoly_multipass_${SLUG}_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOG_DIR"

echo "[$(date -u +%H:%M:%S)] Multi-pass for $SLUG ($DISPLAY): $UNIQUE_NAMES names, $PASS_SIZE/pass = $TOTAL_PASSES passes"
echo "Log dir: $LOG_DIR"

cd "$ROOT"

for ((pass=1; pass<=TOTAL_PASSES; pass++)); do
    offset=$(( (pass - 1) * PASS_SIZE ))
    pass_slug="${SLUG}_pass${pass}"
    pass_results="$ROOT/benchmark/results/az_subdpoly_${pass_slug}"
    if [ -s "$pass_results/probe.jsonl" ]; then
        echo "[$(date -u +%H:%M:%S)] SKIP pass $pass (probe.jsonl populated)"
        continue
    fi

    echo "[$(date -u +%H:%M:%S)] === pass $pass/$TOTAL_PASSES (offset=$offset) ==="
    mkdir -p "$pass_results"

    # Build pass-specific query file
    source .venv/bin/activate
    export GOOGLE_CLOUD_PROJECT=hoaware
    export HOA_DISCOVERY_RESPECT_ROBOTS=1
    export PYTHONUNBUFFERED=1

    python state_scrapers/az/scripts/az_build_subdpoly_county_queries.py \
        --county "$SLUG" \
        --output "$pass_results/queries.txt" \
        --max-queries 5000 \
        --offset "$offset"

    SERPER_RUN="az_subdpoly_${pass_slug}_1"
    python benchmark/scrape_state_serper_docpages.py \
        --state AZ --state-name Arizona \
        --county "$DISPLAY" \
        --default-county "$DISPLAY" \
        --run-id "$SERPER_RUN" \
        --queries-file "$pass_results/queries.txt" \
        --max-queries 5000 --results-per-query 5 --pages-per-query 1 \
        --max-leads 0 --min-score 5 --search-delay 0.25 \
        --include-direct-pdfs

    LEADS="$ROOT/benchmark/results/az_serper_docpages_${SERPER_RUN}/leads.jsonl"
    AUDIT="$ROOT/benchmark/results/az_serper_docpages_${SERPER_RUN}/audit.jsonl"

    OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/openrouter_ks_planner.py validate-leads \
        "$LEADS" --output "$pass_results/validated.jsonl" \
        --audit "$pass_results/validated_audit.json" \
        --state AZ --county "$DISPLAY" \
        --model deepseek/deepseek-v4-flash \
        --fallback-model moonshotai/kimi-k2.6 \
        --batch-size 20 || true

    python3 - "$pass_results/validated.jsonl" "$pass_results/validated_clean.jsonl" "$DISPLAY" <<'PY'
import json, sys, re, glob
inp, out, county = sys.argv[1], sys.argv[2], sys.argv[3]
JUNK = re.compile(
    r"(temporary_breach|/newsletter|/minutes|/agenda|/budget|/financial|/audit|"
    r"/rental|/lease|pool[-_ ]rules|pool[-_ ]pass|directory|roster|violation|"
    r"estoppel|closing|coupon|listing|for[-_ ]sale|reminder|history|"
    r"estate[-_ ]sale|application[-_ ]form|payment|invoice|fee[-_ ]schedule|"
    r"election|nomination|/Legislation/|/AgendaCenter/|/Council/|/Planning/|"
    r"caionline|hoaleader)",
    re.I,
)
seen = set()
for path in glob.glob("benchmark/results/az_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/az_*/cleaned_dedup.jsonl") + \
            glob.glob("benchmark/results/fl_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/fl_*/cleaned_dedup.jsonl"):
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

    python benchmark/clean_direct_pdf_leads.py "$LEADS" \
        --audit "$AUDIT" \
        --output "$pass_results/cleaned.jsonl" \
        --rejects "$pass_results/cleaned_rejects.jsonl" \
        --state AZ --state-name Arizona \
        --max-pages 5 --max-output 500 --delay 0.4 || true

    python3 - "$pass_results/cleaned.jsonl" "$pass_results/cleaned_dedup.jsonl" "$DISPLAY" <<'PY'
import json, sys, glob
inp, out, county = sys.argv[1], sys.argv[2], sys.argv[3]
seen = set()
for path in glob.glob("benchmark/results/az_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/az_*/cleaned_dedup.jsonl") + \
            glob.glob("benchmark/results/fl_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/fl_*/cleaned_dedup.jsonl"):
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

    cat "$pass_results/validated_clean.jsonl" "$pass_results/cleaned_dedup.jsonl" 2>/dev/null > "$pass_results/probe_input.jsonl" || true
    if [ -s "$pass_results/probe_input.jsonl" ]; then
        python benchmark/probe_leads_with_pre_discovered.py "$pass_results/probe_input.jsonl" \
            --output "$pass_results/probe.jsonl" \
            --timeout 180 --max-pdfs 12 --delay 1.0 || true
    fi

    echo "[$(date -u +%H:%M:%S)] pass $pass complete"
done

echo "[$(date -u +%H:%M:%S)] ALL passes complete for $SLUG"
