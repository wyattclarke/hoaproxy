# HOA Law Corpus Execution Log

Purpose: durable, append-only implementation journal for multi-session execution.

## Session: 2026-02-28

### 2026-02-28T00:00Z (approx) - Kickoff
- User directive: execute full legal corpus plan without stopping and record progress continuously.
- Chosen execution order:
  1. Phase 0 artifacts
  2. Schema + service layer
  3. Corpus scripts + seed source map
  4. API/CLI integration
  5. Validation tooling and runbook updates

### 2026-02-28T00:05Z (approx) - Repo Baseline Captured
- Confirmed existing architecture:
  - HOA doc ingestion (`hoaware/ingest.py`)
  - retrieval + QA (`hoaware/search.py`, `hoaware/qa.py`)
  - SQLite schema in `hoaware/db.py`
  - FastAPI endpoints in `api/main.py`
- Confirmed no existing jurisdictional legal corpus tables or endpoints.

### 2026-02-28T00:10Z (approx) - Phase 0 Deliverables Created
- Added `docs/legal-ontology.md`.
- Added `docs/question-contract.md`.
- Updated planning file with progress tracker:
  - `docs/hoa-law-corpus-plan.md`

## Open Work Queue
- [ ] Add legal schema tables and DB helpers
- [ ] Add law-domain service and deterministic QA assembly
- [ ] Implement source-map/fetch/normalize/extract/profile scripts
- [ ] Add law API and CLI
- [ ] Add validation checks and run pilot ingestion

### 2026-02-28T04:00Z (approx) - Schema + Service Layer Implemented
- Updated `hoaware/db.py` with legal tables:
  - `legal_sources`
  - `legal_sections`
  - `legal_rules`
  - `jurisdiction_profiles`
  - `legal_ingest_runs`
- Added legal DB helper methods for:
  - source upsert/list
  - section replace
  - rule replace by scope/topic
  - profile upsert/list/get
  - ingest-run lifecycle logging
- Added deterministic legal QA service:
  - `hoaware/law.py`

### 2026-02-28T04:02Z (approx) - Scripts Added
- Added `scripts/legal/` pipeline scripts:
  - `build_source_map.py`
  - `fetch_law_texts.py`
  - `normalize_law_texts.py`
  - `extract_rules.py`
  - `assemble_profiles.py`
  - `validate_corpus.py`
  - `run_pipeline.py`
- Added `scripts/legal/README.md`.

### 2026-02-28T04:03Z (approx) - API + CLI Integration
- Added API endpoints in `api/main.py`:
  - `GET /law/jurisdictions`
  - `GET /law/{jurisdiction}/profiles`
  - `POST /law/qa`
- Added CLI commands in `hoaware/cli.py`:
  - `law-jurisdictions`
  - `law-profiles`
  - `law-qa`
  - `law-pipeline`

### 2026-02-28T04:03Z (approx) - Pipeline Execution
- `python scripts/legal/build_source_map.py`
  - Wrote `data/legal/source_map.json`
  - row_count=900, seeded=11, pending=889
- Initial fetch in sandbox failed DNS resolution; reran with escalated network access.
- `python scripts/legal/fetch_law_texts.py --limit 50`
  - fetched=10 failed=1
  - failure: NY seed URL returned HTTP 403
- `python scripts/legal/normalize_law_texts.py --limit 50`
  - ok=10 failed=0
- `python scripts/legal/extract_rules.py --limit 50`
  - sources_processed=10
  - scope_topic_rows=12
  - rules_inserted=109
- `python scripts/legal/assemble_profiles.py`
  - profiles_upserted=6
- `python scripts/legal/validate_corpus.py`
  - report written to `legal_corpus/metadata/validation_report.md`

### 2026-02-28T04:05Z (approx) - Smoke Checks
- `python -m hoaware.cli law-jurisdictions` returned AZ/CA/FL/NC/VA coverage.
- `python -m hoaware.cli law-profiles --jurisdiction NC` returned condo+hoa profiles.
- `python -m hoaware.cli law-qa NC --community-type hoa --question-family records_and_sharing` returned deterministic answer with citation and known gaps.

### 2026-02-28T04:06Z (approx) - Full Orchestrator Smoke Run
- Ran end-to-end NC pipeline command:
  - `python scripts/legal/run_pipeline.py --state NC --limit 20`
- Result:
  - build_source_map succeeded
  - fetch succeeded for 3 NC seeded sources
  - normalize succeeded
  - extract succeeded
  - assemble_profiles succeeded
- This confirms single-command orchestration works for a state-scoped run.

### 2026-02-28T04:07Z (approx) - Latest Validation Snapshot
- Re-ran `python scripts/legal/validate_corpus.py`.
- Current counts:
  - source map rows: 900
  - seeded rows: 11
  - fetched sources: 13
  - normalized sources: 16
  - extracted rule rows (jsonl): 141
  - jurisdictions with profiles: 5
  - total profiles: 6
- Persistent blocker remains:
  - NY seed source URL returns HTTP 403 and needs alternate official source mapping.

### 2026-02-28T04:08Z (approx) - Import Sanity
- `api.main` import succeeds (`HOA QA API` app object loaded).
- `hoaware.law` import succeeds.

### 2026-02-28T04:09Z (approx) - Progress Index Added
- Added `scripts/legal/update_progress_index.py`.
- Generated `data/legal/progress_index.json` with per-state status counters (50 states).

## Updated Work Queue
- [x] Add legal schema tables and DB helpers
- [x] Add law-domain service and deterministic QA assembly
- [x] Implement source-map/fetch/normalize/extract/profile scripts
- [x] Add law API and CLI
- [x] Add validation checks and run pilot ingestion
- [ ] Expand verified source discovery from seed set to full-state authoritative coverage
- [ ] Improve extraction precision (currently heuristic and review-flagged)
- [ ] Add directed/undirected proxy-specific precision tests and golden fixtures

### 2026-02-28T04:16Z (approx) - Hardening Pass
- Hardened idempotency:
  - `scripts/legal/fetch_law_texts.py`
    - added `--refresh`
    - skips previously fetched URLs by default
    - dedupes `sources.jsonl` by `(source_url, checksum_sha256)` and rewrites cleanly
  - `scripts/legal/normalize_law_texts.py`
    - added `--force`
    - skips previously normalized snapshots by default
    - dedupes `normalized_sources.jsonl` by `snapshot_path` and rewrites cleanly
  - `scripts/legal/extract_rules.py`
    - dedupes normalized input rows by checksum/scope
    - rewrites `extracted_rules.jsonl` deterministically for processed states
- Hardened orchestration:
  - `scripts/legal/run_pipeline.py`
    - no longer rebuilds source map by default
    - added flags: `--rebuild-source-map`, `--refresh-fetch`, `--force-normalize`, `--skip-validate`
- Hardened citation fidelity:
  - `hoaware/db.py` now joins `legal_rules` to `legal_sources` for `source_type`
  - `hoaware/law.py` citations now emit real `source_type` from source metadata
- Improved extraction heuristics:
  - expanded proxy rule typing (assignment, delivery, ballot interaction, inspection)
  - expanded records typing (request form, retention, recording, richer exclusion signals)
  - reduced noisy long-sentence extraction

### 2026-02-28T04:17Z (approx) - Test Results
- Static checks:
  - `python -m compileall -q hoaware scripts/legal api tests` passed
- Unit tests:
  - `python -m unittest -v tests/test_legal_hardening.py` passed (4/4)
- Pipeline idempotency smoke:
  - `python scripts/legal/run_pipeline.py --state NC --limit 20`
  - observed `fetch`/`normalize` skip behavior (no duplicate growth on rerun)
- Validation snapshot after hardening:
  - source map rows: 900
  - seeded rows: 11
  - fetched sources: 10
  - normalized sources: 13
  - extracted rule rows (jsonl): 112
  - jurisdictions with profiles: 5

### 2026-02-28T04:29Z (approx) - Proxy Matrix + Seeded-State Upgrade
- Added required proxy-cluster matrix support:
  - `scripts/legal/proxy_matrix.py`
  - `scripts/legal/build_proxy_requirement_matrix.py`
  - generated `data/legal/proxy_requirement_matrix.json`
- Enforced proxy-cluster coverage in:
  - `scripts/legal/assemble_profiles.py` (known gaps + confidence gating)
  - `scripts/legal/validate_corpus.py` (cluster-level reporting)
- Fixed source-map limitation (single source per bucket):
  - `scripts/legal/build_source_map.py` now supports multiple seeded sources per `(state, community_type, bucket)` via `source_slot`.
- Expanded FL sources:
  - `Fla. Stat. § 720.306` (proxy-focused)
  - `Fla. Stat. § 617.0721` (nonprofit corp proxy overlay)
- Ran full seeded-state pipeline:
  - `python scripts/legal/run_pipeline.py --rebuild-source-map --rebuild-proxy-matrix --limit 500`
  - results:
    - source map rows: 902
    - seeded rows: 15
    - fetched sources: 14 (NY still 403)
    - normalized sources: 17
    - extracted rules: 185
    - profiles upserted: 6
- FL proxy extraction improved:
  - proxy rules increased to 26
  - matched proxy clusters: permission, form, assignment, direction, duration, revocation, quorum/ballot
  - remaining FL missing cluster: recording_or_inspection

### 2026-02-28T04:30Z (approx) - Tests Updated
- Expanded tests in `tests/test_legal_hardening.py`:
  - proxy cluster evaluation test
  - multi-source seed support test for source map
- Test run:
  - `python -m unittest -v tests/test_legal_hardening.py`
  - passed 6/6

### 2026-02-28T04:38Z (approx) - Two Required Electronic-Proxy Questions Integrated
- Implemented dedicated rule types for:
  - electronic proxy assignment acceptance
  - electronic proxy signature acceptance
- Added extraction support in `scripts/legal/extract_rules.py` for electronic/facsimile/remote-communication signals.
- Added law-layer answer functions in `hoaware/law.py`:
  - `answer_electronic_proxy_questions`
  - `electronic_proxy_summary`
- Added API endpoints in `api/main.py`:
  - `GET /law/{jurisdiction}/proxy-electronic`
  - `GET /law/proxy-electronic/summary`
- Added CLI commands in `hoaware/cli.py`:
  - `law-proxy-electronic`
  - `law-proxy-electronic-summary`
- Added per-state artifact generator:
  - `scripts/legal/build_electronic_proxy_summary.py`
  - output: `data/legal/electronic_proxy_summary.json`
- Updated validator to report electronic-assignment and electronic-signature coverage statuses.

### 2026-02-28T04:39Z (approx) - Re-run with New Question Coverage
- Ran:
  - `python scripts/legal/run_pipeline.py --state FL --rebuild-proxy-matrix --limit 500`
- Result highlights:
  - FL electronic assignment status: `allowed`
  - FL electronic signature status: `unclear`
  - electronic summary built for all 50 states.
- Remaining nationwide gap:
  - Most states remain `unclear` until additional authoritative source rows are discovered/fetched and extracted.

### 2026-02-28T (later) - Persistent Permission Prefixes Enabled
- Added broad persistent approvals for core legal pipeline commands:
  - `python scripts/legal/discover_state_proxy_sources.py`
  - `python scripts/legal/build_source_map.py`
  - `python scripts/legal/fetch_law_texts.py`
  - `python scripts/legal/normalize_law_texts.py`
  - `python scripts/legal/extract_rules.py`
  - `python scripts/legal/assemble_profiles.py`
  - `python scripts/legal/validate_corpus.py`
  - `python scripts/legal/update_progress_index.py`
  - `python scripts/legal/build_proxy_requirement_matrix.py`
  - `python scripts/legal/build_electronic_proxy_summary.py`
- Effect: routine discovery/build/ingest/validate runs can proceed without repeated approval prompts when invoked directly with these prefixes.

### 2026-02-28T (later) - Live-Progress Discovery Run Completed
- Reworked discovery runtime visibility:
  - enabled immediate flush on per-state progress prints in `scripts/legal/discover_state_proxy_sources.py`.
  - restarted stuck discovery process and reran with visible, state-by-state output.
- Command:
  - `python scripts/legal/discover_state_proxy_sources.py --out data/legal/discovered_seeds.json --max-pages-per-state 4 --max-depth 2 --max-per-bucket 2 --timeout 4`
- Result:
  - completed across all 50 states
  - discovered seed rows written: `39`
  - mixed coverage quality by state due source accessibility and site-structure variance (some states returned zero discovered rows and remain gaps for follow-up).

### 2026-02-28T (later) - Expanded Pipeline Run After Discovery
- Rebuilt source map with discovered seeds:
  - `python scripts/legal/build_source_map.py --discovered-seeds data/legal/discovered_seeds.json`
  - result: `source_map.json` rows=1091, seeded=54, pending=1037.
- Ran full pipeline:
  - `python scripts/legal/run_pipeline.py --rebuild-proxy-matrix --limit 800`
- Key outcomes:
  - fetch: fetched=35 failed=3 skipped_existing=16
  - normalize: ok=35 failed=0 skipped_existing=14
  - extract: sources_processed=49, rules_inserted=234
  - profiles_upserted=17
  - validation:
    - jurisdictions with profiles: 16
    - total profiles: 17
    - FL electronic assignment=`allowed`, FL electronic signature=`unclear`
    - assignment status counts: allowed=1, unclear=16
    - signature status counts: unclear=17
  - artifacts refreshed:
    - `legal_corpus/metadata/validation_report.md`
    - `data/legal/progress_index.json`
    - `data/legal/electronic_proxy_summary.json`
- Observed fetch blockers:
  - `GA` discovered Justia links returned 403
  - `NY` seeded BSC source returned 403

### 2026-02-28T (later) - Deterministic State Source Registry (10-state seed) Implemented
- Added curated deterministic source registry:
  - `data/legal/state_source_registry.json`
  - seeded 10 priority states: `AZ, CA, FL, NC, TX, VA, DE, OR, WA, NY`
  - includes HOA/condo/coop-relevant source rows across proxy/nonprofit/electronic overlays where available.
- Updated source-map builder to consume registry first:
  - `scripts/legal/build_source_map.py`
  - new flag: `--registry-seeds`
  - dedupes overlapping seed URLs across registry + legacy pilot seeds
  - preserves source metadata from curated rows (`verification_status`, `notes`, etc.)
- Added regression test:
  - `tests/test_legal_hardening.py::test_source_map_loads_registry_seed_rows`

#### Validation after registry-driven pipeline
- Command:
  - `python scripts/legal/run_pipeline.py --rebuild-source-map --rebuild-proxy-matrix --limit 1200`
- Coverage deltas:
  - seeded rows: `54 -> 89`
  - fetched sources: `49 -> 73`
  - normalized sources: `52 -> 76`
  - extracted rules: `234 -> 1042`
  - profiles upserted: `17 -> 27`
- Electronic proxy status counts:
  - assignment: `allowed=4`, `required_to_accept=3`, `unclear=20`
  - signature: `allowed=1`, `unclear=26`

#### Remaining blockers observed
- NY Senate law pages still return `403` for BSC/STT section URLs in automated fetch.
- NC General Assembly intermittently returns `503` for some section URLs.
- Several discovered legacy seeds remain low-quality/non-authoritative and should be pruned as curated per-state rows expand.

### 2026-02-28T (later) - UETA Overlay Inference Upgrade for Electronic Proxy Questions
- Hardened extraction in `scripts/legal/extract_rules.py` so electronic-transactions overlay text can infer proxy e-assignment/e-signature policies when statutes use generic UETA language (e.g., legal-effect and writing/signature equivalence clauses).
- Added tests in `tests/test_legal_hardening.py`:
  - `test_overlay_electronic_signature_inference`
  - `test_overlay_electronic_record_inference`
- Re-ran pipeline:
  - `python scripts/legal/run_pipeline.py --rebuild-proxy-matrix --limit 1200`
- Impact:
  - extracted rule rows: `1042 -> 1307`
  - electronic assignment counts: `allowed=1, required_to_accept=3, unclear=20` -> `allowed=1, required_to_accept=13, unclear=13`
  - electronic signature counts: `allowed=1, unclear=26` -> `required_to_accept=12, unclear=15`
- FL HOA electronic answer now resolves both dimensions as `required_to_accept` with evidence from `Fla. Stat. § 668.50` plus `§ 617.0721`.

### 2026-02-28T06:23Z - NH Electronic Source URL Corrected + Full Pipeline Re-run
- Updated NH electronic overlay seed in `data/legal/state_source_registry.json`:
  - from: `https://gc.nh.gov/rsa/html/XXIII/294-E/294-E-7.htm` (404 shell)
  - to: `https://www.gencourt.state.nh.us/rsa/html/xxvii/294-e/294-e-7.htm` (live statute page)
- Verified NH fetch/normalize now includes real `294-E:7` text.
- Ran:
  - `python scripts/legal/run_pipeline.py --rebuild-source-map --rebuild-proxy-matrix --limit 1500`
- Result highlights:
  - fetched sources: `98`
  - normalized sources: `101`
  - extracted rules: `1908`
  - profiles: `28`
  - electronic status counts: `assignment allowed=2, required_to_accept=25, unclear=1`; `signature allowed=2, required_to_accept=25, unclear=1`
  - only remaining unclear profile after this run: `IA hoa unknown`.

### 2026-02-28T06:24Z - PDF Normalization Hardening (Systematic)
- Hardened `scripts/legal/normalize_law_texts.py` for PDF legal text extraction:
  - added `pdftotext` fallback path (`_pdf_to_text_pdftotext`) when needed.
  - added PDF text-quality helpers and selection logic.
  - fixed section splitting edge case where footer markers (e.g., `Section 554D.107 (17, 0)`) could cause body truncation by forcing full-text section when first marker appears only near end of file.
- Added regression tests in `tests/test_legal_hardening.py`:
  - `test_pdf_quality_heuristic_flags_short_extracts`
  - `test_split_sections_ignores_footer_only_markers`
- Test run:
  - `.venv/bin/python -m unittest tests/test_legal_hardening.py`
  - passed `18/18`.

### 2026-02-28T06:25Z - IA Electronic Overlay Citation/URL Corrected + Targeted Rebuild
- Corrected IA electronic overlay seed to the legal-recognition section:
  - citation: `Iowa Code § 554D.108`
  - URL: `https://www.legis.iowa.gov/docs/code/554D.108.pdf`
- Rebuilt/targeted pipeline steps:
  - `python scripts/legal/build_source_map.py`
  - `python scripts/legal/fetch_law_texts.py --state IA --limit 50`
  - `python scripts/legal/normalize_law_texts.py --state IA --force`
  - `python scripts/legal/extract_rules.py --state IA --limit 200`
  - `python scripts/legal/assemble_profiles.py --state IA`
  - `python scripts/legal/validate_corpus.py`
  - `python scripts/legal/update_progress_index.py`
  - `python scripts/legal/build_electronic_proxy_summary.py`
- Final impact:
  - `IA hoa unknown` moved from `unclear/unclear` to `required_to_accept/required_to_accept` for electronic assignment/signature.
  - electronic status counts now:
    - assignment: `allowed=2, required_to_accept=26`
    - signature: `allowed=2, required_to_accept=26`
  - no remaining `unclear` profile statuses for the two electronic proxy questions.

### 2026-02-28T06:35Z - Current-State Assessment + Coverage Plan Refresh
- Assessment snapshot:
  - `legal_corpus/metadata/validation_report.md`:
    - source map rows: `1090`
    - seeded rows: `103`
    - fetched sources: `99`
    - normalized sources: `102`
    - extracted rule rows: `1916`
    - profiles: `28` across `16` jurisdictions
    - in assembled profile set, electronic assignment/signature statuses are non-unclear.
  - `data/legal/electronic_proxy_summary.json` still shows many `unclear` states because it spans all 50 jurisdictions, including those without active profile coverage.
  - `data/legal/progress_index.json` shows:
    - seeded jurisdictions: `26`
    - fetched jurisdictions: `24`
    - extracted jurisdictions: `16`
- Blocker concentration from `legal_corpus/metadata/fetch_errors.jsonl`:
  - NY (`403` on `www.nysenate.gov`)
  - NC (`503` intermittently on `www.ncleg.gov`)
  - GA discovered rows (`403` on Justia URLs)
- Source-quality observation:
  - fetched corpus still contains `secondary_aggregator` rows (Lexis/Westlaw placeholders).
- Planning output:
  - Added a concrete Phase 6 completion plan to `docs/hoa-law-corpus-plan.md` covering:
    - source-quality gate + aggregator exclusion
    - blocked-host fallback chains
    - deterministic 50-state seed expansion
    - extraction depth improvements for records/proxy families
    - release gates + machine-readable coverage health report
    - continuous progress visibility requirements.

### 2026-02-28T15:29Z - Phase 6 Hardening Implemented (Quality Gate + Fallbacks + Health Gates)
- Implemented shared source-quality classifier:
  - added `scripts/legal/source_quality.py` with categories:
    - `official_primary`
    - `official_secondary`
    - `aggregator`
    - `unknown`
  - added extraction-eligibility policy helper:
    - default include official
    - default exclude aggregator unless explicitly enabled.
- Integrated quality metadata into source map:
  - `scripts/legal/build_source_map.py` now sets `source_quality` for seeded/discovered rows.
- Hardened fetch pipeline:
  - `scripts/legal/fetch_law_texts.py`:
    - new `--include-aggregators` flag (default off)
    - source-quality gating at fetch time
    - candidate URL fallback chains (row-provided and derived)
    - retry behavior for transient HTTP statuses
    - host-specific fallback derivation for NY and NC statute URL patterns
    - writes `source_quality`, `fetched_url`, and attempt logs into metadata.
- Hardened normalize/extract pipeline:
  - `scripts/legal/normalize_law_texts.py` now carries `source_quality` forward.
  - `scripts/legal/extract_rules.py`:
    - new `--include-aggregators` flag (default off)
    - quality gating before rule extraction.
- Added release-gate + machine-readable health reporting:
  - `scripts/legal/validate_corpus.py` now emits:
    - source quality summary in markdown report
    - release gates:
      - electronic non-unclear
      - minimum topic coverage
      - no aggregator-only profiles
    - `data/legal/coverage_health.json` with gate pass/fail + blockers.
- Updated orchestrator and progress artifacts:
  - `scripts/legal/run_pipeline.py` now passes aggregator toggles through fetch/extract.
  - `scripts/legal/update_progress_index.py` now includes per-state `fetched_source_quality_counts`.
- Added tests:
  - `tests/test_legal_hardening.py` now covers:
    - source quality classification/gating
    - NC and NY fallback URL derivation
  - test suite status: `22/22` passing.

#### Full pipeline run after hardening
- Command:
  - `python scripts/legal/run_pipeline.py --rebuild-source-map --rebuild-proxy-matrix --limit 1500`
- Key outcomes:
  - fetched sources: `105` (up from 99)
  - normalized sources: `108`
  - extracted rows: `1923`
  - profiles: `29`
  - fetch skips due quality gate: aggregator sources for AR/CO/GA/PA
  - NY rows fetched successfully via fallback host chain (`legislation.nysenate.gov`).
- Validation gates:
  - electronic non-unclear: PASS
  - minimum topic coverage: PASS
  - no aggregator-only profiles: PASS
- Source quality (fetched corpus):
  - `official_primary=101`
  - `aggregator=4`

#### Remaining known gaps after hardening
- National 50-state electronic summary remains partially unclear because profile coverage is still concentrated:
  - in `data/legal/electronic_proxy_summary.json`:
    - assignment: `required_to_accept=14`, `allowed=1`, `unclear=35`
    - signature: `required_to_accept=14`, `allowed=1`, `unclear=35`
- Registry breadth remains limited (currently curated for subset of states), so deterministic seeded coverage is not yet nationwide complete.

### 2026-02-28T15:32Z - Additional Hardening Pass (NY Shell Detection + National Gate)
- Improved NY fetch robustness:
  - `scripts/legal/fetch_law_texts.py` now rejects low-value HTML shell responses (`Open Legislation` app bootstrap pages) and continues fallback candidates.
  - NY fallback ordering now prefers API endpoint candidates before HTML shell routes.
  - Added optional NY API key support for API endpoints via `NYSENATE_API_KEY` (header and query param support).
- Added stricter validation signaling:
  - `scripts/legal/run_pipeline.py` now builds electronic summary before validation so gates evaluate current state.
  - `scripts/legal/validate_corpus.py` now includes:
    - `gate_national_electronic_coverage` based on 50-state summary clarity.
- Latest gate result:
  - `gate_national_electronic_coverage: FAIL (unclear_states=35)` (expected until more states are seeded/fetched/extracted).
- Artifact refresh:
  - `data/legal/electronic_proxy_summary.json`
  - `legal_corpus/metadata/validation_report.md`
  - `data/legal/coverage_health.json`
  - `data/legal/progress_index.json`

## Session: 2026-03-01

### 2026-03-01T02:00Z (approx) - Discovery Breadth Expansion to 50 States
- Ran deep discovery:
  - `python scripts/legal/discover_state_proxy_sources.py --out data/legal/discovered_seeds.json --max-pages-per-state 20 --max-depth 4 --max-per-bucket 6 --timeout 8`
- Result:
  - `data/legal/discovered_seeds.json` increased to `236` rows, then to `407` rows after fallback logic hardening.
  - Coverage reached all `50` states in discovered seed inventory.

### 2026-03-01T02:08Z (approx) - Discovery Fallback + Seed Selection Hardening
- Updated `scripts/legal/discover_state_proxy_sources.py`:
  - Added deterministic bucket fallback rows when ranking returns no candidates.
  - Added bucket-aware fallback URL scoring and optional official-host preference.
  - Added/expanded state seed overrides (including AK/AL/CO/CT/GA/IN/KY/LA/MA/MD/MN/MO/MS/NJ/NM/OK/SD/TN/UT/WI/WY).
  - Changed seed URL assembly to prioritize overrides first and raised cap from 8 to 12 seed URLs.
- Added tests:
  - `test_discovery_fallback_seed_url_prefers_bucket_hints`
  - `test_discovery_fallback_seed_url_prefers_official_host`

### 2026-03-01T02:12Z (approx) - Source Map Acceptance Filter Hardening
- Updated `scripts/legal/build_source_map.py` discovered-seed acceptance:
  - Expanded statute token heuristics for non-path URL formats (`cencode`, `citeid`, `infobase=statutes`, etc.).
  - Allowed official fallback rows with strong fallback/legislative context even when URL path is nonstandard.
- Added tests:
  - `test_merge_discovered_accepts_official_fallback_url`
  - `test_merge_discovered_rejects_aggregator_fallback_without_statute_signals`

### 2026-03-01T02:14Z (approx) - Source Quality Host Hardening
- Updated `scripts/legal/source_quality.py` official allowlist:
  - added `billstatus.ls.state.ms.us`
  - added `index.ls.state.ms.us`
- Added test:
  - `test_source_quality_classifies_ms_billstatus_official`

### 2026-03-01T02:16Z (approx) - Fetch Transport Fallback Hardening
- Updated `scripts/legal/fetch_law_texts.py`:
  - Added host-level derived fallbacks for legacy hosts:
    - NJ `lis.njleg...` -> `https://www.njleg.state.nj.us/statutes`
    - SD legacy host -> `https://sdlegislature.gov/Statutes`
    - AK legacy host -> `https://www.akleg.gov/basis/statutes.asp`
    - AL legacy host -> `https://alison.legislature.state.al.us/`
  - Added HTTPS->HTTP fallback derivation for known cert-problem hosts.
  - Added constrained `verify=False` retry for known official hosts with broken certificate chains.
- Added tests:
  - `test_ct_ssl_host_derives_http_fallback`
  - `test_nj_legacy_host_derives_modern_statutes_fallback`

### 2026-03-01T02:18Z (approx) - Full Pipeline Rebuild Run
- Command:
  - `python scripts/legal/run_pipeline.py --rebuild-source-map --rebuild-proxy-matrix --limit 5000`
- Key outcomes:
  - source map rows: `1420`
  - seeded rows: `433`
  - fetched sources: `433`
  - normalized sources: `436`
  - extracted rules: `3875`
  - jurisdiction profiles: `50` profiles across `27` jurisdictions
- Gate outcome:
  - `gate_electronic_non_unclear`: FAIL (`unclear_profiles=14`)
  - `gate_min_topic_coverage`: FAIL (`failures=9`)
  - `gate_no_aggregator_only_profiles`: PASS
  - `gate_national_electronic_coverage`: FAIL (`unclear_states=28`)

### 2026-03-01T02:24Z (approx) - Targeted Blocker-State Re-runs
- Ran targeted state pipelines:
  - `AK AL CO CT MS NJ SD`
- Observed persistent blocker pattern:
  - DNS/host reachability failures for `AK`, `AL`, `CT`, `NJ`, `SD`
  - cert/DNS instability for `MS`
  - aggregator-only sources still dominating `GA`, `OK`, `PA` fallback options
- Current progress index snapshot:
  - seeded states: `50`
  - fetched states: `42`
  - normalized states: `42`
  - extracted states: `27`
  - profile states: `27`
- States with seeded>0 but fetched=0:
  - `AK, AL, CT, GA, MS, NJ, OK, SD`

### 2026-03-01T02:25Z (approx) - Test Status
- `python -m unittest -v tests/test_legal_hardening.py`
  - now `29/29` passing.
