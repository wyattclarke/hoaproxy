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

## Pre-existing CA bank pollution (discovered 2026-05-09 session 1)
Pre-Driver-A scan of `gs://hoaproxy-bank/v1/CA/` found 27 county-slug
subdirectories, **24 of which are mis-routed** (FL/GA county slugs:
charlotte, clayton, dougherty, duval, escambia, floyd, forsyth, franklin,
glynn, gordon, hall, heard, hillsborough, lee, liberty, etc.). Only
`el-dorado/`, `lake/`, and `unresolved-name/` are genuine CA. These
mis-routed manifests are from prior cross-state writes (likely the
legacy CA pipeline running with `state=CA, county=<wrong-state-county>`
or a faulty cross-state route).

**Phase 3 cleanup scope**: adapt `fl_repair_misrouted_manifests.py` to
detect and re-route the 24 foreign-county slugs under `v1/CA/` to their
correct destination state prefix (most are GA, some FL/SC). This is
**identical to the FL→GA case** documented in giant-state §3d.

## Slug-pollution observations (live during Driver A smoke test)
Driver A's probe step occasionally banks manifests under garbled names
when the lead's "name" field came from an OCR fragment rather than the
search query. Examples from el-dorado smoke test:

- `gs://.../v1/CA/unresolved-name/ration-of-sierra-springs-name-of-corporation-is-sierra-springs/`
  — fragment of `DECLARATION OF SIERRA SPRINGS OWNERS ASSOCIATION`
- `SECOND RESTATED GOLD RIDGE FOREST PROPERTY ... GOLD RIDGE FOREST PROPERTY OWNERS ASSOCIATION`
  — fragment of `SECOND RESTATED [DECLARATION OF] GOLD RIDGE FOREST...`

The bank correctly quarantines these under `unresolved-name/`. The
Phase 5 OCR slug-repair pass (conservative regex: "DECLARATION OF
COVENANTS, CONDITIONS AND RESTRICTIONS for [NAME]" pattern) plus the
Phase 10 LLM rename pass will canonicalize these. Per CLAUDE.md memory:
"bank by SHA at intake; derive slug/county/address from OCR text in
prepare, not from search-snippet hints."


