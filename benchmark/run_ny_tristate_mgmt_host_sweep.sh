#!/usr/bin/env bash
# Driver C — NY/NJ/CT tri-state management-company host-expansion sweep.
#
# Reads state_scrapers/ny/leads/ny_tristate_management_company_domains.json, generates site:<domain>
# queries with NY-aware governing-doc patterns, sweeps via Serper, validates,
# cleans direct-PDF leads, and probes/banks. Post-OCR state detection routes
# each hit to its correct NY/NJ/CT bank prefix.
#
# Adapted from benchmark/run_fl_mgmt_host_sweep.sh.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOMAINS_JSON="$ROOT/state_scrapers/ny/leads/ny_tristate_management_company_domains.json"
RESULTS="$ROOT/benchmark/results/ny_tristate_mgmt_host"
QUERIES="$RESULTS/queries.txt"
RUN_ID="ny_tristate_mgmt_host_1"

mkdir -p "$RESULTS"
cd "$ROOT"
source .venv/bin/activate
export GOOGLE_CLOUD_PROJECT=hoaware
export HOA_DISCOVERY_RESPECT_ROBOTS=1
export PYTHONUNBUFFERED=1

# Step 1: Generate queries from each verified domain.
# NY-specific patterns: cover coop offering plan + proprietary lease + condo
# declaration + HOA covenants.
python3 - "$DOMAINS_JSON" "$QUERIES" <<'PY'
import json, sys
from pathlib import Path
domains_file = Path(sys.argv[1]); queries_file = Path(sys.argv[2])
data = json.loads(domains_file.read_text())
PATTERNS = [
    'site:{domain} "Declaration of Condominium" filetype:pdf',
    'site:{domain} "Offering Plan" filetype:pdf',
    'site:{domain} "Proprietary Lease" filetype:pdf',
    'site:{domain} "By-Laws" OR "Bylaws" filetype:pdf',
    'site:{domain} "Declaration of Covenants" filetype:pdf',
    'site:{domain} "House Rules" filetype:pdf',
    'site:{domain} "Master Deed" filetype:pdf',
    'site:{domain} governing documents condominium',
    'site:{domain} cooperative apartment corporation by-laws',
]
lines = [f"# NY tri-state mgmt-co sweep — generated from {domains_file.name}"]
for company, info in data.items():
    if company.startswith("_"):
        continue
    domain = info["domain"]
    lines.append(f"\n# {company} ({domain})")
    for pat in PATTERNS:
        lines.append(pat.format(domain=domain))
queries_file.write_text("\n".join(lines) + "\n")
total = sum(1 for l in lines if l and not l.startswith("#"))
print(f"Wrote {total} queries for {len(data) - sum(1 for k in data if k.startswith('_'))} domains to {queries_file}")
PY
echo "Query file: $QUERIES"; wc -l "$QUERIES"

# Step 2: Serper search. No --county; cleaner will derive county from PDF text.
# --default-county "_unknown-county" — post-OCR detection assigns real county.
# We default state=NY; cross-state hits get rerouted at Phase 5b.
python benchmark/scrape_state_serper_docpages.py \
  --state NY --state-name "New York" \
  --default-county "_unknown-county" \
  --run-id "$RUN_ID" \
  --queries-file "$QUERIES" \
  --max-queries 1500 --results-per-query 10 --pages-per-query 1 \
  --max-leads 2500 --min-score 4 --search-delay 0.30 \
  --include-direct-pdfs

SERPER_OUT="$ROOT/benchmark/results/ny_serper_docpages_${RUN_ID}"
LEADS="$SERPER_OUT/leads.jsonl"
AUDIT="$SERPER_OUT/audit.jsonl"

# Step 3: Validate leads (no --county; statewide acceptance)
VAL="$RESULTS/validated.jsonl"
VAL_AUDIT="$RESULTS/validated_audit.json"
OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/openrouter_ks_planner.py validate-leads \
  "$LEADS" --output "$VAL" --audit "$VAL_AUDIT" \
  --state NY \
  --model deepseek/deepseek-v4-flash \
  --batch-size 20 || true

# Dedup validated rows against ALL prior NY/NJ/CT results.
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

# Step 4: clean_direct_pdf_leads — detects state/county from PDF text via
# detect_state_county. --state NY is the default; cross-state hits route
# properly via OCR-detected state.
CLEAN="$RESULTS/cleaned.jsonl"
CLEAN_REJ="$RESULTS/cleaned_rejects.jsonl"
python benchmark/clean_direct_pdf_leads.py "$LEADS" \
  --audit "$AUDIT" \
  --output "$CLEAN" --rejects "$CLEAN_REJ" \
  --state NY --state-name "New York" \
  --max-pages 5 --max-output 500 --delay 0.4 || true

python3 - "$CLEAN" "$RESULTS/cleaned_dedup.jsonl" <<'PY'
import json, sys, glob
inp, out = sys.argv[1], sys.argv[2]
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
    clean.append(r)
with open(out, "w") as f:
    for r in clean:
        f.write(json.dumps(r, sort_keys=True) + "\n")
print(f"cleaned_in={len(rows)} cleaned_dedup={len(clean)}", file=sys.stderr)
PY

# Step 5: Probe combined leads — bank_hoa() routes by state/county.
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
  echo "no leads to probe for ny_tristate_mgmt_host" >&2
fi
