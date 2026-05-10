# AZ HOA registry seed — blocker notes

**Status:** blocked. No `az_registry_seed.jsonl` produced.

## What was tried (2026-05-09)

1. **AZ Corporation Commission eCorp** (`ecorp.azcc.gov/EntitySearch/Index`):
   public search interface only — no bulk download endpoint, no published
   API. Most AZ condo / planned-community associations are organized as
   nonprofit corps under ACC jurisdiction, but they're not separable from
   the rest of the ~tens-of-thousands of AZ nonprofits without a name-based
   filter, and we can only pull names one-by-one through the search UI.

2. **AZ Corporation Commission Data Requests**: paid bulk products available
   only by emailing/calling 602-542-3026.

3. **Maricopa County Recorder document search**
   (`recorder.maricopa.gov/recording/document-search.html`):
   single-document search; CC&Rs and HOA filings are searchable by HOA name
   in a "Business Name" field, but no listing/dump.

4. **Maricopa County GIS Open Data Portal**
   (`data-maricopa.opendata.arcgis.com/`):
   has parcels and plats; subdivisions layer exists but does not associate
   plats with HOA legal-entity names directly.

5. **Pima County Library / Pima County Recorder**: list-and-look-up only.

## What is already covered

Nothing in `state_scrapers/az/` yet — empty directory.

## Next steps if this gets revisited

- Pay for an AZCC bulk corp dump (call 602-542-3026 for pricing).
- Build a per-county Serper sweep similar to the NC pattern.
- Harvest the third-party `arizona-homeowners-associations.com` directory by
  county (free but is a scrape of a private site — check robots and TOS).
- Use the Maricopa Plat Index's subdivision table joined to ACC nonprofit
  search by subdivision name.
