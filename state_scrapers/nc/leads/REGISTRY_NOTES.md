# NC HOA registry seed — blocker notes

**Status:** blocked. No `nc_registry_seed.jsonl` produced.

## What was tried (2026-05-09)

1. **NC Secretary of State Business Registration Search**
   (`sosnc.gov/online_services/search/by_title/_Business_Registration`):
   returns 403 to programmatic fetches. Search UI exists but does not expose
   a bulk export.

2. **NC SoS Data Subscriptions** (`sosnc.gov/online_services/data_subscriptions`):
   bulk corporate data is **paid only** — currently only UCC/Notary and
   Business Registration subscription contracts are offered. No public CSV.
   Pricing requires emailing the office.

3. **NC Planned Community Act registration**:
   NC has no centralized public HOA/POA registration database. Per Chapter 47F,
   communities are created by recording a declaration in the county land
   records. There is no state-level public list to scrape.

4. **OpenCorporates**: covers NC SoS but bulk access is paid (£2,250/yr API
   tier) and TOS forbids scraping.

## What is already covered

`reference_nc_hoa_sources.md` (user memory) lists productive aggregator
domains we already scrape via Serper:
- closingcarolina.com
- casnc.com
- seasideobx.com
- triadhoa.com
- wilsonpm.com
- Wake County GIS (city + neighborhood polygons)
- Mecklenburg County GIS (city + neighborhood polygons)

## Next steps if this gets revisited

- Pay for the NC SoS Business Registration data subscription (one-time pull).
- Or harvest county GIS subdivision/neighborhood polygon attribute tables for
  Wake / Mecklenburg / Guilford / Forsyth / Durham / New Hanover / Buncombe /
  Cumberland — these often contain HOA/subdivision name fields.
