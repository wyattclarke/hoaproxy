# HOA Governing Document Discovery Playbook

This is the current repeatable system for starting a new state and scraping public HOA governing documents into the document bank.

The goal is not to make an LLM browse the web directly. The goal is to use cheap deterministic code for search, fetching, PDF harvesting, deduping, and banking, then use OpenRouter only where judgment is valuable: search strategy and noisy lead validation.

## System Shape

The bank is the raw GCS sink:

```text
gs://hoaproxy-bank/v1/{STATE}/{county}/{hoa-slug}/manifest.json
gs://hoaproxy-bank/v1/{STATE}/{county}/{hoa-slug}/doc-{sha[:12]}/original.pdf
```

Use `hoaware.bank.bank_hoa()` as the only write API. Discovery code should produce `Lead` objects and let `hoaware.discovery.probe.probe()` fetch public pages, harvest document links, download PDFs, and bank them.

Key files:

- `hoaware/bank.py`: GCS banking and dedup.
- `hoaware/discovery/leads.py`: `Lead` dataclass.
- `hoaware/discovery/probe.py`: public page probe, PDF harvest, bank write.
- `benchmark/scrape_ks_serper_docpages.py`: deterministic Serper search -> noisy candidate leads.
- `benchmark/openrouter_ks_planner.py`: OpenRouter county query generation and lead validation.
- `benchmark/run_ks_openrouter_discovery.py`: OpenRouter-assisted direct PDF candidate triage and banking.

## Recommended Workflow For A New State

Start county by county, then pivot into host-focused search once a productive host appears. Statewide queries are too noisy when they are broad, but statewide searches constrained to a high-signal host pattern can be very productive.

Recommended first counties are the highest HOA-density counties, usually suburbs around the largest metros.

For each county:

1. Generate or write county-focused queries.
2. Run deterministic Serper discovery in discovery-only mode.
3. Validate noisy candidate leads with OpenRouter.
4. Probe only validated leads.
5. If page probes do not find PDFs, run direct-PDF OpenRouter triage for that county.
6. Record yield and move to the next county.

After the first productive counties, switch from county sweeps to source-family expansion:

1. Identify hosts that actually banked documents.
2. Search those hosts directly with city/state/document terms.
3. Deduplicate already-seen URLs before validation.
4. Validate the new candidates with OpenRouter.
5. Probe one lead at a time with a timeout if the host sometimes hangs.

## Environment

Activate the repo venv and load `settings.env` for API keys and GCP credentials:

```bash
source .venv/bin/activate
set -a; source settings.env; set +a
export GOOGLE_CLOUD_PROJECT=hoaware
```

Required keys:

- `SERPER_API_KEY`: search API.
- `OPENROUTER_API_KEY`: model strategy/validation/triage.
- GCP application credentials or ADC that can write `gs://hoaproxy-bank/`.

Do not echo secrets. Do not scrape logged-in portals, private dashboards, resident data, email, payment pages, or IBM/work data.

## Model Strategy

Use OpenRouter for compact judgment tasks, not browsing.

Current recommended models:

- Strategy/query generation: `google/gemini-3.1-pro-preview`.
- Strict lead validation: `google/gemini-3.1-pro-preview`, fallback to `moonshotai/kimi-k2.6`.
- Cheap direct PDF triage: `deepseek/deepseek-v4-flash` or `deepseek/deepseek-v4-pro` when not rate-limited.
- Best judgment fallback: `anthropic/claude-opus-latest` / `anthropic/claude-opus-4.7`, used sparingly.

Practical finding from Kansas: OpenRouter does not reduce token count by itself. The architecture reduces tokens. Code searches and extracts; models only see compact URL/title/snippet/candidate JSON.

## County Query Generation

Use Gemini to generate county-specific queries:

```bash
OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/openrouter_ks_planner.py county-queries \
  --county Sedgwick \
  --count 30 \
  --output benchmark/results/ks_sedgwick_gemini_queries.txt \
  --model google/gemini-3.1-pro-preview \
  --fallback-model deepseek/deepseek-v4-pro
```

For another state, either generalize the script arguments or create a state-specific copy. The desired query pattern is:

```text
"{County} County" "{STATE_NAME}" "HOA Documents" bylaws
"{City}" "{STATE_NAME}" "Governing Documents" HOA
"{City}" "{STATE_NAME}" "Homes Association" "Declaration of Restrictions"
site:*.org "{City}" "governing documents" HOA
site:*.com "{City}" "HOA Documents" bylaws
```

For Kansas, “homes association,” “deed restrictions,” and “declaration of restrictions” were high-value phrases.

## Deterministic Candidate Discovery

Run the Serper doc-page scraper without probing first:

```bash
python benchmark/scrape_ks_serper_docpages.py \
  --run-id sedgwick_gemini_1 \
  --queries-file benchmark/results/ks_sedgwick_gemini_queries.txt \
  --max-queries 30 \
  --results-per-query 10 \
  --pages-per-query 1 \
  --max-leads 60 \
  --min-score 8 \
  --search-delay 0.15
```

Output:

```text
benchmark/results/ks_serper_docpages_{run-id}/leads.jsonl
benchmark/results/ks_serper_docpages_{run-id}/audit.jsonl
benchmark/results/ks_serper_docpages_{run-id}/summary.json
```

This file is noisy by design. Do not blindly probe it. It can include legal-info pages, news, government pages, malformed HOA names, and out-of-state pages.

## Host-Focused Expansion

When a source family starts yielding, focus on it directly. This was the biggest Kansas improvement.

For eNeighbors-style public pages:

```text
site:eneighbors.com Kansas HOA documents covenants
site:eneighbors.com Kansas "Homeowners Association" "documents"
site:eneighbors.com/!h_ "public-document" "Kansas" "Homeowners Association"
site:eneighbors.com/p/ "Overland Park" HOA
site:eneighbors.com/p/ "Olathe" HOA
site:eneighbors.com/p/ "Leawood" HOA
```

For municipal document centers:

```text
site:.gov/DocumentCenter/View Kansas "Homeowners Association" "Declaration"
site:.gov/DocumentCenter/View Kansas "Declaration of Restrictions" subdivision
site:.gov/AgendaCenter/ViewFile Kansas "Homeowners Association" "Declaration"
site:bucoks.gov/DocumentCenter/View "Declaration" "Association"
site:manhattanks.gov/DocumentCenter/View "Homeowners Association"
site:wichita.gov/Archive.aspx "Homeowners Association"
```

For management-company community pages:

```text
site:cobaltreks.com/hoa-management "HOA" "Covenants"
site:cobaltreks.com/hoa-management "Declaration" "HOA"
site:cobaltreks.com/hoa-management "Kansas" "Homeowners Association"
```

For independent community domains after a productive metro is found:

```text
"Kansas" "HOA documents" "bylaws" -eneighbors
"Kansas" "governing documents" "homeowners association" -eneighbors
"Overland Park" "HOA documents" -eneighbors
"Leawood" "homes association" documents -eneighbors
"Wichita" "homeowners association" "documents" -eneighbors
```

Always dedupe against previous validated/probed URLs before validation. A cheap local dedupe step saved model spend in Kansas once eNeighbors started returning repeats.

## OpenRouter Lead Validation

Validate noisy candidates before banking:

```bash
OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/openrouter_ks_planner.py validate-leads \
  benchmark/results/ks_serper_docpages_sedgwick_gemini_1/leads.jsonl \
  --output benchmark/results/ks_sedgwick_gemini_validated_1.jsonl \
  --audit benchmark/results/ks_sedgwick_gemini_validated_1_audit.json \
  --county Sedgwick \
  --model google/gemini-3.1-pro-preview \
  --fallback-model moonshotai/kimi-k2.6 \
  --batch-size 8
```

The validator should:

- Keep only specific community/HOA leads.
- Repair malformed names when the URL/title clearly identifies the HOA.
- Reject generic legal pages, social posts, government pages, attorney pages, news, and generic management marketing pages.
- Prefer public governing-doc/document pages over generic home pages.

If a model returns malformed JSON or rate-limits, rerun with smaller `--batch-size` and a different fallback model.

## Probe Validated Leads

Probe only the validated lead file:

```bash
python -m hoaware.discovery --json probe-batch \
  benchmark/results/ks_sedgwick_gemini_validated_1.jsonl
```

For hosts that can hang, probe one lead at a time with a subprocess timeout. eNeighbors and some WordPress sites can otherwise stop a long batch on one URL.

Count current bank coverage:

```bash
gsutil ls 'gs://hoaproxy-bank/v1/KS/**/manifest.json' 2>/dev/null | wc -l
gsutil ls -r 'gs://hoaproxy-bank/v1/KS/' 2>/dev/null | grep '/original.pdf$' | wc -l
```

State path changes are automatic if leads have `state="{STATE}"`.

## Direct PDF Escalation

If validated pages create clean HOA manifests but do not bank PDFs, use the direct PDF discovery harness on the same county query file:

```bash
OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/run_ks_openrouter_discovery.py \
  --models google/gemini-3.1-pro-preview \
  --run-id gemini_sedgwick_pdf_1 \
  --queries-file benchmark/results/ks_sedgwick_gemini_queries.txt \
  --skip-seed-queries \
  --model-queries 0 \
  --max-queries 30 \
  --results-per-query 10 \
  --pages-per-query 1 \
  --max-results 120 \
  --max-pages 70 \
  --max-pdfs 25 \
  --triage-batch-size 4 \
  --search-delay 0.15
```

Use this only after page discovery flattens, because it is more likely to infer names from PDFs and needs stricter triage.

## Current Kansas Lessons

High-yield:

- Host-focused expansion after the first hit. eNeighbors was much better than continuing low-density county sweeps.
- eNeighbors public-document URLs and `/p/{community}` pages. Many direct public-document URLs bank exactly one PDF; community pages can bank multiple PDFs.
- Independent community domains found with `-eneighbors` searches after eNeighbors flattened. This found many Johnson County sites and some Sedgwick/Butler/Leavenworth sites.
- Municipal document centers that serve PDF bytes from non-`.pdf` URLs, especially `DocumentCenter/View` and archive URLs.
- County/city focused queries for finding the first productive hosts.
- Public community websites with pages named documents, governing documents, bylaws, restrictions, deed restrictions, HOA documents.
- Deterministic management/association directories when available.
- OpenRouter validation before probing noisy candidates.

Low-yield or risky:

- Broad statewide search.
- Broad county sweeps after the main metro counties are exhausted. Several Kansas counties produced candidates but zero validated leads.
- HA-KC pages as currently probed. They create many plausible HOA manifests but mostly expose skipped/non-PDF links; use them as lead discovery unless a custom parser is added.
- Exact HOA-name search from city lists without document-page hints.
- Raw direct-PDF search without strong name evidence.
- Generic legal-info sites and government packets.
- Querying too much with cheap models that produce bloated completions.

Observed model behavior:

- `google/gemini-3.1-pro-preview` is good at county query generation and strict validation, but can occasionally return malformed/truncated JSON. Smaller batches and retry/fallback help.
- `deepseek/deepseek-v4-flash` is cheap and usable for PDF triage, but can hit upstream OpenRouter 429s.
- `qwen/qwen3.5-flash` was cheap but token-wastey and worse at clean HOA names.
- `moonshotai/kimi-k2.6` is a reasonable fallback when DeepSeek is rate-limited.

## Handoff Checklist

Before moving to the next state or context:

1. Commit reusable code and docs.
2. Do not commit `benchmark/results/` unless a specific result artifact is intentionally needed.
3. Record:
   - state
   - counties attempted
   - query files
   - validated lead files
   - bank count before/after
   - model spend or token usage when available
   - false-positive patterns to block next time
4. Leave long-running probes only if the next context is told exactly how to monitor them.

Useful process checks:

```bash
ps -fA | rg 'hoaware.discovery|run_ks_openrouter_discovery|scrape_ks_serper|openrouter_ks_planner'
```

Useful result inspection:

```bash
find benchmark/results -maxdepth 2 -name summary.json -o -name '*_audit.json'
```

## Autonomy Failure Mode

Do not treat the assistant turn boundary as a blocker. A final response stops the current execution turn; it is not itself a valid reason to stop autonomous scraping.

If the user has asked for autonomous scraping, only send a final response when there is a real blocker, the explicit budget is exhausted, or the user asks for status. Otherwise:

1. Check for active scrape/probe/model processes.
2. Record any useful handoff state in committed docs or small source changes.
3. Launch the next concrete scrape/probe/validation step.
4. Use short commentary updates while work continues.

Bad pattern:

```text
Blocked by the turn boundary.
```

Correct pattern:

```text
No real blocker; continue with the next county, host family, or probe batch.
```
