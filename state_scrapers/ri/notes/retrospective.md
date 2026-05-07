# Rhode Island HOA Scrape — Retrospective

A frank account of what worked, what didn't, and what to do differently the
next time you scrape a small Northeast state where land records sit at the
municipal level and condo management firms wall their document libraries.

> **Scope note.** This is a retrospective written for *the next person who
> tries a small state*, not a marketing summary. It deliberately documents
> dead ends. If a section says "this didn't work," that's load-bearing
> information — don't repeat it.

## TL;DR

- **Outcome:** 198 RI HOAs live on hoaproxy.org with 300 documents, 6,145
  search chunks, 99.5% map coverage. Total marginal spend **~$2.55**
  ($1.29 DocAI + ~$1.05 Serper + ~$0.20 OpenRouter classifier + ~$0.005
  embeddings). That is **~$0.013 per imported HOA, ~$0.046 per
  substantive (≥10-chunk) HOA, ~$0.0035 per entity attempted**.
- **Coverage of RI's *registered* condo/HOA universe:** ~27%
  (198 of 721 active SoS entities) by HOA count, but only ~28% of the 198
  imported (55 HOAs) have substantive content (≥10 chunks). The other 143
  imported are thin profiles with 1–2 page filing receipts.
- **The structural ceiling for free public discovery in RI is essentially
  what we hit.** Going higher requires paid data: Tyler / Cott / IQS town
  clerk feeds (the recorded declarations themselves), or partnerships with
  Associa / FirstService Residential / regional mgmt firms (their walled
  portals' contents).
- **The RI pipeline is the canonical reference for the SoS-first +
  per-entity-Serper pattern.** Cleanly transfers to other small Northeast
  states: CT, NH, ME, VT, and (with adaptation) HI and DC.

## What RI is structurally

39 cities/towns across 5 statistical counties. RI has **no county
government** — counties are statistical only, and **all land evidence is
recorded by the municipal town clerk**. Each of the 39 town clerks runs (or
contracts out) its own search portal:

- 15 use US Land Records (`uslandrecords.com`)
- 6 use Info IQS (`searchiqs.com`)
- 6 use KoFile / Tyler `countyfusion`
- 3 use NewVision Systems
- The rest use Cott Systems, Cranston's `landrecordsonline.com`, Pawtucket's
  ALIS, custom builds at `68.15.39.209`, `townoffoster.com`, etc.

**Every one of these portals is search-only with per-document fees** to
download the actual recorded PDF. None expose a public PDF index that can
be crawled. They are useful as *citations of existence* in a manifest, but
they are not a primary content source.

Documented in full in `state_scrapers/ri/notes/land_evidence_portals.json`.

## Architecture choice that drove everything else

**The first attempt used the keyword-Serper-per-county pattern that worked
for KS, TN, GA, DE.** It failed badly. RI is small enough that
`"Rhode Island" "Bristol County" homeowners association declaration filetype:pdf`
matched federal PDFs, FL/MA "Bristol County" overlap, academic papers about
RI condo law, and Pasco County FL planning documents. After 26 leads banked
from the Bristol-only run, exactly **zero** of them were Rhode Island
HOAs. Killed the run, deleted 211 GCS blobs, pivoted.

**The right architecture for RI was SoS-registry-first, then per-entity
exact-name Serper.** Specifically:

```
RI Secretary of State business search
    ↓ scrape active entities matching HOA name patterns
    ↓ filter by mailing address state, name-pattern post-filter
721 canonical RI HOA/condo entities
    ↓ per-entity Serper: "<exact name>" Rhode Island filetype:pdf
    ↓ per-entity Serper: "<exact name>" "Rhode Island" declaration OR bylaws
    ↓ score by SPECIFIC (non-generic) name token overlap, reject sub-threshold
509 entities with PDF candidates, 221 with website candidates
    ↓ custom probe driver (preserves pre_discovered_pdf_urls)
553 manifests + 1130 PDFs banked
    ↓ prepare_bank_for_ingest.py with --max-docai-cost-usd 15
214 docs prepared into 138 bundles ($1.29 DocAI spend)
    ↓ POST /admin/ingest-ready-gcs?state=RI&limit=50 (loop)
198 HOAs live with documents
    ↓ ZIP-centroid backfill via zippopotam.us
197 of 198 mappable
```

**Generalize the picking rule.** Use the SoS-first pattern when:

1. State county/town names overlap with other states (Bristol, Newport,
   Washington, Lincoln — every state has a Lincoln County; Rhode Island
   itself appears in non-RI federal/legal docs).
2. Land records are at the municipal level and walled.
3. Population is small enough that a per-entity Serper (~$0.30 per 1,000
   queries) is cheap.

This will be the right pattern for **CT, NH, ME, VT** for sure, and
probably HI and DC. It's the wrong pattern for FL, TX, CA where county
recorders publish PDFs and county names are unique.

## The four discovery strategies, ranked by yield

### Strategy 0: SoS-registry-first + per-entity Serper *(the working pipeline)*

Yield: **721 canonical entities → 198 imported → 55 substantive.** This is
the entire usable corpus.

Two scripts do the work:

- `state_scrapers/ri/scripts/scrape_ri_sos.py` — ASP.NET WebForms scraper
  for `business.sos.ri.gov/CorpWeb/CorpSearch/`. Three quirks worth noting
  (each cost me an hour to figure out):

  - **The "F" mode (contains-search) treats the input as a single token.**
    Multi-word queries return zero matches even when the literal substring
    exists. Use single-word patterns: `condominium`, `homeowners`,
    `owners`, `civic`, `townhouse`, `townhome`, `estates`, `village`,
    `commons`. Then post-filter by name pattern (`HOA_NAME_RE`) to drop
    `American Realty Owners`, `Civic Initiatives, LLC`, `Townhouse Pizza`,
    etc.
  - **Pagination needs the full preserved form, not just the hidden
    fields.** A minimal `__VIEWSTATE` + `__EVENTVALIDATION` POST returns
    the empty search page. You must include `__VIEWSTATEENCRYPTED`,
    `__LASTFOCUS`, and the post-search hidden fields.
  - **POST URL flips per request.** First-page search redirects (302) to
    `CorpSearchResults.aspx`; pagination postbacks must go to whichever
    URL the most recent response actually came from
    (`response.url`, after redirects), not the form's `action` attribute.
    Easy fix: just use `response.url` from the last 200 response.

  Takes ~5 minutes to scrape 721 entities. Output: clean JSONL with
  name, sos_id, NAICS, mailing address (street, city, state, ZIP),
  parsed and county-tagged via a hardcoded `CITY_COUNTY` map that includes
  postal village → municipality fixups (Chepachet → Glocester, Rumford →
  East Providence, etc.).

- `state_scrapers/ri/scripts/enrich_ri_leads_with_serper.py` — per-entity
  exact-name Serper search that scores hits on **specific (non-generic)
  name token overlap**. The key insight is that a candidate must share at
  least 1 specific token with the entity name; a hit that contains only
  generic tokens like `condominium`, `association`, `Rhode`, `Island`
  scores -50. This kills 95% of the false positives that broke Strategy 1
  (the original keyword-Serper attempt) and Strategy 2 (site:-restricted).

  Hit rate: 71% of entities (509/721) got at least one PDF candidate. Most
  of those are SoS corporate filings (`business.sos.ri.gov/CORP_DRIVE/...`)
  that Serper indexes. The rest are independent HOA websites and the
  occasional municipal planning doc.

The custom probe driver `state_scrapers/ri/scripts/probe_enriched_leads.py`
exists because the stock `python -m hoaware.discovery probe-batch`
constructs `Lead` from JSON via `Lead(**d)` and *silently drops*
`pre_discovered_pdf_urls`. This is a footgun for any flow where leads
carry curated PDF URLs. The DE runner sets `category_hint=cat` per doc
when it banks; if you rely on that path, fine — but if you go through
`probe-batch`, you need the wrapper.

### Strategy 1: Management-company crawling *(0 yield)*

Hypothesis: the SoS data clusters HOAs by mailing address. Top clusters
identify the management firm; if the firm publishes governing docs
publicly, harvest them.

Cluster analysis: 12 address clusters of ≥4 entities cover 125 of 721
HOAs. Top clusters:

| Address | Entities | Firm (identified via Serper Places) | Website |
|---|---|---|---|
| 181 Knight St, Warwick | 46 | The Hennessy Group | thehennessygrp.com |
| 222 Broadway, Providence | 11 | Divine Investments | divineinvestments.com |
| 615 Jefferson Blvd, Warwick | 10 | Brock / Heffner offices | (no mgmt site) |
| 498 Main St, Warren | 9 | Apex Management Group | apexmanagementgroup.net |
| 250B Centerville Rd, Warwick | 6 | Summit Management | summit-mgmtri.com |
| 76 Westminster St, Providence | 5 | Acropolis Management | (LinkedIn/Facebook only) |
| 786 Oaklawn Ave, Cranston | 4 | C.R.S. Realty | crsmgmt.com |
| 75 Lambert Lind Hwy, Warwick | 4 | Picerne Real Estate | picerne.com |

**Result: zero governing-doc PDFs** from any firm. Every one of them
walls their document library:

- Hennessy: AppFolio + ManageBuilding (`signin.managebuilding.com`,
  `thg.appfolio.com`)
- Apex: CINC Web Axis (`apexmg.cincwebaxis.com`)
- Summit: SecureCafe (`securecafe3.com`)
- CRS: ManageBuilding (`crsmgmt.managebuilding.com`)
- Divine: no document portal at all (paper-based small firm)
- Picerne: blocked behind Cloudflare to anonymous traffic

This is **a deliberate industry choice**. RI condo mgmt firms run their
document libraries as a paid amenity for residents/board members. They are
not going to publish CC&Rs to anonymous Google crawlers — that would
remove their value proposition.

Lesson: **don't waste time on this strategy in any other Northeast state**
where the same firms (FirstService Residential, Associa, The Dartmouth
Group, Brigs LLC, Barkan Management) operate. They use the same walled
platforms. The CAI New England Resource Directory confirms the same firms
manage condos across MA, ME, NH, RI, VT.

The scripts are kept for the next state's first attempt because verifying
this empirically is faster than reasoning about it from scratch:

- `state_scrapers/ri/scripts/find_mgmt_companies.py` — address cluster
  analysis + Serper Places identification
- `state_scrapers/ri/scripts/harvest_mgmt_companies.py` — site-search +
  HTML crawl + name-token matching back to cluster members

Both produce a clear empirical "no, this firm walls everything" result
within minutes.

### Strategy 2: site:-restricted Serper to known HOA-doc-host platforms *(0 real matches)*

Hypothesis: some small HOAs publish governing docs on common SaaS hosts
that Google indexes — `hoa-express.com`, `hoastart.com`, `cinchosting.com`,
`*.squarespace.com`, `s3.amazonaws.com`, etc.

Ran 27 site:-restricted queries. **110 unique PDF hits.** Matched
**7 to existing RI HOAs by name-token overlap.** Audited each:

- Cheshire CT Master Plan ≠ South Trail Commerce Center (CT town doc)
- TN govt + a Condo P&S template ≠ Oakland Beach Real Estate Owners
- PRRAC research paper ≠ Lincoln Mobile Estates
- Fairfax County Community Association Manual ≠ South County Trail HOA
- Providence Eastside Resale Certificate Sample (template, not this HOA's)
- The CAI directory itself, attached to "New England Grand Banks"
- Warwick city zoning preliminary plan ≠ Bay View Townhouses

**Zero of the 7 were real governing docs for the named HOA.** Every one was
a coincidental name-token collision.

Why it failed: the `site:` operator finds documents on those platforms,
but the *kinds* of documents indexed publicly there tend to be marketing
brochures, sample resale certificates, planning department PDFs from
unrelated municipalities — not actual governing docs. The HOAs that *do*
host their docs on Squarespace or hoa-express tend to scope their doc
URLs behind opaque paths (`/private-files.api.hoa-express.com/document/<id>/...`)
that aren't useful for general indexing.

Script kept for completeness: `state_scrapers/ri/scripts/site_restricted_serper.py`.
For the next state, run it once to confirm zero yield, then move on.

### Strategy 3: CAI New England directory *(intelligence value, 0 docs)*

Hypothesis: the Community Associations Institute's New England chapter
publishes an annual resource directory listing member firms and their
managed associations. The 2024-2025 PDF was 164 pages.

What it gave me:

- A more complete inventory of NE-regional firms with RI presence:
  Barkan Management (200+ communities), Brigs LLC (220 / 12,500 units),
  First Realty Management Corp (36 / 4,072), **FirstService Residential**
  (9,000 communities, 2M+ units across MA, ME, NH, RI, VT), Franklin Square
  Management (60 / 2,000), The Dartmouth Group / Associa (160 / 17,000+).
  All large enough to be on commercial walled portals (TownSq,
  ConnectResident, AppFolio, FrontSteps).
- Smaller RI-specific firms that I'd missed in clustering: Seaflower
  Property Management (11 S Angell St), Image Property Management
  (PO Box 8889 Cranston), PMI of Rhode Island, Building Engineering
  Services, LLG Realty Lincoln, First Choice Property Management
  (48 Hamlet Ave Woonsocket).
- One association name (Newport Green Condominium) not in my SoS dataset.

What it didn't give: any per-association governing-doc URLs. The directory
lists firms and their high-level service areas; it doesn't list individual
HOA names with their documents.

Net product impact: 0 new documents, but useful confirmation that the
walled-portal pattern dominates the RI mgmt firm landscape. Worth fetching
in the next state's first hour to map the firm ecosystem; not worth more.

## The structural ceiling

By the end of all four strategies, the corpus stood at 198 HOAs / 300 docs
/ 6,145 chunks. **The remaining ~520 unimported RI HOAs and the 143 thin
profiles fall into three buckets:**

| Bucket | Approx count | Why we can't reach them |
|---|---|---|
| Walled mgmt portals | ~350 | AppFolio, CINC, ManageBuilding, SecureCafe, TownSq, ConnectResident, FrontSteps. Per-resident login required. |
| Walled town clerk recorders | covers all condos | Tyler / Cott / IQS / NewVision / custom. Per-document fees, search-only, ToS-hostile. |
| Paper-only / no website | ~150 | Truly nothing digital exists for the small or older entities. |

**Free public discovery in RI is essentially capped at what the SoS-first
pipeline produced.** To break through, you need a paid data deal:

- **Title-company-grade access to one of the recorder vendors** (Tyler /
  Cott / IQS) for the actual recorded declarations. This is the highest-
  yield single move — recorded condo declarations are exactly what the
  product needs. Pricing varies; this is normally sold by-document or
  by-bulk-feed to title insurance and real estate companies.
- **API partnerships with FirstService / Associa / Brigs / Dartmouth.** The
  walled portals contain bylaws, declarations, rules, minutes, and
  insurance. These firms typically don't have public APIs but may sell
  bulk feeds to ProSearch / RealManage / similar.

Both are out of scope for a free small-state autonomous run. Both will be
the right answer for any state with the same legal structure (CT, MA, NH).

## Why the rejection rate looks high but is correct

`prepare_bank_for_ingest.py` rejected 633 of 1,128 banked documents (56%).
Initial reaction: "that's a bug; we're losing legitimate governing docs."
After auditing, **the rejections are well-calibrated**:

| Reason | Count | Verdict |
|---|---|---|
| `junk:government` | 231 | Correct — almost all are SoS Annual Reports (Form 631), routine yearly compliance filings that just list officers + corporate address. Not governing docs. |
| `pii:membership_list` | 108 | Correct — owner rosters with names + addresses. PII policy. |
| `unsupported_category:unknown` | 107 | Correct — also mostly SoS Annual Reports + Serper-enrichment false positives (a Thundermist Health Annual Report attached to Arctic Village Association, etc.). |
| `junk:unrelated` | 79 | Correct — RI senate bill text, Palo Alto Weekly, industry magazines that bled through name-token matching. |
| `junk:court` | 38 | Correct policy — court rulings about an HOA aren't governing docs. |
| `duplicate:prepared` | 31 | Correct — already prepared. |
| `junk:tax` | 12 | Correct — Form 990 etc. |
| `page_cap:*` | ~17 | Correct — oversize PDFs (Maryland condo docs over 200 pages) declined. |
| `pii:ballot/violation` | 3 | Correct. |

**Walked back claim:** earlier in this conversation I claimed the playbook
should call SoS corporate-filing PDFs "first-class governing docs" and
hard-tag them as `articles`. That was wrong. The classifier already handles
this correctly — it tags actual Articles of Incorporation as `articles`
(66 survived as such) and rejects Annual Reports as `junk:government`. The
distinction is real and the LLM-backed precheck classifier (deepseek-v4-flash)
captures it. Don't pre-tag SoS URLs in probe; let the classifier decide.
The playbook update reflecting this is in the
"Best Practices Learned" section.

## What a 1-page "articles" PDF looks like vs why thin profiles happen

The classifier kept 66 docs as `articles` from SoS filings. Sample:
**Cedar Ridge Condominium Association** (`gs://hoaproxy-bank/v1/RI/bristol/cedar-ridge/...`)
— a 1-page PDF with **zero extractable text**. Almost certainly a scanned
filing receipt or cover sheet, not the substantive Articles document.
Treated as `articles` because the precheck classifier matched on filename
or filing metadata, but it produces no chunks at ingest because PyPDF
finds no text and we don't OCR every prepared doc.

This is why **41% of imported RI HOAs (82 of 198) have ≤2 chunks**:
their entire prepared corpus is one or two SoS filing receipts that look
like governing docs at the metadata level but have no extractable content.

Practical fix considered but not applied:
**Option B from my pre-retrospective notes** — set a `chunk_count >= 3`
threshold and demote thin HOAs to `metadata_type=stub` to hide from search
without deleting. Reversible. Reduces visible HOA count to ~115 but every
visible HOA has real content. Recommended for product polish but did not
apply since you said "leave it as-is" implicitly by moving to (1)–(3)
strategies.

If you do want to apply this, it's a single SQL update against the live DB:

```sql
UPDATE hoas SET metadata_type = 'stub'
WHERE state = 'RI'
  AND id IN (SELECT hoa_id FROM hoa_documents
             GROUP BY hoa_id
             HAVING SUM(chunk_count) < 3);
```

…or via a small Python script that calls `/admin/backfill-locations` with
`location_quality = 'city_only'` to additionally hide thin ones from the
map (city_only is hidden by default).

## Map enrichment: when public Nominatim becomes a liability

`prepare_bank_for_ingest.py` calls public Nominatim for polygon enrichment
during the prepare phase. **For RI's 198 manifests, public Nominatim
returned 134 `geo_enrichment_error` rows with HTTP 429 + `Retry-After: 0`.**
Once burst-detection trips, the lockout persists for **15+ minutes** even
with 1.2s+ inter-request delay. Empirically tested: a clean retry from a
fresh process at 1.2s delay still 429s for ~20 minutes after the initial
trigger.

**Don't make polygon enrichment from public Nominatim a critical path.**
Treat any polygon it returns as a bonus. For production-grade map coverage,
ZIP centroid is the right primary fallback:

- `https://api.zippopotam.us/us/{zip}` — free, no observed rate limit at
  RI's scale (~40 unique ZIPs cached during the run), returns lat/lon +
  state validation in one call.
- The script `state_scrapers/ri/scripts/enrich_ri_locations.py` does the
  full ZIP-centroid pass: pulls live HOAs without coordinates from
  `/hoas/summary`, looks up SoS-derived ZIPs via zippopotam, falls back
  to a hardcoded `RI_CITY_CENTROIDS` table (50 RI municipalities + village
  centroids), demotes any out-of-state coordinates to `city_only`, and
  posts batches to `/admin/backfill-locations`.

Result: **194 of 198 (98%)** got `zip_centroid` quality. 3 fell back to
`place_centroid` (city centroid). 1 stayed `city_only` (no usable address).
1 out-of-state coordinate (Park Terrace Condominium Association at San
Diego coords from a Nominatim hit on the wrong "Park Terrace") was
correctly demoted.

For other small Northeast states, copy the script and replace
`RI_CITY_CENTROIDS` + `RI_BBOX` with the target state's. ZIP centroids are
visually clustered (they show up at ZIP centers, not at the actual condo)
but they're real coordinates inside the bounding box and they pass the
playbook's 80% map-rate target trivially.

## Render import: a few sharp edges

- **`/admin/ingest-ready-gcs` caps `limit` at 50.** A request with
  `limit=100` returns HTTP 400 `"limit must be between 1 and 50"`. The
  playbook's example showed 100; the playbook is now corrected.
- **The response shape is a `results` array, not top-level imported/processed
  fields.** Walk the array and count `entry.status == "imported"` to get
  the real import count. Initial implementation reading
  `body.get("imported")` returned 0 even when 50 imported successfully,
  which made the loop terminate after one call.
- **The local `JWT_SECRET` in `settings.env` drifts from the live Render
  value.** Render's UI silently rotates sensitive env vars when edited;
  the local file gets stale. The runner now reads the live value via the
  Render API at runtime:

  ```python
  def _live_admin_token():
      if os.environ.get("HOAPROXY_ADMIN_BEARER"):
          return os.environ["HOAPROXY_ADMIN_BEARER"]
      api_key = os.environ.get("RENDER_API_KEY")
      service_id = os.environ.get("RENDER_SERVICE_ID")
      if api_key and service_id:
          r = requests.get(
              f"https://api.render.com/v1/services/{service_id}/env-vars",
              headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
          r.raise_for_status()
          for env in r.json():
              e = env.get("envVar", env)
              if e.get("key") == "JWT_SECRET" and e.get("value"):
                  return e["value"]
      return os.environ.get("JWT_SECRET")
  ```

  Use this from any state-local runner. Local fallback only matters for
  pure-local development.

## Cost summary and per-HOA economics

| Phase | API | Spend (RI run) | Notes |
|---|---|---|---|
| SoS scrape | none | $0 | No auth, polite scraping |
| Per-entity enrichment | Serper | ~$1.00 | 1,442 calls (721 leads × 2 queries) at ~$50 / 100K |
| Mgmt-company + site-restricted strategies | Serper | ~$0.05 | ~50 additional calls including Serper Places |
| Bank-time classification | OpenRouter (deepseek-v4-flash) | ~$0.15 | ~1,130 PDFs × ~1.5K tokens each at $0.07/1M input |
| Prepare-time first-page review (regex first; LLM fallback for ambiguous) | OpenRouter | ~$0.05 | ~200 LLM fallback calls; most resolved by regex |
| Prepare-time OCR | Google Document AI | $1.29 | 861 pages at $0.0015/page (8.6% of $15 cap) |
| Probe / fetch / GCS storage | none | $0 | HTTP fetches only; ~50 MB total bank storage |
| Render-side embedding (state-attributable) | OpenAI text-embedding-3-small | ~$0.005 | ~6,145 chunks × ~800 tokens at $0.02/1M |
| ZIP centroid backfill | zippopotam.us | $0 | Free public API |
| **Total marginal cost for RI** | | **~$2.55** | |

### Per-HOA cost framings

Three framings depending on what you count as the unit:

| Unit | Count | Cost per unit |
|---|---|---|
| **Entity attempted** (every SoS-listed RI HOA that we ran enrichment on) | 721 | **$0.0035 / entity** |
| **HOA imported live** (one row in `hoas` table, regardless of doc richness) | 198 | **$0.013 / live HOA** |
| **HOA with substantive content** (≥10 chunks — the user-facing-useful corpus) | 55 | **$0.046 / useful HOA** |

The most honest framing for project planning is **per substantive HOA**,
which lands at ~$0.05. **Even at 10× the RI scale, scraping a state for
under $25 is realistic.** The dominant cost is DocAI — and the cap is
elastic. Serper and OpenRouter combined are noise (~$1.25).

For comparison, the playbook's small-state DocAI cap is $15. RI used 8.6%
of it. The cap is the right size for states where more docs are scanned
PDFs needing full OCR; for RI most discovered PDFs were the
text-extractable SoS filings, so PyPDF handled the bulk for free.

**What would change the cost profile materially:**

- A state with mostly-scanned recorded declarations (older recorder
  systems with image-only PDFs) — DocAI cost rises 5–10× toward the cap.
- A state with no SoS-equivalent registry — discovery falls back to
  broad-keyword Serper, which is 5–10× more queries to filter the noise
  to the same-quality lead set. Serper cost rises but stays under $10.
- A state with LLM classification disabled (`HOA_DOC_CLASSIFIER_LLM=0`) —
  OpenRouter cost goes to $0 but classification recall drops, more docs
  end up `unsupported_category:unknown` and rejected, more substantive
  docs get lost. Not recommended.

## Reusable scripts (canonical paths)

| Script | Phase | Reusable as-is? |
|---|---|---|
| `state_scrapers/ri/scripts/scrape_ri_sos.py` | SoS registry scrape | Adapt URL, CITY_COUNTY, SEARCH_PATTERNS, HOA_NAME_RE per state |
| `state_scrapers/ri/scripts/enrich_ri_leads_with_serper.py` | Per-entity Serper enrichment | State-agnostic. Just point to the new state's leads file. |
| `state_scrapers/ri/scripts/probe_enriched_leads.py` | Probe-batch wrapper preserving pre_discovered_pdf_urls | State-agnostic. |
| `state_scrapers/ri/scripts/find_mgmt_companies.py` | Address cluster + firm ID | State-agnostic. |
| `state_scrapers/ri/scripts/harvest_mgmt_companies.py` | Firm site crawl + PDF harvest | State-agnostic but expect 0 yield until you find a state where firms publish openly. |
| `state_scrapers/ri/scripts/site_restricted_serper.py` | site:-restricted Serper | State-agnostic. Run once for empirical yield check. |
| `state_scrapers/ri/scripts/enrich_ri_locations.py` | ZIP centroid backfill | Adapt `RI_BBOX`, `RI_CITY_CENTROIDS` per state. |
| `state_scrapers/ri/scripts/run_state_ingestion.py` | End-to-end orchestrator | Skeleton; copy + replace state constants. |

## Concrete recommendations for the next state

### If the next state is CT, NH, ME, VT, MA

These are the closest cousins to RI: walled town/county recorder portals,
heavy condo population, NE-regional walled mgmt firms.

1. **Skip Strategy 0a (broad-keyword Serper).** It will fail the same way.
2. **Start at the SoS registry** (`concord-sots.ct.gov` for CT, NH SoS
   QuickStart, Maine SoS Corporations Search). Each state's WebForms quirks
   will be slightly different — budget an hour for the postback flow.
3. **Apply the same `HOA_NAME_RE` post-filter.** Single-word search
   patterns + name-pattern post-filter is the SoS-first invariant.
4. **Build a CITY_COUNTY map that includes USPS village → municipality
   fixups.** For NH the unincorporated places + grants/locations need
   special handling (e.g. "Hales Location"); for ME the unorganized
   territories don't have a municipality at all.
5. **Don't waste time on mgmt-company crawling.** Spend 15 minutes
   running `find_mgmt_companies.py` to confirm the firm ecosystem, then
   move on. The same big firms (FirstService Residential, Associa /
   Dartmouth, Brigs, Barkan) recur across all NE states.
6. **Budget DocAI conservatively.** RI used $1.29 of $15. CT will be
   bigger but probably still under $5.
7. **Apply the `chunk_count >= 3` thin-HOA filter early** — set
   `metadata_type=stub` on import or post-import. Don't ship 41% thin.

### If the next state is HI, DC

Similar walled-recorder pattern but very different geography and mgmt firm
ecosystem. SoS-first will still be right; mgmt-company stuff will be
different (HI has dominant local firms like Hawaiiana Management; DC has
First Service / Comstock).

### If the next state is FL, TX, CA, AZ

Don't use this RI playbook. Use the keyword-Serper-per-county pattern (KS,
TN, GA precedent). Their county recorders publish PDFs to public .gov
sites; SoS would be redundant.

### Universal applicable lessons

- The playbook is now updated with everything in this retrospective.
  Read `docs/small-state-end-to-end-ingestion-plan.md` first. The
  "Best Practices Learned" section captures the load-bearing rules.
- Use `--apply` only after a dry-run validates the lead JSONL format.
- Always write a final state report JSON to
  `state_scrapers/{state}/results/{run_id}/final_state_report.json` even
  if the run is partial. Future you will want it.
- Commit your scripts and queries before kicking off a multi-hour run.
  External branch activity (other agents, you on another machine) can
  cause merge conflicts that swallow uncommitted work.

## Open questions for paid data partnerships

The natural escalation paths from RI's structural ceiling — these are
notes for whoever decides to pursue paid data:

1. **Tyler / Cott / IQS / NewVision town clerk feeds.** The recorder
   software vendors typically sell bulk extracts to title-insurance and
   government-records aggregators (DataTree, FirstAm, Pacer). Per-state
   coverage varies; for RI you'd need contracts with multiple vendors.
2. **Associa API.** Associa runs The Dartmouth Group, FirstService
   Residential, and many regional brands. Their internal portal is
   TownSq. They've made API access available to specific partners
   (closing/title companies, lender networks). Worth a sales call.
3. **FirstService Residential** is a separate company from Associa
   (despite naming overlap). Different sales process.
4. **CAI New England** runs the regional trade association. They could
   broker introductions to multiple firms at once and might have
   partner-pricing arrangements.

None of these are blockers for the product. The free-discovery floor of
~25% coverage with high-quality content for the substantive subset is
already a useful product surface — searchable by name, mappable, with
confirmed legal-entity existence. Paid data is the way to multiply
content quality, not table-stakes.

## Final state, for reference

```json
{
  "state": "RI",
  "live": {
    "hoa_count": 198,
    "document_count": 300,
    "chunk_count": 6145,
    "total_bytes": 367201425,
    "map_points": 197,
    "map_rate": 0.995,
    "by_quality": {"zip_centroid": 194, "place_centroid": 3},
    "out_of_state_points": 0,
    "thin_hoa_count_le_2_chunks": 82,
    "substantive_hoa_count_ge_10_chunks": 55
  },
  "pipeline_costs_usd": {
    "serper": 1.05,
    "openrouter_classifier": 0.20,
    "docai": 1.29,
    "openai_embedding": 0.005,
    "total": 2.55
  },
  "cost_per_hoa_usd": {
    "per_entity_attempted": 0.0035,
    "per_imported_hoa": 0.013,
    "per_substantive_hoa_ge_10_chunks": 0.046
  },
  "ceiling_explanation": [
    "~350 RI condos managed by walled-portal firms (AppFolio/CINC/etc.) — paid login required",
    "All recorded declarations behind town clerk portals (Tyler/Cott/IQS) — per-doc fees",
    "~150 paper-only entities — nothing digital exists"
  ],
  "next_step_to_break_ceiling": "paid data: town clerk feed (highest yield) or mgmt firm portal partnership"
}
```

— Written for the next person who tries this. Don't repeat the dead ends.
