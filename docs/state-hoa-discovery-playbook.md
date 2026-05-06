# HOA Governing Document Discovery Playbook

This is the current repeatable system for starting a new state and scraping public HOA governing documents into the document bank.

The goal is not to make an LLM browse the web directly. The goal is to use cheap deterministic code for search, fetching, PDF harvesting, deduping, and banking, then use OpenRouter only where judgment is valuable: search strategy and noisy lead validation.

## What Counts As A Worthwhile Lead

**Optimize for breadth.** The bank exists so a downstream worker has the largest possible pool of candidate HOAs to draw from. Bank a lead whenever you have at least:

- A plausible **HOA name**, AND
- Either a **town/city or county**, OR a public **document URL** (governing PDF or doc-page).

The HOA must be a **mandatory association created by recorded deed restrictions** — not a voluntary neighborhood/civic association. Mandatory-HOA signals include: Declaration of Covenants, CC&Rs, Restrictive Covenants, Master Deed, "Articles of Incorporation" of an HOA, "Bylaws of <community> Homeowners Association" tied to a recorded declaration. Voluntary-neighborhood signals to *avoid*: standalone "Architectural Guidelines" or "Design Guidelines" with no Declaration/CC&R reference, civic-association meeting minutes, garden-club bylaws.

**"HOA" always includes condos.** Condominium associations (FL Chapter 718, equivalents elsewhere) are mandatory associations created by recorded master deeds + declarations of condominium and are in scope. Bank them under the same `(state, county, slug)` layout as any other HOA. Townhome and master associations are also in scope. Statute-level routing (Chapter 720 vs 718, etc.) is for the drain worker — discovery does not need to distinguish.

When writing search queries, anchor any "architectural" / "design guidelines" / "architectural review" terms to a mandatory-HOA signal in the same query (e.g. `"Architectural Guidelines" "Declaration of Covenants" filetype:pdf`). A bare `"Architectural Guidelines" filetype:pdf` query will pick up voluntary-association docs and pollute the bank.

Manifests with no PDFs are still useful if name+location are present — they tell the drain worker an HOA exists in a place. Manifests with PDFs but malformed names should still be banked; the PDF can be re-named later. The only hard rejects are: generic legal/explainer pages without a specific community, private/walled portal pages, voluntary neighborhood associations, and obvious junk hosts (real-estate listings, attorney marketing, social media, IRS/990 filings).

Do **not** filter for "high quality" by withholding a lead just because the inferred name is messy or the slug is ugly — let it land in the bank with whatever name you have, and clean up names in a separate post-hoc pass (`openrouter_repair_lead_names.py`, then re-bank).

### Out-Of-State And Out-Of-County Hits Are Free Wins, Not Rejects

If a sweep targeted at state X or county X turns up an HOA that's actually in state Y or county Y (mandatory association, real PDF, plausible name), **bank it under the correct Y prefix** — do NOT drop it. We will eventually scrape the whole country, and a TN HOA found by a GA sweep is a future-TN-pass HOA we don't have to discover again.

Concretely:

- The *cleaner* and the *validator* should not hard-reject leads on out-of-state grounds. They should re-route to the correct state's prefix when the evidence is clear (PDF text mentions "X County, Tennessee" or the URL host is a known TN domain) and otherwise let the lead through with `state=null` so the backfill / drain worker can route it later.
- The *probe* writes via `bank_hoa()`, which already takes the lead's `state` and `county`. If discovery code knows the target state/county is wrong, it should overwrite `Lead.state` and `Lead.county` before probing.
- Slugging dedup will handle the case where a TN HOA we banked from a GA sweep is later re-found by a TN sweep — bank merges by `(state, county, slug)`, so the second sighting just appends a new `metadata_source` entry to the existing manifest.
- Implementation status (2026-05-05, updated): `clean_direct_pdf_leads.py` now does this — `detect_state_county()` extracts `(state_abbrev, county_name)` from PDF text and the emitted Lead is stamped with the detected state/county instead of the sweep's target. The validator (`openrouter_ks_planner.py validate-leads`) also asks for repaired state/county fields and preserves clear mandatory-HOA hits outside the sweep scope. Border-metro hits no longer pay the discovery cost twice.

The same logic applies inside a state: a Fulton sweep that finds a Cobb HOA should bank it under `gs://hoaproxy-bank/v1/GA/cobb/<slug>/`, not under `gs://hoaproxy-bank/v1/GA/fulton/<slug>/` and not under `_unknown-county/`. The county scope of a sweep is a *search hint*, not a *banking constraint*. Use the lead's own evidence (city in URL/anchor/PDF text → city→county map → bank under that county) and only fall back to the sweep's `--default-county` when no better signal exists.

The hard requirement to run sweeps county-by-county is unchanged — that's about query scoping and stopping discipline, not about where leads ultimately land.

## State Stopping Rule

Use this rule to decide when to stop active scraping for a state and move effort to the next state.

Stop active discovery only when **two consecutive sweeps** both produce:

- Fewer than 3 net-new valid in-state manifests.
- Fewer than 10 net-new valid in-state PDFs.
- More than 80% rejects, exact duplicates, forms, newsletters, minutes, generic legal/government pages, or out-of-scope records.

A sweep is one concrete executed pass: one query file/source-family/county batch run through discovery, cleaning/validation, and banking or documented rejection. A strategy family is broader, such as `eNeighbors direct URLs`, `hmsft/PMTech`, `HOA Express file/document`, `WordPress uploads`, `county recorder legal phrases`, or a county-focused direct-PDF pattern. Do not stop because one broad family looks weak; require the next concrete sweep to fail the thresholds too.

When the two-sweep rule triggers, do not launch another broad scrape for that state. Allowed follow-up work is cleanup only: duplicate audits, unknown-county repair, name repair, out-of-scope rerouting, and targeted re-mining of already-downloaded result sets that costs no new Serper/OpenRouter budget. Restart active discovery only if a genuinely new high-yield source family appears.

## Always Run County-By-County

**HARD REQUIREMENT.** Every Serper sweep, every validate-leads call, and every probe batch is scoped to one county at a time, with the county passed through end-to-end so manifests land under `gs://hoaproxy-bank/v1/{STATE}/{county}/`. This rule has two reasons:

1. Anything banked statewide lands under `_unknown-county/` and is invisible to downstream county analytics until a backfill re-routes it. Backfill is lossy (the heuristics route ~40% in practice) and creates collisions when the same HOA already exists at the right slug.
2. County scoping gives a clear stopping rule. When county X yields zero new manifests in two consecutive query angles, that county is exhausted — move on. Without scoping, "is this state done" is unanswerable and the assistant ends up sprawling forever.

**Do not launch a statewide `scrape_state_serper_docpages.py` invocation** (no `--default-county` set, broad query file with no `"<County> County"` constraint). Any time you reach for one, treat it as a sign that you've drifted off the rule and stop. The only allowed statewide-shaped operations are:

- Per-HOA enrichment passes that use the *existing* manifest's county (e.g. `scripts/ga_find_owned_website.py`, `scripts/ga_owned_domain_depth.py`). These do not create new manifests; they enrich within the bank's existing county routing.
- A one-time backfill (`scripts/ga_county_backfill.py`) over historical `_unknown-county/` debt — never to legitimize new statewide sweeps.

In practice the per-county pipeline is:

- One queries file per county (or per source-family-within-county). Generate it with `openrouter_ks_planner.py county-queries --county <Name>` or hand-edit a per-county `benchmark/{state}_{county}_queries.txt`.
- `scrape_state_serper_docpages.py --default-county <Name>` so emitted leads carry the county.
- `openrouter_ks_planner.py validate-leads --county <Name>` so the validator stamps the county on every kept lead and the prompt scopes acceptance to that county.
- `clean_direct_pdf_leads.py` should also write `county` on accepted rows (currently leaves it null — see Cross-State Lessons).
- `probe_leads_with_pre_discovered.py` reads `Lead.county` and `bank_hoa()` puts it in the GCS path; you do not have to set anything else once the lead carries the county.

The Serper-cost penalty for repeating productive query patterns once per county is real but small (Serper is ~$1 per 1000 searches; even 150 counties × 30 queries each = $4.50 worst case). Pay it.

If you find yourself looking at a query angle that "works statewide and would be nice to run once", convert it to a per-county loop instead — make a `benchmark/run_ga_<angle>_per_county.sh` that loops the per-county sweep with that angle's queries. Never reach for the statewide form.

If a previous pass left manifests under `_unknown-county/`, treat that as legacy debt: backfill it, but don't take the existence of `_unknown-county/` as license to add more.

This playbook is state-agnostic and harness-agnostic. Replace `{STATE}` (e.g. `GA`, `TN`, `KS`) and `{state-name}` (e.g. `Georgia`) throughout. Code blocks containing literal `Kansas`/`KS` strings are illustrative — they are real queries from the May 2026 Kansas pass and should be translated, not copied. The lessons in "Cross-State Lessons" generalize, even though the named communities are Kansas examples.

For per-state progress, see the matching handoff doc (`ks-discovery-handoff.md`, `tn-discovery-handoff.md`, etc.). When starting a new state, create `docs/{state}-discovery-handoff.md` and update it as you go.

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
- `benchmark/scrape_state_serper_docpages.py`: state-generic deterministic Serper search -> noisy candidate leads. Use this for new states.
- `benchmark/scrape_ks_serper_docpages.py`: legacy Kansas-only variant; kept for reproducibility, do not extend.
- `benchmark/openrouter_ks_planner.py`: OpenRouter county query generation and lead validation. Despite the filename, it accepts `--county` and a queries-file path, so it works for any state.
- `benchmark/run_ks_openrouter_discovery.py`: OpenRouter-assisted direct PDF candidate triage and banking. Same: filename is legacy; arguments are state-agnostic.

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

- Default cheap triage/classification: `deepseek/deepseek-v4-flash`.
- Strategy/query generation: `deepseek/deepseek-v4-flash`.
- Strict lead validation quality/availability fallback: `moonshotai/kimi-k2.6` when DeepSeek is rate-limited/malformed, or for the bounded subset of compact candidates that DeepSeek rejects, cannot name, or scores below the helper's quality threshold after deterministic gates.
- Do not use `google/gemini-3.1-pro-preview` for this workflow; the Kansas activity export showed it was too expensive for the yield.
- Avoid bulk classifier use of `qwen/qwen3.5-flash` and `qwen/qwen3.6-flash`; the Kansas activity export showed runaway hidden reasoning-token usage.
- Best judgment fallback: `anthropic/claude-opus-latest` / `anthropic/claude-opus-4.7`, used sparingly.

Practical finding from Kansas: OpenRouter does not reduce token count by itself. The architecture reduces tokens. Code searches and extracts; models only see compact URL/title/snippet/candidate JSON.

Log all model calls to `HOA_MODEL_USAGE_LOG` (`data/model_usage.jsonl` by default). The log records model, endpoint, generation id, token counts, exact OpenRouter generation metadata when available, latency, operation, compact source metadata, and errors. It must not include prompts, completions, document text, cookies, or API keys.

## County Query Generation

Generate county-specific queries with the cheap default first. Template:

```bash
OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/openrouter_ks_planner.py county-queries \
  --county {County} \
  --count 30 \
  --output benchmark/results/{state-lower}_{county-lower}_deepseek_queries.txt \
  --model deepseek/deepseek-v4-flash
```

Example (Kansas, Sedgwick County) — translate names for your target state:

```bash
OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/openrouter_ks_planner.py county-queries \
  --county Sedgwick \
  --count 30 \
  --output benchmark/results/ks_sedgwick_deepseek_queries.txt \
  --model deepseek/deepseek-v4-flash
```

The discovery scripts block Gemini and Qwen Flash by default through
`HOA_DISCOVERY_MODEL_BLOCKLIST`. Override only for an explicit benchmark, not
for autonomous scraping.

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
  --run-id sedgwick_deepseek_1 \
  --queries-file benchmark/results/ks_sedgwick_deepseek_queries.txt \
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

The query examples below use literal `Kansas` / city names from the May 2026 Kansas pass — substitute your target `{state-name}` and metro names when reusing them. The host patterns themselves (eNeighbors, `DocumentCenter/View`, `cobaltreks.com`, `gogladly.com/connect/document`, `hmsft-doc`, `pmtechsol.sfo2.cdn.digitaloceanspaces.com`, etc.) are nationwide, not Kansas-specific.

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

For host-pattern expansion after direct-PDF hits:

```text
site:gogladly.com/connect/document "Kansas" "homeowners association" bylaws
site:pmtechsol.sfo2.cdn.digitaloceanspaces.com/hmsft-documents "deed restrictions" "Kansas"
inurl:hmsft-doc "Kansas" "homes association" "deed restrictions"
inurl:/file/document/ "Kansas" "homeowners association" covenants
inurl:/wp-content/uploads/ "Kansas" "homeowners association" bylaws
inurl:/wp-content/uploads/ "Kansas" "homes association" restrictions
```

For recorded governing-document phrase searches:

```text
filetype:pdf "Kansas not-for-profit corporation" "Homeowners Association"
filetype:pdf "Kansas non-profit corporation" "Homes Association"
filetype:pdf "Register of Deeds" "Johnson County, Kansas" "Homes Association"
filetype:pdf "Sedgwick County, Kansas" "Declaration of Covenants" "Homeowners Association"
filetype:pdf "Johnson County, Kansas" "Declaration of Restrictions" "Homes Association"
```

This is high precision because bylaws, declarations, and amendments often contain formal corporation language and county recording language. For another state, translate the corporation phrase and swap in the target state/county names.

For late-stage expansion, add amendment/article variants:

```text
filetype:pdf "Articles of Incorporation" "{County} County, {STATE}" "Homeowners Association"
filetype:pdf "Amendment to Declaration" "{County} County, {STATE}" "Homes Association"
filetype:pdf "Restated Bylaws" "{STATE}" "Homeowners Association"
filetype:pdf "Supplemental Declaration" "{STATE}" "Homes Association"
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
  benchmark/results/ks_serper_docpages_sedgwick_deepseek_1/leads.jsonl \
  --output benchmark/results/ks_sedgwick_deepseek_validated_1.jsonl \
  --audit benchmark/results/ks_sedgwick_deepseek_validated_1_audit.json \
  --county Sedgwick \
  --model deepseek/deepseek-v4-flash \
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
  benchmark/results/ks_sedgwick_deepseek_validated_1.jsonl
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
  --models deepseek/deepseek-v4-flash \
  --run-id deepseek_sedgwick_pdf_1 \
  --queries-file benchmark/results/ks_sedgwick_deepseek_queries.txt \
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

For high-signal `filetype:pdf` search runs, a cheaper deterministic variant can beat LLM triage:

1. Run Serper with `--include-direct-pdfs`.
2. Inspect host distribution and only keep HOA-owned or clearly community-specific hosts.
3. Manually clean malformed names and group multiple PDFs under one `Lead`.
4. Probe grouped leads one at a time with a subprocess timeout.
5. If a host times out while crawling its homepage, retry with the same `pre_discovered_pdf_urls` and `website=null` so the probe fetches only the known PDFs.

This worked well in Kansas for public HOA document libraries such as Meadows at Shawnee, Montclair, Deer Valley, Reflection Ridge, Shadow Rock, and Andover Forest. It avoided spending OpenRouter tokens on obvious direct PDF hits while still keeping bad generic names out of the bank.

For owned HOA websites with many PDFs, preflight the links before probing. The current page probe banks every linked PDF, so sites with newsletters, minutes, budgets, forms, pool documents, or rental documents can pollute the bank. For these sites, scrape the page links first, whitelist only governing-document URLs, and pass those URLs as `pre_discovered_pdf_urls` instead of letting `probe(lead)` crawl the whole page.

## Cross-State Lessons (originally captured during Kansas pass)

The named communities below are Kansas-specific examples, but the *patterns* (host families, query phrasings, false-positive shapes, model behavior) generalize to other states.

High-yield:

- Host-focused expansion after the first hit. eNeighbors was much better than continuing low-density county sweeps.
- eNeighbors public-document URLs and `/p/{community}` pages. Many direct public-document URLs bank exactly one PDF; community pages can bank multiple PDFs.
- Independent community domains found with `-eneighbors` searches after eNeighbors flattened. This found many Johnson County sites and some Sedgwick/Butler/Leavenworth sites.
- Municipal document centers that serve PDF bytes from non-`.pdf` URLs, especially `DocumentCenter/View` and archive URLs.
- County/city focused queries for finding the first productive hosts.
- Public community websites with pages named documents, governing documents, bylaws, restrictions, deed restrictions, HOA documents.
- Direct `filetype:pdf` city searches once they are cleaned before probing. Good pattern: search finds the PDF, the probe crawls the same HOA-owned host and banks the rest of the public document library.
- Host-pattern searches for public document systems, especially `gogladly.com/connect/document`, `hmsft-doc`, `/file/document/`, communitysite file URLs, and HOA WordPress uploads.
- Legal/county phrase searches over recorded documents. In Kansas, `Kansas not-for-profit corporation`, `Kansas non-profit corporation`, `Register of Deeds`, and county-specific `Declaration of Restrictions` searches were the cleanest late-stage expansion source.
- Articles/amendments/restatement phrases are lower volume but still useful after the main recorded-declaration phrases flatten.
- Deterministic management/association directories when available.
- OpenRouter validation before probing noisy candidates.

Low-yield or risky:

- Broad statewide search.
- Broad county sweeps after the main metro counties are exhausted. Several Kansas counties produced candidates but zero validated leads.
- HA-KC pages as currently probed. They create many plausible HOA manifests but mostly expose skipped/non-PDF links; use them as lead discovery unless a custom parser is added.
- Exact HOA-name search from city lists without document-page hints.
- Raw direct-PDF search without strong name evidence.
- Raw direct-PDF search without name cleanup. The search hit may contain the right PDF but the inferred HOA name can be generic or malformed.
- Generic legal-info sites and government packets.
- Querying too much with cheap models that produce bloated completions.

Observed model behavior:

- `google/gemini-3.1-pro-preview` consumed over half the OpenRouter spend in the Kansas activity export and is not used for ongoing scraping.
- `deepseek/deepseek-v4-flash` is cheap and usable for PDF triage, but can hit upstream OpenRouter 429s.
- `qwen/qwen3.5-flash` was noisy on HOA names and produced runaway hidden reasoning-token usage in the activity export; it is blocklisted for classifier calls unless explicitly overridden.
- `moonshotai/kimi-k2.6` is a reasonable fallback when DeepSeek is rate-limited.

Analyze exported OpenRouter activity before changing routing:

```bash
python benchmark/analyze_openrouter_activity.py ~/Downloads/openrouter_activity_2026-05-05.csv
```

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

## Use Cheaper Subagents Where Possible

When running this playbook in Claude Code (or any harness with subagents), default to delegating to **Sonnet** subagents (or any cheaper-than-the-orchestrator model) for the mechanical work, and reserve the top-tier orchestrator model for judgment.

Cheap, mechanical, delegate freely:

- Building per-county query files and shell scripts for a new state (mirror a prior state's `run_X_county_sweep_v2.sh` and counties file).
- Parsing public datasets — Sunbiz / Secretary-of-State bulk dumps, Census gazetteers, ZIP→county crosswalks, county-name normalization.
- Tagging existing JSONL rows with derived fields (county, slug, source family).
- Running long-running scrape / validate / probe / bank batches in the background and writing per-county summary lines to the handoff doc.
- Repairing mis-routed manifests (looking up correct county, calling `bank_hoa()`).
- Repetitive deduplication audits and post-hoc name-repair passes.

Reserve the orchestrator for:

- Deciding which source family to pursue next when multiple are plausible.
- Reading validator audits and updating the false-positive blocklist.
- Scoping when to pivot from county sweeps → host-family expansion → legal-phrase searches → owned-domain preflights.
- Calling the state stopping rule (two consecutive failed sweeps).
- Resolving cross-state-routing edge cases.
- Handling new policy / safety questions raised mid-run.

Launch independent subagents in parallel — building scripts and parsing data don't have to be sequential. The orchestrator's job is to plan and verify; the subagents do the typing.

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
