#!/usr/bin/env bash
# Driver E — AZ master-planned community pass. For each curated community,
# generates per-community Serper queries (anchored on the community name +
# alt_names + suspected portal domains) plus site:<portal> probes. Yields
# rich governing-doc results for AZ's iconic master-planned communities
# (Sun City, DC Ranch, Verrado, Estrella, Eastmark, etc.).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMMUNITIES_JSON="$ROOT/state_scrapers/az/leads/az_master_planned.json"
RESULTS="$ROOT/benchmark/results/az_master_planned"
QUERIES="$RESULTS/queries.txt"
RUN_ID="az_master_planned_1"

mkdir -p "$RESULTS"
cd "$ROOT"
source .venv/bin/activate
export GOOGLE_CLOUD_PROJECT=hoaware
export HOA_DISCOVERY_RESPECT_ROBOTS=1
export PYTHONUNBUFFERED=1

python3 - "$COMMUNITIES_JSON" "$QUERIES" <<'PY'
import json, sys
from pathlib import Path
src = Path(sys.argv[1]); out = Path(sys.argv[2])
data = json.loads(src.read_text())
NAME_PATTERNS = [
    '"{name}" "Arizona" "Declaration" filetype:pdf',
    '"{name}" "Arizona" "Covenants" filetype:pdf',
    '"{name}" "Arizona" "Bylaws" filetype:pdf',
    '"{name}" "Arizona" "Articles of Incorporation" filetype:pdf',
    '"{name}" "Arizona" "Declaration of Condominium" filetype:pdf',
    '"{name}" "Arizona" "CC&Rs" filetype:pdf',
    '"{name}" "Arizona" governing documents',
]
SITE_PATTERNS = [
    'site:{domain} "Declaration"',
    'site:{domain} "Covenants" filetype:pdf',
    'site:{domain} "Bylaws" filetype:pdf',
    'site:{domain} "Articles" filetype:pdf',
    'site:{domain} "CC&Rs" OR "CCR" filetype:pdf',
    'site:{domain} "Master Deed" filetype:pdf',
    'site:{domain} governing documents',
]
lines = [f"# AZ master-planned community sweep — generated from {src.name}"]
for entry in data:
    names = [entry["name"]] + entry.get("alt_names", [])
    for n in names:
        if not n or len(n) < 6:
            continue
        for pat in NAME_PATTERNS:
            lines.append(pat.format(name=n))
    for url in entry.get("suspected_portal_urls", []):
        # Extract bare domain
        u = url
        for prefix in ("https://", "http://"):
            if u.startswith(prefix):
                u = u[len(prefix):]
        domain = u.split("/", 1)[0].lower()
        if not domain or "." not in domain:
            continue
        for pat in SITE_PATTERNS:
            lines.append(pat.format(domain=domain))
out.write_text("\n".join(lines) + "\n")
total = sum(1 for l in lines if l and not l.startswith("#"))
print(f"Wrote {total} queries for {len(data)} master-planned communities to {out}")
PY
echo "Query file: $QUERIES"; wc -l "$QUERIES"

python benchmark/scrape_state_serper_docpages.py \
  --state AZ --state-name Arizona \
  --default-county "_unknown-county" \
  --run-id "$RUN_ID" \
  --queries-file "$QUERIES" \
  --max-queries 5000 --results-per-query 8 --pages-per-query 1 \
  --max-leads 0 --min-score 4 --search-delay 0.25 \
  --include-direct-pdfs

SERPER_OUT="$ROOT/benchmark/results/az_serper_docpages_${RUN_ID}"
LEADS="$SERPER_OUT/leads.jsonl"
AUDIT="$SERPER_OUT/audit.jsonl"

VAL="$RESULTS/validated.jsonl"
VAL_AUDIT="$RESULTS/validated_audit.json"
OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/openrouter_ks_planner.py validate-leads \
  "$LEADS" --output "$VAL" --audit "$VAL_AUDIT" \
  --state AZ \
  --model deepseek/deepseek-v4-flash \
  --fallback-model moonshotai/kimi-k2.6 \
  --batch-size 20 || true

python3 - "$VAL" "$RESULTS/validated_clean.jsonl" <<'PY'
import json, sys, re, glob
inp, out = sys.argv[1], sys.argv[2]
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
                for u in r.get("pre_discovered_pdf_urls", []) + [r.get("source_url") or "", r.get("website") or ""]:
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

python3 - "$CLEAN" "$RESULTS/cleaned_dedup.jsonl" <<'PY'
import json, sys, glob
inp, out = sys.argv[1], sys.argv[2]
seen = set()
for path in glob.glob("benchmark/results/az_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/az_*/cleaned_dedup.jsonl") + \
            glob.glob("benchmark/results/fl_*/validated_clean.jsonl") + \
            glob.glob("benchmark/results/fl_*/cleaned_dedup.jsonl"):
    try:
        with open(path) as f:
            for line in f:
                r = json.loads(line)
                for u in r.get("pre_discovered_pdf_urls", []) + [r.get("source_url") or "", r.get("website") or ""]:
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
  echo "no leads to probe for az_master_planned" >&2
fi
