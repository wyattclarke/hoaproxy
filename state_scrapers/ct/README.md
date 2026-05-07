# Connecticut Scraper

Per the multi-state ingestion playbook (`docs/multi-state-ingestion-playbook.md`).

## Why CT is a good autonomous-run target

- 169 towns/cities across 8 traditional counties — small enough to cover end-to-end
  in one pass.
- CT abolished county government in 1960; land evidence (deeds, condominium
  declarations, planned-community covenants) is recorded at the **municipal**
  level by each town clerk. Bank under
  `gs://hoaproxy-bank/v1/CT/{county}/{slug}/` even though counties are
  statistical only — keeps the layout consistent with the rest of the corpus.
- CT condominium law follows the **Common Interest Ownership Act**
  (Title 47 Ch. 828), a UCIOA derivative — same legal family as RI's Title 34
  Ch. 36/36.1, so the existing classifier and category taxonomy work without
  changes.
- CT publishes its Secretary-of-State business registry as **public Open Data**
  (`data.ct.gov` dataset id `n7gp-d28j`). That replaces HTML scraping with
  structured SODA queries — far more reliable than the new Salesforce-based
  service.ct.gov portal that took over from the old CONCORD ASP.NET search.

## Counties → municipalities (169 towns)

| County | # | Municipalities |
|---|---|---|
| Fairfield | 23 | Bethel, Bridgeport, Brookfield, Danbury, Darien, Easton, Fairfield, Greenwich, Monroe, New Canaan, New Fairfield, Newtown, Norwalk, Redding, Ridgefield, Shelton, Sherman, Stamford, Stratford, Trumbull, Weston, Westport, Wilton |
| Hartford | 29 | Avon, Berlin, Bloomfield, Bristol, Burlington, Canton, East Granby, East Hartford, East Windsor, Enfield, Farmington, Glastonbury, Granby, Hartford, Hartland, Manchester, Marlborough, New Britain, Newington, Plainville, Rocky Hill, Simsbury, South Windsor, Southington, Suffield, West Hartford, Wethersfield, Windsor, Windsor Locks |
| Litchfield | 26 | Barkhamsted, Bethlehem, Bridgewater, Canaan, Colebrook, Cornwall, Goshen, Harwinton, Kent, Litchfield, Morris, New Hartford, New Milford, Norfolk, North Canaan, Plymouth, Roxbury, Salisbury, Sharon, Thomaston, Torrington, Warren, Washington, Watertown, Winchester, Woodbury |
| Middlesex | 15 | Chester, Clinton, Cromwell, Deep River, Durham, East Haddam, East Hampton, Essex, Haddam, Killingworth, Middlefield, Middletown, Old Saybrook, Portland, Westbrook |
| New Haven | 27 | Ansonia, Beacon Falls, Bethany, Branford, Cheshire, Derby, East Haven, Guilford, Hamden, Madison, Meriden, Middlebury, Milford, Naugatuck, New Haven, North Branford, North Haven, Orange, Oxford, Prospect, Seymour, Southbury, Wallingford, Waterbury, West Haven, Wolcott, Woodbridge |
| New London | 21 | Bozrah, Colchester, East Lyme, Franklin, Griswold, Groton, Lebanon, Ledyard, Lisbon, Lyme, Montville, New London, North Stonington, Norwich, Old Lyme, Preston, Salem, Sprague, Stonington, Voluntown, Waterford |
| Tolland | 13 | Andover, Bolton, Columbia, Coventry, Ellington, Hebron, Mansfield, Somers, Stafford, Tolland, Union, Vernon, Willington |
| Windham | 15 | Ashford, Brooklyn, Canterbury, Chaplin, Eastford, Hampton, Killingly, Plainfield, Pomfret, Putnam, Scotland, Sterling, Thompson, Windham, Woodstock |

CT also has a long list of postal villages that aren't incorporated municipalities
(e.g. **Mystic** → Stonington, **Pawcatuck** → Stonington, **Storrs** → Mansfield,
**Willimantic** → Windham, **Cos Cob/Old Greenwich/Riverside** → Greenwich,
**Sandy Hook** → Newtown, **Niantic** → East Lyme, **Uncasville** → Montville,
**Jewett City** → Griswold, **Collinsville** → Canton, **Tariffville** → Simsbury,
**Unionville** → Farmington, **Forestville** → Bristol). The
`CITY_COUNTY` map in `scripts/scrape_ct_sos.py` already covers the common ones.
Audit the bank for `_unknown-county/` slugs after discovery and backfill any
holes.

## Discovery anchor: SODA, not HTML

The previous CONCORD ASP.NET search at `concord-sots.ct.gov/CONCORD/` now 302s
to a Salesforce Lightning portal at `service.ct.gov/business/s/onlinebusinesssearch`,
which is JavaScript-rendered behind an Aura RPC. Rather than reverse-engineer
that, we query the same registry via Open Data:

- Dataset: `n7gp-d28j` — *Connecticut Business Registry - Business Master*
- Endpoint: `https://data.ct.gov/resource/n7gp-d28j.json`
- ~1.27M rows total; ~5K active HOA-shaped names across the 21 patterns we
  search for.
- Structured fields: `name`, `status`, `business_type`, `accountnumber`,
  `naics_code`, `billingstreet`, `billingcity`, `billingstate`,
  `billingpostalcode`, `mailing_address`.
- Anonymous queries are throttled but adequate for this volume; an optional
  `--app-token` raises the limit if needed.

## Pipeline

Same shape as RI — see `docs/multi-state-ingestion-playbook.md`.

```bash
.venv/bin/python state_scrapers/ct/scripts/run_state_ingestion.py \
  --max-docai-cost-usd 15 \
  --apply
```

The runner orchestrates:

1. `scrape_ct_sos.py` — pull active HOA-shaped entities from the SODA dataset
   into `leads/ct_sos_associations.jsonl`.
2. `enrich_ct_leads_with_serper.py` — per-name Serper search for governing
   PDFs into `leads/ct_sos_associations_enriched.jsonl`.
3. `probe_enriched_leads.py` — bank into `gs://hoaproxy-bank/v1/CT/...`.
4. `scripts/prepare_bank_for_ingest.py --state CT` — page-one OCR review,
   sidecar creation, geography enrichment, prepared bundle writes.
5. `POST /admin/ingest-ready-gcs?state=CT&limit=50` loop until empty.
6. `enrich_ct_locations.py --apply` — ZIP-centroid backfill via
   `api.zippopotam.us` (per playbook §6, since public Nominatim
   rate-limits hard above ~100 sequential lookups).

## What to watch for that wasn't an issue in RI

1. **Fairfield County out-of-state management.** Many CT condos cluster in
   Greenwich/Stamford/Norwalk and are managed by NYC-based firms. Expect more
   SoS mailing addresses with NY/NJ ZIPs. The `--include-out-of-state` flag in
   `scrape_ct_sos.py` and the OOS-demote pass in `enrich_ct_locations.py`
   handle this — but worth sample-auditing the SoS leads for management-co
   addresses that mask the actual HOA location.
2. **Yacht/golf/civic clubs that aren't HOAs.** CT has many of these. The
   `NON_HOA_TOKENS_RE` filter in `scrape_ct_sos.py` rejects them
   (`yacht`, `country club`, `golf club`, `chamber of commerce`, etc.), but
   tighten the regex if false positives leak into the bank.
3. **Serper budget.** ~5K entities × 2 queries ≈ 10K Serper calls (~$3 spend
   at the standard tier) — confirm the SERPER plan supports it before kicking
   off the enrichment pass.
4. **Bristol/Coventry/Washington name overlap.** "Bristol" exists in both CT
   (Hartford) and RI (Bristol). "Coventry" is in both (CT Tolland vs RI Kent).
   The exact-name Serper match keyed off `"<entity>" Connecticut` should
   keep them separate, but spot-check the audit if anomalies surface.
