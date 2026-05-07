# Autonomous State HOA Discovery — Prompt Template

This is the reusable, harness-agnostic prompt for kicking off autonomous public HOA governing-document scraping for a new state. Works in both Codex and Claude Code.

Before pasting into a fresh context, substitute three placeholders:

- `{STATE}` — two-letter postal code, e.g. `GA`, `TN`, `FL`
- `{state-name}` — full name, e.g. `Georgia`, `Tennessee`, `Florida`
- `{METRO_LIST}` — bullet list of the highest-HOA-density metros for that state (county / largest cities). Pick 6–12. The fresh context can extend later, but seeding the right metros up front saves a research turn.

---

## Prompt

You are in `/Users/ngoshaliclarke/Documents/GitHub/hoaproxy`. Read the harness instructions for your environment (`AGENTS.md` if you are Codex, `CLAUDE.md` if you are Claude Code), then `docs/state-hoa-discovery-playbook.md`, `docs/openrouter-public-scraping.md`, and at least one prior handoff for cross-state lessons. `state_scrapers/ks/notes/discovery-handoff.md` is the most detailed; `state_scrapers/tn/notes/discovery-handoff.md` is a thinner, more recent example. Do not let any single state's specific choices constrain you — host families and query patterns generalize, named communities do not.

Other state runs may be active in parallel (in this or the other harness). Coexist gracefully on rate limits. Do not edit `hoaware/discovery/__main__.py` or files actively touched by another run unless you have a specific reason.

### Task

Autonomously scrape public {state-name} HOA governing documents into the existing GCS bank using the existing bank layout and write API. Do not create a new bucket. Use `state="{STATE}"` on leads so documents land under `gs://hoaproxy-bank/v1/{STATE}/...`.

### Constraints

- Continue autonomously; do not stop at checkpoints. Turn boundaries are not blockers — see "Autonomy Failure Mode" in the playbook. Only send a final response when there is a real blocker, the explicit budget is exhausted, or the user asks for status.
- Do not use Gemini. Blocklisted; too expensive for the yield (per the May 2026 Kansas activity export).
- Do not use Qwen Flash variants (`qwen/qwen3.5-flash`, `qwen/qwen3.6-flash`) for bulk classification. They produced runaway hidden reasoning-token usage and are blocklisted by `HOA_CLASSIFIER_BLOCKLIST` / `HOA_DISCOVERY_MODEL_BLOCKLIST`.
- Prefer deterministic search → fetch → preflight → bank over model calls. Use OpenRouter only for compact judgment tasks (county query generation, noisy lead validation, ambiguous PDF triage).
- Primary model: `deepseek/deepseek-v4-flash`. Quality fallback: `moonshotai/kimi-k2.6`, used only for a bounded set of candidates that DeepSeek rejects/cannot name/scores below the helper's quality threshold after deterministic gates. Do not retry whole failed DeepSeek batches on Kimi.
- Never send to any model: secrets, cookies, logged-in pages, resident data, private portal content, emails, payment data, or internal/work data. Models see only URL / title / snippet / compact candidate JSON.
- Respect `robots.txt` (`HOA_DISCOVERY_RESPECT_ROBOTS=1`) and practical per-host delays.
- Log all model usage to `data/model_usage.jsonl` (`HOA_MODEL_USAGE_LOG`). Do not log prompts, completions, document text, cookies, or API keys.
- Commit reusable code and docs after each milestone with a descriptive message, then keep scraping. Do not commit `benchmark/results/`, `benchmark/run_benchmark.sh`, or `benchmark/task.txt`.

### Model and subagent right-sizing

Use the strongest main-agent reasoning only for orchestration and hard judgment: choosing the next county/source-family branch, resolving novel false-positive patterns, deciding whether a new class of documents is bankable, and reviewing milestone summaries before commit/handoff.

When the harness supports subagents, you are explicitly authorized to use them autonomously to reduce main-agent cost. Delegate bounded, non-overlapping work:

- Explorer: inspect one county or one host family and return likely query/source patterns.
- Runner: execute deterministic search/dedupe/clean/probe commands for one county/source family.
- Curator: review compact public metadata after deterministic gates and propose keep/reject/name repairs.
- Verifier: check bank counts, probe output, handoff consistency, and dirty git scope.

Use low/medium reasoning for subagents unless the assigned task is genuinely ambiguous. Subagents may run deterministic scripts and prepare curated JSONL, but they must not override safety rules or send secrets, cookies, logged-in pages, resident/private data, emails, payment data, internal/work data, or full unreviewed document text to any model.

### Mandatory workflow gates

For every sweep, apply these gates before model validation or bank writes:

1. Refresh exact-source dedupe against live GCS manifests for `{STATE}`.
2. Reject signed, credentialed, private, portal, payment, resident, login, and obvious internal URLs.
3. Reject obvious non-governing document types: newsletters, minutes, budgets, forms, applications, directories, facility/pool docs, real-estate listings, court packets, and government planning packets.
4. Require governing-document evidence from the filename, URL, title/snippet, page text, or extracted PDF text.
5. Require state/county evidence, or reroute to the correct state/county when clear.
6. Use OpenRouter only on surviving compact public metadata: `name`, `source_url`, `title`, `snippet`, `filename`, deterministic category, and state/county hints.

Keep these gates lightweight. Do not build a bespoke review process for every candidate; encode recurring rejects as deterministic filters, then move on.
When a public PDF survives the hard rejects but category evidence is ambiguous,
bank it with `suggested_category=null` rather than discarding it. The prepared
worker will extract or OCR page 1 of every non-duplicate, non-PII,
non-obvious-junk candidate before making the final keep/reject decision.

### Metadata collection requirements

The live site depends on more than PDFs. Every banked HOA should include as many
of these fields as public evidence supports:

- Canonical HOA name plus aliases from source pages, PDFs, secretary-of-state records, or management portals.
- `metadata_type`: HOA, condo, coop, or timeshare when clear.
- `address.state`, `address.county`, `address.city`, and any public street or ZIP. Do not invent addresses.
- `website.url`, platform/manager hints, and whether the site is login-walled.
- Public source provenance for every metadata field.
- Geography clues: subdivision/neighborhood name, city, county, ZIPs found in governing PDFs, plat/subdivision labels, and any public GIS or map link.
- Direct governing-document source URLs and filenames.

Do not stop at state-only metadata when city/county/website clues are available.
The prepared ingest worker can resolve OSM/Nominatim polygons before Render
import, but only if discovery captured enough locality evidence to query
accurately.

For future states, geography should be resolved before live import:

1. Scrapers collect raw geography clues and bank them.
2. `scripts/prepare_bank_for_ingest.py` runs cached Nominatim/OSM polygon lookup
   for missing `geometry.boundary_geojson`.
3. If no credible polygon exists, a post-prep ZIP centroid pass can use document
   ZIPs and `/admin/backfill-locations`.
4. Render imports only prepared metadata; it should not call Nominatim or run
   geographic cleanup.

### Initial strategy

1. Count current {STATE} bank coverage:
   ```bash
   gsutil ls 'gs://hoaproxy-bank/v1/{STATE}/**/manifest.json' 2>/dev/null | wc -l
   gsutil ls 'gs://hoaproxy-bank/v1/{STATE}/*/*/doc-*/original.pdf' 2>/dev/null | wc -l
   ```
2. Search county-by-county, starting with the largest HOA-density metros:
   {METRO_LIST}
3. Use deterministic query families. Translate the state/county/city names; the host patterns are nationwide:
   - `"{state-name}" "HOA documents" bylaws`
   - `"{state-name}" "homeowners association" "declaration of covenants" filetype:pdf`
   - `"{County} County" "{state-name}" "governing documents"`
   - `"{City}" "homeowners association" "covenants" filetype:pdf`
   - `"{City}" "architectural guidelines" HOA filetype:pdf`
   - source-family searches for management portals and public document hosts that prove productive (eNeighbors, Cobalt, HOAMsoft / hmsft-doc, GoGladly, HOA Express, municipal `DocumentCenter/View`, BuilderCloud / S3, WordPress uploads, etc.)
4. Preflight candidate pages and direct PDFs. Only bank governing PDFs: declarations, CC&Rs, bylaws, articles of incorporation, amendments, rules / regulations, architectural / design guidelines, resolutions, and recorded subdivision plats.
5. Avoid newsletters, meeting minutes, budgets, forms, pool documents, directories, violation letters, real estate listings, court / government planning packets, and out-of-scope hits. Do not reject out-of-state mandatory-HOA documents when the correct state/county is clear; reroute and bank under the correct prefix. When using same-host crawl on document-rich HOA websites, preflight links and pass only whitelisted governing PDFs as `pre_discovered_pdf_urls` — see "owned-site pass" lessons in the playbook.
6. Whenever a source family proves productive, stop using the model on it and scrape that family deterministically.
7. Maintain `state_scrapers/{state-lower}/notes/discovery-handoff.md` with bank counts (before / after / running), source families attempted, query files used, false-positive patterns to block, model spend or token usage when available, and next branches. Commit it as you go so progress is recoverable across context resets.

### Autonomous loop and stop rules

Run the state as an autonomous queue:

1. Seed the top HOA-density counties from `{METRO_LIST}`.
2. For each county, run deterministic search, dedupe, clean/preflight, compact validation only if needed, bank, count, update handoff, and continue.
3. When a productive source family appears, create a source-family branch and run it with county/state routing.
4. After two successful sweeps in one source family, promote it to deterministic mode and stop using models on it except for compact name repair.
5. Commit reusable query/handoff milestones, then immediately continue.

Per-branch stop rules:

- If exact-source dedupe leaves fewer than 5 candidates from about 20 search calls, stop that branch.
- If two consecutive sweeps in one family produce fewer than 3 new manifests and fewer than 10 PDFs, deprioritize that family.
- If a county has two consecutive dry query angles, move to the next county.
- If a source family becomes mostly mirrors, record it as duplicate-heavy and pivot.

If county sweeps are dry, pivot to host-family expansion (see playbook section "Host-Focused Expansion"). If a source family stops adding new manifests, pivot to legal-phrase searches over recorded documents (`Register of Deeds`, `{state-name} not-for-profit corporation`, `Articles of Incorporation`, `Amendment to Declaration`, `Restated Bylaws`, `Supplemental Declaration`). When all of those flatten, run owned-domain whitelisted preflights against any HOA-owned websites surfaced earlier.

Stop only when source families are genuinely exhausted, the OpenRouter credit budget is exhausted, or the user explicitly asks for status.

### Final state narrative requirement

When a state reaches the live site, write a final narrative retrospective in the
state-specific artifact directory, for example
`state_scrapers/{state-lower}/results/<run_id>/{state-lower}_scrape_retrospective.md`.
This is mandatory for every new state. It must be useful to future state
scrapers, not just a status report. Include:

- What worked, what failed, and which source families should or should not be
  reused.
- Final raw-bank, prepared-ingest, live-site, document, chunk, map-coverage, and
  out-of-bounds map counts.
- A cost estimate per HOA scraped, explicitly including Serper, OpenRouter, and
  DocAI, plus the assumptions used when exact metering is unavailable.
- The main false-positive classes and the cleanup steps needed before the state
  was safe to call done.
