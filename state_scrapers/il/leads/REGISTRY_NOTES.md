# IL HOA registry seed — blocker notes

**Status:** blocked. No `il_registry_seed.jsonl` produced.

## What was tried (2026-05-09)

1. **Illinois SoS Business Entity Search** (`apps.ilsos.gov/businessentitysearch/`):
   the SoS explicitly prohibits bulk downloads. From their site:
   > This database may not be used to copy or download bulk searches or
   > information.
   Bulk data is offered for sale by phone (217-782-6961); no public CSV/API.

2. **Cook County Recorder of Deeds (now Cook County Clerk)**:
   condominium declarations are NOT available online for bulk download.
   "Plats, condominium declarations, and real estate transfer declarations are
   not available for sale through the search portal."

3. **Cook County Assessor open data** (`datacatalog.cookcountyil.gov`):
   has a `Residential Condominium Unit Characteristics` dataset — but that's
   *unit-level* PIN data (one row per condo unit, not per association). Could
   in principle be aggregated by 10-digit MAJOR pin to get one row per condo
   building, but the building name is not in that dataset, so we'd produce
   nameless rows.

## What is already covered

`state_scrapers/il/leads/il_chicagoland_*.jsonl` — Cook + collar counties,
seeded via assessor parcel + Serper places search. Counts in the 1000–2000
range, far below the ~19,750 universe estimate.

## Next steps if this gets revisited

- Pay for a one-time IL SoS bulk dump (cost unknown — call 217-782-6961).
- Or do a per-county Serper sweep statewide (slow, ~$50 in API cost).
- Or join Cook County assessor condo PINs against IDOT plats by MAJOR.
