# State Kickoff Prompt — Template

Agent-agnostic prompt template used to start an autonomous state-scraping
session in either Claude Code or Codex. Substitute every `{placeholder}` listed
below before pasting the **Prompt Body** block into a fresh session.

## How to use

1. Open Appendix D of `docs/multi-state-ingestion-playbook.md` and find the
   row for your assigned state.
2. Fill in the **Required substitutions** table below using values from
   Appendix D.
3. Copy everything between the `---PROMPT START---` and `---PROMPT END---`
   markers below into a fresh agent session. Both Claude Code and Codex
   accept the same body.
4. The session will read the playbook itself; the prompt only carries the
   per-state parameters and the autonomy contract.

## Required substitutions

| Placeholder | What to put | Example (VT) |
|---|---|---|
| `{STATE}` | 2-letter USPS code, uppercase | `VT` |
| `{state-name}` | Full state name | `Vermont` |
| `{state-lower}` | 2-letter USPS code, lowercase | `vt` |
| `{TIER}` | Tier number 0–4 (Appendix D) | `0` |
| `{CAI_BOUND}` | CAI estimate string from Appendix D | `<1,250` |
| `{MAX_DOCAI_USD}` | Per-tier DocAI cap (see table below) | `5` |
| `{DISCOVERY_PRIMARY}` | Primary discovery source from Appendix D, with brief amplification | `SoS-first (Vermont Secretary of State business registry)` |
| `{DISCOVERY_FALLBACK_NOTE}` | Brief fallback note | `with the keyword-Serper pattern as fallback if SoS proves inadequate` |
| `{PARALLEL_STATES}` | Comma-separated list of other parallel runs, or `[none]` | `NH, ME, WY` |
| `{COUNTY_GUIDANCE}` | One paragraph: county list + density hints (Appendix D notes) | `Vermont's 14 counties (Addison, Bennington, ...). Highest HOA density: Chittenden (Burlington), Rutland, Washington (Montpelier/Stowe), Bennington, Lamoille (resort condos).` |
| `{NAME_PATTERN_NOTES}` | State-specific HOA name patterns, or empty string | `` (empty) or `Many WY HOAs are organized as "ranch" or "club" associations rather than condominium associations; do not exclude those name patterns.` |
| `{GENERIC_REJECT_TOKENS}` | Generic tokens to reject, slash-separated | `condominium / association / Vermont` |
| `{STATE_SPECIFIC_NOTES}` | Optional paragraph for unique quirks; leave empty if none | `` (empty) or `Watch for the "Maine" / "main" homophone in unrelated documents.` |

## Per-tier cost defaults

| Tier | CAI band | `--max-docai-cost-usd` | Wall time |
|---|---|---|---|
| 0 | < 1,500 | 5 | 4–12 h |
| 1 | 1,500 – 4,000 | 10 | 1–2 days |
| 2 | 4,000 – 10,000 | 30 | 3–5 days, phased |
| 3 | 10,000 – 25,000 | 75 | multi-week, operator-supervised |
| 4 | > 25,000 | custom | own state-specific plan |

## Run-id format

`{state-lower}_{YYYYMMDD_HHMMSS}_{agent}` where `{agent}` is `claude` or
`codex`. Example: `vt_20260507_180000_claude`. Used for retrospective
attribution and cross-batch performance comparison.

---

## Prompt Body

`---PROMPT START---`

```
You are in /Users/ngoshaliclarke/Documents/GitHub/hoaproxy. Read CLAUDE.md (or AGENTS.md for Codex), then docs/multi-state-ingestion-playbook.md, docs/agent-ingestion.md, and at least one prior handoff. state_scrapers/ks/notes/discovery-handoff.md is the most detailed; state_scrapers/ri/notes/retrospective.md is a strong reference for SoS-first methodology. Do not let any single state's specific choices constrain you.

Other state runs ({PARALLEL_STATES}) are active in parallel right now. Coexist gracefully on rate limits. Do not edit shared files actively touched by another run unless you have a specific reason. git pull --rebase before every git push so concurrent sessions don't collide.

### Task

Autonomously scrape public {state-name} HOA governing documents into the existing GCS bank. Use state="{STATE}" on leads so documents land under gs://hoaproxy-bank/v1/{STATE}/.... Do not create a new bucket.

### Tier and budget

{state-name} is Tier {TIER} (CAI {CAI_BOUND}) per Appendix D of the playbook. Recommended discovery: {DISCOVERY_PRIMARY}, {DISCOVERY_FALLBACK_NOTE}. Run-id format: {state-lower}_{YYYYMMDD_HHMMSS}_{claude|codex} so retros and ledgers are attributable.

Per-tier cost ceiling for this run: --max-docai-cost-usd {MAX_DOCAI_USD} on prepare_bank_for_ingest.py. OpenRouter spend cap: $5. Serper spend cap: $3. If any cap is hit before completion, write a partial retrospective at state_scrapers/{state-lower}/notes/retrospective.md, commit, and stop.

### Constraints

- Continue autonomously. Turn boundaries are not blockers — see "Autonomy Failure Mode" in the playbook. Only send a final response when there is a real blocker, the explicit budget is exhausted, or the user asks for status.
- Do not use Gemini. Do not use Qwen Flash variants for bulk classification. Both are blocklisted.
- Prefer deterministic search → fetch → preflight → bank over model calls.
- Primary classifier model: deepseek/deepseek-v4-flash. Quality fallback: moonshotai/kimi-k2.6 for the bounded subset of candidates DeepSeek rejects/cannot name/scores below threshold after deterministic gates. Do not retry whole failed DeepSeek batches on Kimi.
- Never send to any model: secrets, cookies, logged-in pages, resident data, private portal content, emails, payment data, or internal/work data.
- Respect robots.txt and practical per-host delays.
- Log all model usage to data/model_usage.jsonl. Do not log prompts, completions, document text, cookies, or API keys.
- Commit reusable code and docs after each milestone. git pull --rebase before push.

### Sub-agent right-sizing

Delegate to cheaper subagents (Explorer / Runner / Curator / Verifier roles as defined in Phase 0) for mechanical work. Reserve the orchestrator for judgment: choosing next source-family branch, reading validator audits, calling the two-sweep stop rule, cross-state routing edge cases, safety/policy questions.

### Initial strategy

1. Set up state_scrapers/{state-lower}/ by copying state_scrapers/_template/ (see its README.md). Replace placeholders. Use the runner template's --discovery-mode that matches your primary source family.

2. Count current {STATE} bank coverage:
   gsutil ls 'gs://hoaproxy-bank/v1/{STATE}/**/manifest.json' 2>/dev/null | wc -l
   gsutil ls 'gs://hoaproxy-bank/v1/{STATE}/*/*/doc-*/original.pdf' 2>/dev/null | wc -l

3. Phase 1 preflight: confirm the primary discovery source is open and queryable. If it requires payment / blocked / closed, immediately fall back to the secondary pattern. {COUNTY_GUIDANCE}

4. Universe pass: scrape the primary source for HOA-shaped name patterns (condominium, homeowners, owners, civic, townhouse, estates, village, condominium trust, property owners). {NAME_PATTERN_NOTES} Apply post-filter to drop generic single-keyword hits. Filter by mailing-address state == {STATE} (keep an --include-out-of-state flag for management-co audit, but live HOAs must be in-state).

5. Per-entity enrichment: for each lead, run "<exact entity name>" {state-name} filetype:pdf and "<exact entity name>" {state-name} declaration OR bylaws OR covenants. Score on specific (non-generic) name-token overlap. Reject hits whose only overlap is generic ({GENERIC_REJECT_TOKENS}). SoS corporate-filing PDFs hosted on the {state-name} SoS document drive are first-class governing documents — accept articles of incorporation and bylaws-as-exhibits.

   {STATE_SPECIFIC_NOTES}

6. Maintain state_scrapers/{state-lower}/notes/discovery-handoff.md with running bank counts, source families attempted, query files used, false-positive patterns to block, model spend, and next branches. Commit as you go.

### Stop rules

See Phase 2 "Per-branch stop thresholds" and "Per-state two-sweep stop rule." Stop when source families are genuinely exhausted, any cost cap above is hit, or the user explicitly asks for status.

### Required artifacts on completion

- state_scrapers/{state-lower}/results/{run_id}/final_state_report.json
- state_scrapers/{state-lower}/notes/retrospective.md (see Phase 10 requirements; mandatory)
- A commit and push of the retrospective + final_state_report

Begin now.
```

`---PROMPT END---`

---

## Worked example: VT (Tier 0, SoS-first)

Substitution table:

| Placeholder | Value |
|---|---|
| `{STATE}` | `VT` |
| `{state-name}` | `Vermont` |
| `{state-lower}` | `vt` |
| `{TIER}` | `0` |
| `{CAI_BOUND}` | `<1,250` |
| `{MAX_DOCAI_USD}` | `5` |
| `{DISCOVERY_PRIMARY}` | `SoS-first (Vermont Secretary of State business registry)` |
| `{DISCOVERY_FALLBACK_NOTE}` | `with the keyword-Serper pattern as fallback if SoS proves inadequate` |
| `{PARALLEL_STATES}` | `NH, ME, WY` |
| `{COUNTY_GUIDANCE}` | `Vermont's 14 counties (Addison, Bennington, Caledonia, Chittenden, Essex, Franklin, Grand Isle, Lamoille, Orange, Orleans, Rutland, Washington, Windham, Windsor). Highest HOA density: Chittenden (Burlington), Rutland, Washington (Montpelier/Stowe), Bennington, Lamoille (resort condos).` |
| `{NAME_PATTERN_NOTES}` | (empty) |
| `{GENERIC_REJECT_TOKENS}` | `condominium / association / Vermont` |
| `{STATE_SPECIFIC_NOTES}` | (empty) |

## Notes for parallel batches

- Stagger session starts by ~3 minutes so preflights don't thunder Render's `/admin/costs` and Serper rate limits simultaneously.
- Mix `claude` and `codex` agents in the same batch (e.g. 2 of each across 4 states). Anthropic and OpenAI rate-limit buckets are independent.
- The cumulative DocAI ceiling across parallel sessions is the GCP `hoaware` project monthly cap (~$600, auto-shutoff via the `stop-billing` Cloud Function). 4 sessions × $5 × 30 days = $600 — plan accordingly. The Render-side `/upload` daily cap (`DAILY_DOCAI_BUDGET_USD=20`) does not apply to this pipeline because OCR runs in `prepare_bank_for_ingest.py` against GCP directly.
- Each session does its own `git pull --rebase` before push. Concurrent commits will not destructively collide but the operator should expect a non-linear merge graph.

## See also

- `docs/multi-state-ingestion-playbook.md` — canonical playbook (Phase 0 through Phase 10)
- `docs/multi-state-ingestion-playbook.md` Appendix D — per-state launch packet (CAI counts, tiers, recommended discovery sources)
- `state_scrapers/_template/README.md` — runner template usage
- `state_scrapers/ri/notes/retrospective.md` — Tier 1 SoS-first retrospective exemplar
- `state_scrapers/ga/notes/retrospective.md` — Tier 3 keyword-Serper retrospective exemplar
- `state_scrapers/tn/notes/retrospective.md` — Tier 2 keyword-Serper retrospective exemplar
