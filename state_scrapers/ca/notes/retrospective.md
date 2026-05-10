# California HOA Ingestion — Retrospective

**Status: IN PROGRESS** (multi-day run started 2026-05-09).

Will be filled in once Phase 10 close completes. Tracked sections:

## Registry source + as-of date
- Primary: `data/california_hoa_entities.csv` (CA SoS bulk corp dump, pre-filtered)
- Secondary: legacy `data/california/{hoa_index, hoa_details}.jsonl` (Mar 2026)
- Tertiary attempted: SI-CID list (CA SoS Common Interest Development under §5405)
- Driver D: DRE Subdivision Public Reports

## Final counts
- `bank_entity_count`: TBD
- `bank_with_docs_count`: TBD
- `live_entity_count`: TBD

## Geometry stack tier-by-tier match rates
- E4 city/ZIP-centroid: TBD
- E3 OSM Nominatim: TBD
- E2 HERE Geocoder: TBD
- E1 county Subd_poly: TBD
- E0 county-tract polygon: TBD

## State-mismatch reroute counts
- TBD (expected low — CA is net destination of cross-state OCR mismatches)

## OCR slug-repair count + samples
- TBD

## Total spend per cost line
- Serper: TBD
- DocAI: TBD
- HERE: TBD (free tier expected)
- OpenRouter: TBD

## What didn't I try
- TBD (expected: Driver E county recorder; tertiary mgmt-co harvest;
  subdivision public-report exhibit-PDF extraction)

## Audit-driven adjustments (2026-05-09)
- **Concurrent quality audit deleted ~50% of legacy live CA HOAs** for junk
  content (per `state_scrapers/_orchestrator/quality_audit_2026_05_09/FINAL_REPORT.md`
  + `state_scrapers/ca/results/audit_2026_05_09/ca_sample.json`, 46/80 = 57.5%
  junk rate in sample). Junk patterns are diverse — government docs, tax
  forms, zoning resolutions, maps, newsletters, court filings, wrong-HOA
  CC&Rs, generic legal handbooks, omnibus dumps.
- **Root cause**: legacy CA pipeline (Google-Scrape) was not name-anchored;
  banked any PDF that mentioned the HOA name, even in unrelated city
  zoning hearings.
- **Pipeline mitigation**:
  - Driver A is name-anchored at the query level, far cleaner than legacy.
  - **Phase 5c LLM content-grading gate** added (mandatory before drain).
    Adapts `scripts/audit/grade_hoa_text_quality.py` to run against bank
    manifests using OCR sidecar text. Expected spend: ~$5 / ~25k entities.
  - Junk-graded manifests stay banked but never drain to live.
- **Bank vs live separation worked correctly**: my Driver A sweep populates
  `gs://hoaproxy-bank/v1/CA/...`, isolated from the live deletion in
  progress. No collision risk.

