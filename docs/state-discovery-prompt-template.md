# Autonomous State HOA Discovery — Prompt Template

This is the reusable, harness-agnostic prompt for kicking off autonomous public HOA governing-document scraping for a new state. Works in both Codex and Claude Code.

Before pasting into a fresh context, substitute three placeholders:

- `{STATE}` — two-letter postal code, e.g. `GA`, `TN`, `FL`
- `{state-name}` — full name, e.g. `Georgia`, `Tennessee`, `Florida`
- `{METRO_LIST}` — bullet list of the highest-HOA-density metros for that state (county / largest cities). Pick 6–12. The fresh context can extend later, but seeding the right metros up front saves a research turn.

---

## Prompt

You are in `/Users/ngoshaliclarke/Documents/GitHub/hoaproxy`. Read the harness instructions for your environment (`AGENTS.md` if you are Codex, `CLAUDE.md` if you are Claude Code), then `docs/state-hoa-discovery-playbook.md`, `docs/openrouter-public-scraping.md`, and at least one prior handoff for cross-state lessons. `docs/ks-discovery-handoff.md` is the most detailed; `docs/tn-discovery-handoff.md` is a thinner, more recent example. Do not let any single state's specific choices constrain you — host families and query patterns generalize, named communities do not.

Other state runs may be active in parallel (in this or the other harness). Coexist gracefully on rate limits. Do not edit `hoaware/discovery/__main__.py` or files actively touched by another run unless you have a specific reason.

### Task

Autonomously scrape public {state-name} HOA governing documents into the existing GCS bank using the existing bank layout and write API. Do not create a new bucket. Use `state="{STATE}"` on leads so documents land under `gs://hoaproxy-bank/v1/{STATE}/...`.

### Constraints

- Continue autonomously; do not stop at checkpoints. Turn boundaries are not blockers — see "Autonomy Failure Mode" in the playbook. Only send a final response when there is a real blocker, the explicit budget is exhausted, or the user asks for status.
- Do not use Gemini. Blocklisted; too expensive for the yield (per the May 2026 Kansas activity export).
- Do not use Qwen Flash variants (`qwen/qwen3.5-flash`, `qwen/qwen3.6-flash`) for bulk classification. They produced runaway hidden reasoning-token usage and are blocklisted by `HOA_CLASSIFIER_BLOCKLIST` / `HOA_DISCOVERY_MODEL_BLOCKLIST`.
- Prefer deterministic search → fetch → preflight → bank over model calls. Use OpenRouter only for compact judgment tasks (county query generation, noisy lead validation, ambiguous PDF triage).
- Primary model: `deepseek/deepseek-v4-flash`. Fallback when DeepSeek is rate-limited or returns malformed JSON: `moonshotai/kimi-k2.6`.
- Never send to any model: secrets, cookies, logged-in pages, resident data, private portal content, emails, payment data, or internal/work data. Models see only URL / title / snippet / compact candidate JSON.
- Respect `robots.txt` (`HOA_DISCOVERY_RESPECT_ROBOTS=1`) and practical per-host delays.
- Log all model usage to `data/model_usage.jsonl` (`HOA_MODEL_USAGE_LOG`). Do not log prompts, completions, document text, cookies, or API keys.
- Commit reusable code and docs after each milestone with a descriptive message, then keep scraping. Do not commit `benchmark/results/`, `benchmark/run_benchmark.sh`, or `benchmark/task.txt`.
- **Use cheaper subagents (Sonnet) wherever possible to conserve quota.** Reserve the orchestrator (Opus) for judgment-heavy steps: deciding which source family to chase next, reading new false-positive patterns out of validator audits, scoping the next pivot. Delegate the mechanical work to Sonnet via the Agent tool — building per-county query files, mirroring shell scripts to a new state, parsing public datasets (Sunbiz, gazetteers, ZIP→county maps), tagging rows, running long scrape/probe/validate batches in the background, and writing per-county summary updates to the handoff doc. Launch independent Sonnet agents in parallel when possible. The orchestrator's job is to plan and verify; the subagents do the typing.

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
4. Preflight candidate pages and direct PDFs. Only bank governing PDFs: declarations, CC&Rs, bylaws, articles of incorporation, amendments, rules / regulations, architectural / design guidelines, resolutions.
5. Avoid newsletters, meeting minutes, budgets, forms, pool documents, directories, violation letters, real estate listings, court / government planning packets, and out-of-state hits. When using same-host crawl on document-rich HOA websites, preflight links and pass only whitelisted governing PDFs as `pre_discovered_pdf_urls` — see "owned-site pass" lessons in the playbook.
6. Whenever a source family proves productive, stop using the model on it and scrape that family deterministically.
7. Maintain `docs/{state-lower}-discovery-handoff.md` with bank counts (before / after / running), source families attempted, query files used, false-positive patterns to block, model spend or token usage when available, and next branches. Commit it as you go so progress is recoverable across context resets.

### Pivot rule

If county sweeps are dry, pivot to host-family expansion (see playbook section "Host-Focused Expansion"). If a source family stops adding new manifests, pivot to legal-phrase searches over recorded documents (`Register of Deeds`, `{state-name} not-for-profit corporation`, `Articles of Incorporation`, `Amendment to Declaration`, `Restated Bylaws`, `Supplemental Declaration`). When all of those flatten, run owned-domain whitelisted preflights against any HOA-owned websites surfaced earlier.

Stop only when source families are genuinely exhausted, the OpenRouter credit budget is exhausted, or the user explicitly asks for status.
