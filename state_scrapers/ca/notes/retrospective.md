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
