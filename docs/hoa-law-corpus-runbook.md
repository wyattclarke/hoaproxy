# HOA Law Corpus Runbook

Last updated: 2026-02-28

## Resume Checklist
1. Confirm working tree and latest logs:
   - `docs/hoa-law-corpus-execution-log.md`
   - `legal_corpus/metadata/validation_report.md`
2. Rebuild source map only if needed:
   - `python scripts/legal/build_source_map.py`
3. Rebuild proxy requirement matrix if needed:
   - `python scripts/legal/build_proxy_requirement_matrix.py`
4. Fetch legal sources (network required):
   - `python scripts/legal/fetch_law_texts.py --limit 200`
   - Add `--refresh` only when you intentionally want to re-fetch known URLs.
5. Normalize:
   - `python scripts/legal/normalize_law_texts.py --limit 200`
   - Add `--force` only when you intentionally want to re-normalize known snapshots.
6. Extract:
   - `python scripts/legal/extract_rules.py --limit 200`
7. Assemble profiles:
   - `python scripts/legal/assemble_profiles.py`
8. Validate:
   - `python scripts/legal/validate_corpus.py`
9. Refresh state progress index:
   - `python scripts/legal/update_progress_index.py`
10. Build per-state answers for electronic proxy questions:
   - `python scripts/legal/build_electronic_proxy_summary.py --community-type hoa`

## State-focused execution
- NC only:
  - `python scripts/legal/run_pipeline.py --state NC`
- NC with forced refresh:
  - `python scripts/legal/run_pipeline.py --state NC --refresh-fetch --force-normalize`
- All seeded states with rebuilt matrices:
  - `python scripts/legal/run_pipeline.py --rebuild-source-map --rebuild-proxy-matrix --limit 500`

## Current Known Blockers
- Transport/dns issues on seeded official hosts for:
  - `AK`, `AL`, `CT`, `MS`, `NJ`, `SD`
- Aggregator-only source dependence still present for:
  - `GA`, `OK`, `PA`
- National electronic-coverage gate remains failed due unresolved jurisdictions.

## Current Coverage Snapshot
- Seeded source rows: 433
- Seeded states: 50
- Fetched source rows: 433
- Fetched states: 42
- Normalized source rows: 436
- Extracted rule rows: 3875
- Jurisdictions with profiles: 27 (50 total profiles)

## Quality Notes
- Extracted rules are auto-generated heuristics and still require legal QA.
- Production legal claims should only use rows whose source text has been verified.
- Proxy completeness is now gated by required coverage clusters (permission, form, assignment, direction, duration, revocation, quorum/ballot, recording/inspection).
