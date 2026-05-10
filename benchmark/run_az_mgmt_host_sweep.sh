#!/usr/bin/env bash
# Driver C — AZ management-company host sweep. Iterates verified mgmt-co
# domains and runs site:<domain> queries for governing-doc patterns. Lower
# expected yield in AZ than NC/IN because AZ mgmt cos gate documents behind
# HomeWiseDocs/CondoCerts/Nabr/Buurt — but cheap to run.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOMAINS_JSON="$ROOT/state_scrapers/az/leads/az_management_company_domains.json"
RESULTS="$ROOT/benchmark/results/az_mgmt_host"
QUERIES="$RESULTS/queries.txt"
RUN_ID="az_mgmt_host_1"

mkdir -p "$RESULTS"
cd "$ROOT"
source .venv/bin/activate
export GOOGLE_CLOUD_PROJECT=hoaware
export HOA_DISCOVERY_RESPECT_ROBOTS=1
export PYTHONUNBUFFERED=1

python3 - "$DOMAINS_JSON" "$QUERIES" <<'PY'
import json, sys
from pathlib import Path
domains_file = Path(sys.argv[1]); queries_file = Path(sys.argv[2])
data = json.loads(domains_file.read_text())
PATTERNS = [
    'site:{domain} "Declaration of Covenants" filetype:pdf',
    'site:{domain} "Master Deed" filetype:pdf',
    'site:{domain} "Bylaws" "Homeowners" filetype:pdf',
    'site:{domain} "Articles of Incorporation" filetype:pdf',
    'site:{domain} "Declaration of Condominium" filetype:pdf',
    'site:{domain} "Restated Declaration" filetype:pdf',
    'site:{domain} "CC&Rs" filetype:pdf',
    'site:{domain} "planned community" "Declaration" filetype:pdf',
    'site:{domain} documents Arizona HOA',
]
lines = [f"# AZ management-company host sweep — generated from {domains_file.name}"]
domain_count = 0
for company, info in data.items():
    if company.startswith("_"):
        continue
    for d in [info["domain"]] + info.get("extra_domains", []):
        domain_count += 1
        lines.append(f"\n# {company} ({d})")
        for pat in PATTERNS:
            lines.append(pat.format(domain=d))
queries_file.write_text("\n".join(lines) + "\n")
total = sum(1 for l in lines if l and not l.startswith("#"))
print(f"Wrote {total} queries for {domain_count} domains to {queries_file}")
PY
echo "Query file: $QUERIES"; wc -l "$QUERIES"

python benchmark/scrape_state_serper_docpages.py \
  --state AZ --state-name Arizona \
  --default-county "_unknown-county" \
  --run-id "$RUN_ID" \
  --queries-file "$QUERIES" \
  --max-queries 2000 --results-per-query 10 --pages-per-query 1 \
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
  echo "no leads to probe for az_mgmt_host" >&2
fi
