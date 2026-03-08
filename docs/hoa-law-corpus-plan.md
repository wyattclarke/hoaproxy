# HOA/Condo/Co-op State Law Corpus Plan

Last updated: 2026-02-28
Owner: HOAware project
Status: Planning in progress

## Goal
Build a reliable, jurisdiction-based legal corpus so HOAware can consistently answer:

1. What association documents/records are available to homeowners, and what legal controls limit sharing/use of those records?
2. What are the rules for proxy voting (validity, assignment, directed vs. undirected proxies, recording/retention, revocation, scope, and meeting use)?

This must cover:
- HOA planned communities
- Condominium associations
- Co-op boards
- General entity law that may govern these organizations (nonprofit corporation acts, business corporation acts, property codes, election/meeting statutes).

## Repo Familiarization Snapshot (2026-02-28)
- Current system is a document ingestion + retrieval stack using SQLite + Qdrant + OpenAI embeddings.
- Primary inputs today are HOA community PDFs under `casnc_hoa_docs/` and uploaded HOA files.
- Existing schema tracks HOAs, documents, chunks, and HOA location metadata (`hoaware/db.py`).
- Existing app copy already references legal basis examples in `api/static/about.html`, but there is no structured 50-state law corpus or normalized rule extraction pipeline yet.
- Current local DB sample shows limited jurisdiction coverage (`NC` plus blanks), so jurisdictional legal intelligence is not yet first-class.

## Architecture Plan

## Phase 0: Define legal ontology and answer contract
Create a strict schema for legal outputs before data collection.

Deliverables:
- `docs/legal-ontology.md` defining canonical fields:
  - `jurisdiction` (state, district, territory if included)
  - `community_type` (`hoa`, `condo`, `coop`)
  - `entity_form` (`nonprofit_corp`, `for_profit_corp`, `unincorporated`, `unknown`)
  - `topic_family` (`records_access`, `records_sharing_limits`, `proxy_voting`)
  - `rule_type` (inspection right, delivery deadline, redactable category, proxy validity term, directed proxy requirement, etc.)
  - `source_type` (statute, regulation, case, AG opinion, SOS/agency guidance)
  - `citation`, `effective_date`, `last_verified_date`, `excerpt`, `plain_english_summary`
  - `confidence` (`high`, `medium`, `low`) and `needs_human_review` flag
- `docs/question-contract.md` with strict response templates for the two question families, including mandatory citation payload.

Why first:
- Prevents unstructured summary drift and makes later QA deterministic.

## Phase 1: Build source map by jurisdiction and organization type
For each state, enumerate authoritative legal sources in priority order.

Source priority:
1. Official state statutes (primary law)
2. Official regulations/admin code (if applicable)
3. State court rules or published case law (for interpretation edges)
4. AG opinions / agency guidance (secondary)
5. Reputable legal summaries only as gap-fillers

Coverage map per state:
- Planned community / HOA act (if any)
- Condominium act
- Cooperative housing statutes (often in coop corporation, real property, or landlord-tenant sections)
- Nonprofit corporation records + member meeting/proxy provisions
- Business corporation proxy provisions if co-ops/associations are organized there
- Election/meeting formalities that affect proxy handling or records retention

Deliverable:
- `data/legal/source_map.json` with one row per `(state, community_type, governing_law_bucket, source_url)`.

## Phase 2: Acquire and version legal text corpus
Implement deterministic fetch + snapshot so laws can be re-verified after updates.

Data layout:
- `legal_corpus/raw/<state>/<source_id>/...` (downloaded statute pages/PDFs)
- `legal_corpus/normalized/<state>/<source_id>.md` (cleaned text with section boundaries)
- `legal_corpus/metadata/sources.jsonl` (checksum, fetched_at, effective dates, parser version)

Requirements:
- Keep URL + retrieval timestamp + checksum for each source artifact.
- No destructive overwrite; append new snapshots when text changes.
- Track parser errors for manual review.

Implementation notes:
- Add scripts in `scripts/legal/`:
  - `build_source_map.py`
  - `fetch_law_texts.py`
  - `normalize_law_texts.py`
- Keep parsing modular per publisher pattern (state legislature sites vary heavily).

## Phase 3: Extract normalized rules for the two target question families
Convert text into structured rule rows with citations.

Tables to add (SQLite):
- `legal_sources`
  - source metadata, jurisdiction, checksum, fetched/verified timestamps
- `legal_sections`
  - normalized section text + citation anchors
- `legal_rules`
  - extracted atomic rules tied to `legal_sections`
- `jurisdiction_profiles`
  - denormalized, query-ready synthesis per `(state, community_type, entity_form)`

Key extraction targets:
- Records/documents access:
  - who may inspect/copy
  - required records categories
  - response deadlines and formats
  - fee/cost limits
  - withholding/redaction categories
  - membership list and reuse limits
  - downstream sharing constraints (privacy, litigation, commercial use, etc.)
- Proxy voting:
  - proxy validity period/expiration
  - assignability and form requirements
  - directed vs. undirected proxy rules
  - revocation/substitution rules
  - quorum/election use constraints
  - board duties for recording/retaining proxies
  - notice and ballot/proxy interaction rules

## Phase 4: Jurisdiction reasoning layer and deterministic answer generation
Add a legal query service that resolves applicable law stack in order:

1. Jurisdiction (state)
2. Community type (`hoa` / `condo` / `coop`)
3. Entity form overlay (nonprofit/business corp)
4. Conflict resolution policy (specific statute over general; newer effective date wins unless otherwise required)

Output contract:
- Answer body in plain language
- Structured rule checklist
- Mandatory citations to statute sections
- Confidence + explicit unknowns
- `last_verified_date` shown to user

## Phase 5: Validation and legal QA workflow
Create repeatable quality checks:
- Per-state completeness checklist for both question families
- Contradiction checks between `legal_rules` rows
- Citation integrity checks (section text still matches stored checksum)
- Human review queue for low-confidence or parser-failure jurisdictions

Target acceptance gate before “all-state” release:
- 100% states have at least one validated profile for each applicable community type
- 0 unresolved parser errors in top-priority states
- Spot-check memo for at least 10 states across diverse statutory structures

## Incremental Implementation Plan (Engineering Sprints)
1. Sprint A: Ontology + schema + source map scaffolding
2. Sprint B: Raw fetch + normalization pipeline for pilot states
3. Sprint C: Rule extraction for records-access and proxy topics
4. Sprint D: Jurisdiction profile assembler + API endpoint
5. Sprint E: QA harness + update/refresh cadence automation

## Progress Tracker
- Phase 0 (ontology + answer contract): Completed
- Phase 1 (source mapping): Completed (baseline 50-state map + pilot seeds)
- Phase 2 (corpus acquisition/normalization): In progress (pilot-state fetch/normalize running)
- Phase 3 (rule extraction): In progress (heuristic extractor active; human review required)
- Phase 4 (reasoning + answer service): Completed (initial deterministic service + API/CLI)
- Phase 5 (validation + refresh): In progress (validation report implemented)
- Phase 6A (source-quality gate): Completed
- Phase 6B (blocked-host fallback strategy): In progress
- Phase 6C (deterministic 50-state seed expansion): In progress
- Phase 6D (extraction depth expansion): In progress
- Phase 6E (release gates + machine health report): Completed
- Phase 6F (continuous run visibility): In progress

Pilot states recommended first:
- NC (existing repo context)
- CA, FL, TX, VA, AZ (already referenced in `about.html`)
- NY (important for co-op prevalence)

## Integration Plan for Current Codebase
- Keep existing HOA document ingestion unchanged for community docs.
- Add parallel “law corpus” ingestion path and tables under `hoaware/db.py`.
- Add API endpoints (proposed):
  - `GET /law/jurisdictions`
  - `GET /law/{state}/profiles`
  - `POST /law/qa` with inputs: `state`, `community_type`, `entity_form`, `question_family`
- Reuse chunking + embedding approach for semantic fallback, but base answers on structured `legal_rules` first.
- Keep legal corpus physically separate from HOA community uploads to avoid contamination of sources.

## Risks and Controls
- Risk: Statutory text format variance by state.
  - Control: parser adapters + source checksum + manual exception queue.
- Risk: Mixed applicability (HOA vs condo vs coop vs corp overlays).
  - Control: explicit applicability matrix in `jurisdiction_profiles`.
- Risk: Stale legal answers.
  - Control: stored `last_verified_date`, scheduled refetch checks, checksum diff alerts.
- Risk: Overclaiming legal certainty.
  - Control: confidence labels, unknown flags, explicit citations, human-review workflow.

## Coverage Completion Plan (2026-02-28)
Objective:
- Move from strong pilot depth to broad, reliable 50-state coverage for both target question families, with priority on the two electronic proxy questions.

Current baseline:
- Validation profile set has no `unclear` statuses for electronic assignment/signature in assembled profiles.
- National summary still has many `unclear` states because most jurisdictions are not yet seeded/fetched/extracted.
- Most repeat fetch failures are concentrated in NY (`403`), NC (`503` intermittently), and GA (discovered `403` hosts).

### Phase 6A: Source Quality Gate and Registry Cleanup
Deliverables:
- Add a source-quality classifier in ingestion metadata:
  - `official_primary`
  - `official_secondary`
  - `aggregator`
  - `unknown`
- Enforce extraction allowlist by default:
  - include `official_primary` and `official_secondary`
  - exclude `aggregator` unless explicitly enabled.
- Replace current aggregator rows (Lexis/Westlaw placeholders) with official state sources where possible.

Acceptance criteria:
- `secondary_aggregator` source count in fetched corpus reduced to zero for active extraction runs.
- Validation report includes a source-quality summary section.

### Phase 6B: Blocked-Host Fallback Strategy
Deliverables:
- Add per-state fallback chains in `state_source_registry.json` for blocked/unreliable hosts:
  - primary URL
  - mirror/alternate official URL
  - section PDF fallback
- Implement retry/backoff class handling in fetch:
  - `403`: host-specific alternate URL strategy
  - `503`: timed retry then fallback URL
- Add explicit host adapter notes for:
  - NY
  - NC
  - GA

Acceptance criteria:
- NY seeded electronic/proxy/records overlays fetch successfully from an official host.
- NC `55A-7-24` equivalent proxy provisions fetched via alternate URL or PDF fallback.
- GA no longer depends on discovered Justia-only URLs for seeded records.

### Phase 6C: Deterministic 50-State Seed Expansion
Deliverables:
- Expand `state_source_registry.json` to full baseline per state/community type:
  - HOA:
    - community act (or closest equivalent)
    - nonprofit/business overlay
    - electronic transactions overlay
    - proxy voting
    - records access
    - records sharing limits
  - Condo:
    - condo act
    - nonprofit/business overlay if applicable
    - electronic transactions overlay
    - proxy voting + records access
  - Coop:
    - cooperative/business corp overlay
    - electronic transactions overlay
    - proxy + records access where available
- Keep discovered seeds only as a candidate queue, not default extraction inputs.

Acceptance criteria:
- `seeded_sources > 0` for all 50 jurisdictions.
- `fetched_snapshots > 0` for at least 45 jurisdictions in routine runs.
- `extracted_rule_rows > 0` for at least 40 jurisdictions.

### Phase 6D: Rule Extraction Depth for the Two Primary Question Families
Deliverables:
- Expand extraction patterns for:
  - records sharing limits:
    - member-list commercial-use bans
    - PII redaction constraints
    - litigation/privilege withholding
    - abuse/harassment privacy exclusions
  - proxy voting:
    - directed/undirected distinctions
    - assignment/delegation language
    - revocation mechanics
    - validity/duration
    - delivery/declaration mechanics
    - record retention/inspection of proxies
    - ballot/proxy interaction restrictions
- Add unit tests for each proxy cluster and records sharing cluster.

Acceptance criteria:
- For top 20 population states, each HOA profile has:
  - non-empty records access extraction
  - non-empty proxy extraction
  - at least 6 of 10 proxy clusters matched or explicitly marked not-applicable.

### Phase 6E: Validation, Reporting, and Release Gates
Deliverables:
- Strengthen `validate_corpus.py` with explicit release gates:
  - gate 1: electronic assignment/signature non-unclear in assembled profiles
  - gate 2: minimum extraction counts by topic family
  - gate 3: source-quality floor (no aggregator-only profiles)
- Emit machine-readable health report:
  - `data/legal/coverage_health.json`
  - includes per-state blockers and next remediation action.

Acceptance criteria:
- Automated run produces pass/fail summary for all release gates.
- Health report highlights only actionable blocker states.

### Phase 6F: Continuous Operation and Visibility
Deliverables:
- Add periodic progress snapshots in execution log with:
  - states improved
  - blockers remaining
  - coverage deltas
- Add lightweight heartbeat output for long discovery/fetch jobs.

Acceptance criteria:
- No long-running job without visible progress for more than 60 seconds.
- Execution log stays current with each major pipeline run.

### Immediate 7-Day Work Queue
1. Remediate transport blockers for `AK, AL, CT, MS, NJ, SD` (DNS/cert/legacy host issues).
2. Add deterministic official seeds for `GA, OK, PA` to replace aggregator dependence.
3. Increase extracted coverage from 27 to 40+ states by adding statutory sources (records + proxy buckets).
4. Reduce profile-level electronic `unclear` statuses (currently 14) via targeted source additions.
5. Re-run full pipeline and refresh validation/health/progress artifacts after each blocker batch.

## Progress Log
### 2026-02-28
- Completed repo scan focused on ingestion, DB schema, API, and current legal references.
- Confirmed there is no dedicated jurisdictional legal corpus pipeline yet.
- Identified reusable components:
  - document chunking/embedding/search stack (`hoaware/*.py`)
  - SQLite persistence patterns in `hoaware/db.py`
  - existing legal framing in `api/static/about.html`
- Created this implementation plan with phased architecture, schema direction, and sprint roadmap.
- Next actionable step: implement Phase 0 deliverables (`legal-ontology.md` and `question-contract.md`) and create empty schema migration for legal tables.

### 2026-02-28 (Execution update)
- Implemented Phase 0 artifacts:
  - `docs/legal-ontology.md`
  - `docs/question-contract.md`
- Implemented legal corpus schema/data layer in `hoaware/db.py`:
  - `legal_sources`, `legal_sections`, `legal_rules`, `jurisdiction_profiles`, `legal_ingest_runs`
  - upsert/list/query helpers for legal scopes, rules, and profiles
- Implemented deterministic law service in `hoaware/law.py`.
- Added legal API endpoints in `api/main.py`:
  - `GET /law/jurisdictions`
  - `GET /law/{jurisdiction}/profiles`
  - `POST /law/qa`
- Added legal CLI commands in `hoaware/cli.py`:
  - `law-jurisdictions`, `law-profiles`, `law-qa`, `law-pipeline`
- Implemented full script pipeline under `scripts/legal/`:
  - `build_source_map.py`, `fetch_law_texts.py`, `normalize_law_texts.py`, `extract_rules.py`, `assemble_profiles.py`, `validate_corpus.py`, `run_pipeline.py`
- Executed pipeline run:
  - source map generated: 900 rows (11 seeded + 889 pending placeholders)
  - fetched: 10/11 seeded sources (NY source returned HTTP 403)
  - normalized: 10
  - extracted rules inserted: 109
  - profiles assembled: 6
- Validation report generated at `legal_corpus/metadata/validation_report.md`.

### 2026-02-28 (Hardening + test update)
- Hardened pipeline idempotency for fetch/normalize/extract metadata.
- Added pipeline control flags (`--rebuild-source-map`, `--refresh-fetch`, `--force-normalize`, `--skip-validate`).
- Improved legal extraction heuristics for proxy and records categories.
- Fixed citation metadata fidelity so `source_type` comes from `legal_sources` rather than a hardcoded value.
- Added regression tests in `tests/test_legal_hardening.py`; all tests passed.
- Re-ran NC pipeline to verify rerun-safe behavior; fetch/normalize now skip existing by default.

### 2026-02-28 (Proxy-matrix enforcement + seeded-state expansion)
- Added required proxy coverage matrix:
  - `scripts/legal/proxy_matrix.py`
  - `scripts/legal/build_proxy_requirement_matrix.py`
  - generated `data/legal/proxy_requirement_matrix.json`
- Enforced required proxy clusters in profile assembly and validation.
- Fixed source-map seed model to support multiple citations per `(state, community_type, bucket)` using `source_slot`.
- Expanded seeded sources for Florida proxy analysis:
  - `Fla. Stat. § 720.306`
  - `Fla. Stat. § 617.0721`
- Re-ran pipeline across all seeded states:
  - source map rows: 902
  - seeded rows: 15
  - fetched sources: 14
  - normalized sources: 17
  - extracted rule rows: 185
- FL proxy extraction improved substantially (26 proxy rows), with one remaining missing proxy cluster (`recording_or_inspection`) currently flagged.

### 2026-02-28 (Electronic assignment/signature question integration)
- Added first-class support for two cross-state questions:
  - whether electronic proxy assignment is allowed/required/restricted
  - whether electronic signatures for proxy assignment are allowed/required/restricted
- Added API, CLI, and generated summary artifact:
  - `GET /law/{jurisdiction}/proxy-electronic`
  - `GET /law/proxy-electronic/summary`
  - `data/legal/electronic_proxy_summary.json` (50-state status rows)
- Updated validation to include these two status dimensions.

### 2026-03-01 (Coverage breadth + transport hardening)
- Discovery breadth expanded to all 50 states with deterministic fallback rows.
- Source map now has seeded coverage for all 50 states:
  - `seeded_states=50`
  - `seeded_rows=433`
- Fetch/normalize/extract full rebuild completed:
  - fetched sources: `433`
  - normalized sources: `436`
  - extracted rules: `3875`
  - profiles: `50` across `27` jurisdictions
- Current release gate status:
  - electronic non-unclear: `FAIL` (`unclear_profiles=14`)
  - min topic coverage: `FAIL` (`failures=9`)
  - no aggregator-only profiles: `PASS`
  - national electronic coverage: `FAIL` (`unclear_states=28`)
- Current progress-index state counts:
  - seeded states: `50`
  - fetched states: `42`
  - extracted states: `27`
  - profile states: `27`
- Current no-fetch states despite seeded sources:
  - `AK, AL, CT, GA, MS, NJ, OK, SD`
