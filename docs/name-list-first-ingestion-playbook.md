# Name-List-First HOA Ingestion Playbook

This is the canonical playbook for ingesting HOAs/condos/cooperatives in
jurisdictions where **the entity universe exists in a public registry but the
governing documents do not exist in the public web index**. It is a
state-agnostic alternative to
[`docs/multi-state-ingestion-playbook.md`](./multi-state-ingestion-playbook.md)
(the "keyword-Serper-per-county" playbook). The two playbooks complement each
other; pick one per state based on the suitability matrix in §1.

---

## Intent and success framing

**The job is to find every plausible HOA in the state's public surface area, up to but not over the budget envelope.** This is a source-exhaustion task, not a target-attainment task — success is measured by what's left untried, not by hitting a numeric floor. Three rules that follow:

1. **Anchor to the registry size, not to prior session baselines.** The registry is your universe — `live_count / bank_entity_count` is the source-stop-aligned success metric (target ≥10% per §5e). If the registry has 3,289 entities and the run lives 200, the session is incomplete; document discovery has more sources to try (broader Serper templates, mgmt-company harvesting, a second pass with relaxed filename heuristics, etc.).

2. **Budgets are envelopes, not ceilings.** Plan to use 70–90% of each cost cap. Under-spending more than that without a written diminishing-returns justification is a failure mode, not a virtue. Concretely: if you used $5 of a $25 DocAI cap, the retrospective must answer "the next $X of DocAI would have yielded < $X because [specific reason]" — not "we came in well under cap."

3. **Source-stop, not target-stop.** Stop conditions are about *sources*: stop when (a) every entity in the registry has been swept ≥2 times with widened query templates, **and** (b) management-company harvesting / aggregator harvest / page-1-OCR-verification (per §3b) have been run or formally declined, **and** (c) the budget envelope is 70–90% spent. A run that satisfies the `live / bank_entity_count ≥ 10%` floor but stopped after one Serper template per entity is a partial run.

**Required structural counterweight: source-family inventory.** Produce `state_scrapers/{state}/notes/source-inventory.md` listing every document-discovery source considered (3 default Serper templates per entity, jurisdiction-specific governing-doc vocabulary variants, mgmt-co portfolios, recorded-doc registry direct probes, page-1 OCR cross-validation, second-pass discovery with relaxed filters, …) — each marked productive / sterile / untried-with-reason. The Phase 10 retrospective cross-references it. Without it, "what didn't I try" is hand-wavable; with it, the gap is visible.

**Defer retrospective drafting** until the source-family inventory is exhausted. Drafting wrap-up text mid-session is a tell that the agent has mentally checked out; redirect to the next yield lever.

The IL downstate session (May 2026, keyword-Serper sister playbook) is the canonical example of this failure mode and what to do differently — see `state_scrapers/il/notes/retrospective-downstate.md` "What didn't work" + "Lessons for the playbook" for a worked postmortem. The same failure shape applies here.

---

## 0. When to use this playbook (vs. the keyword-Serper one)

The keyword-Serper playbook works when:
- **HOAs publish governing docs to the public web** (community websites,
  management-co portals, recorder PDFs that Google has crawled).
- The state is **suburban-density** with one HOA = one website (KS, TN, GA, FL).

It fails when:
- **Public documents are paywalled** (e.g., HI Bureau of Conveyances,
  DC Recorder of Deeds, NYC ACRIS — the registry is public but per-record
  retrieval is paywalled).
- **Documents are behind login walls** (CINC, AppFolio, FrontSteps,
  TownSq — the dominant condo management platforms).
- The jurisdiction is **dense urban condo/coop** stock where each building's
  governing docs aren't on a community website at all (DC, NYC, Chicago,
  Honolulu, Boston, SF condos).

In those cases, the keyword-Serper sweep finds a few percent of the universe
and a lot of junk (court filings, news articles, government planning packets
that mention the words "homeowners association"). The right pattern is:

1. **Bypass Serper for entity discovery** — pull the complete list of
   registered HOAs/condos/coops directly from the authoritative registry.
2. **Use Serper only for document discovery, anchored on entity names** that
   already exist in the registry. Each query is high-precision because it
   asks "find a PDF for THIS specific named entity."
3. **Pin the canonical name from the registry** to every banked manifest, so
   junk SERP titles (court captions, paper titles, mgmt-co marketing pages)
   never overwrite the entity identity.

### State suitability matrix

| Pattern | When to use | Canonical example |
|---|---|---|
| **Keyword-Serper-per-county** (existing playbook) | Suburban-density state, county recorders publish PDFs, HOAs run their own community sites | KS (canonical Tier 1), TN (Tier 2), GA (Tier 3), FL (Tier 4) |
| **Name-list-first** (this playbook) | Dense urban condo/coop, registry exists but docs paywalled / login-walled | **DC** (CONDO REGIME table, 3,289 projects, this playbook's reference run) |
| Hybrid | Mid-density state where registry covers part of the universe — run both, dedup | NV Clark (registry covers HOAs but not condos in the same way) |

States to consider for name-list-first when their queues come up:

- **Tier 0/1**: HI (Hawaii Real Estate Branch condo registry; condo-heavy state).
- **Tier 2**: NJ (DCA registered cooperatives), CT (DOC registered communities), MA (Cape & Boston condo registry).
- **Tier 3**: NY (DCP/ACRIS for NYC condos+coops; ~30k-50k entities), IL (Cook County registered condos), MA Greater Boston, WA King County.
- **Tier 4**: CA (CDPR condo registrations + CalHFA assisted projects).

---

## 1. Required prerequisites

Identical to the keyword-Serper playbook (GCS, DocAI, Serper, Render admin
auth) plus two extras:

- **A registry endpoint or downloadable file** that yields canonical entity
  names. See §2 for how to find this.
- **A HERE Geocoding API key** (`HERE_API_KEY` in `settings.env`) for the
  Phase 9 / address-enrichment pass. Free tier 250k transactions/mo —
  comfortably covers any single-state run. Sign up at
  https://platform.here.com/sign-up. HERE replaced public OSM Nominatim as
  the production geocoder on 2026-05-09 after the DC stub experiment showed
  HERE recovered ~93% of polygon centroids to street-level address quality
  in ~5 min wall-time vs. Nominatim's ~95 min. The Nominatim path remains in
  `state_scrapers/_orchestrator/dc_stub_addresses.py` as a zero-setup
  fallback for runs without HERE access.

This pattern uses the same `prepare_bank_for_ingest.py` → `/admin/ingest-ready-gcs`
→ `phase10_close.py` chain as the keyword-Serper playbook. Phases 5–10 are
unchanged. The novelty is entirely in **Phase 1 (registry pull)** and
**Phase 2 (name-binding discovery)**.

---

## 2. Phase 1 — Find the registry and pull the entity universe

The registry source varies wildly by jurisdiction. **Spend up to ~60 minutes
hunting** before falling back to keyword-Serper. Likely sources, in order:

### 2a. State or county GIS REST endpoints (fastest path)

Many state/county GIS portals expose public ArcGIS REST FeatureServers that
include condo registry tables. Probe:
```
https://maps.{state-gis-domain}/.../rest/services/?f=json
https://maps.{state-gis-domain}/.../rest/services/{folder}/?f=json
```
Look for tables named `CONDO`, `CONDOMINIUM`, `CAMA`, `REGIME`, `COOPERATIVE`,
`COMMERCIAL`, `OWNERPLY`, or layer descriptions mentioning "registered
condominiums" / "condominium projects."

**DC reference run (canonical example):**
```
Service catalog:  https://maps2.dcgis.dc.gov/dcgis/rest/services?f=json
FeatureServer:    https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/Property_and_Land_WebMercator/FeatureServer
Table 72:         CONDO REGIME — 3,289 condominium projects with NAME, REGIME_ID, COUNT (units), COMPLEX
Table 24:         CONDOMINIUM (CAMA) — 61,284 condo units (joinable to projects via SSL/CMPLX_NUM)
Table 23:         COMMERCIAL (CAMA) — cooperatives live here (filter on USECODE_DESC or property class)
Layer 44:         Condo Approval Lots — geometry for approved projects
```

Pull pattern (paginated):
```python
def fetch_paginated(table_id, where="1=1", page_size=1000):
    out = []
    offset = 0
    while True:
        params = {
            "where": where, "outFields": "*", "f": "json",
            "resultRecordCount": page_size, "resultOffset": offset,
            "orderByFields": "OBJECTID",
        }
        body = requests.get(f"{FEATURESERVER}/{table_id}/query", params=params, timeout=120).json()
        feats = body.get("features") or []
        if not feats: break
        out.extend(f.get("attributes") or {} for f in feats)
        if len(feats) < page_size: break
        offset += page_size
    return out
```

**Reference implementation:**
[`state_scrapers/_orchestrator/dc_cama_pipeline.py`](../state_scrapers/_orchestrator/dc_cama_pipeline.py)

### 2b. State agency registration databases

Most condo-active states have a registration requirement:
- **HI**: Hawaii Real Estate Commission's Condominium Property Regime Section
  (DCCA Professional and Vocational Licensing). Public list at dcca.hawaii.gov.
- **NJ**: Department of Community Affairs (DCA) Planned Real Estate Development
  Full Disclosure Act registry.
- **CT**: Department of Consumer Protection registered common-interest
  community list.
- **NY**: Department of State (DOS) condo and coop offering plans (Attorney
  General review database).
- **CA**: Department of Real Estate (DRE) Public Reports for subdivision sales,
  including condos.
- **FL**: Sunbiz already covered (canonical Tier 4 keyword-Serper run).

These are usually scraped via a search form or paginated table. Approach is
state-specific; budget 1–4 hours per state to write the scraper.

### 2c. Property-tax records (always available, sometimes useful)

County or state property tax assessor databases include condo unit records.
Often cleaner than a registry but with much higher volume (one record per
unit, not per project). Useful when 2a/2b are unavailable.

### 2d. Management-company harvesting (fallback)

If no registry is accessible, the largest residential management companies in
the jurisdiction often publish their client portfolios:
- HI: Hawaiiana Management (~700+ condos), Associa Hawaii, Touchstone Properties.
- DC: FirstService Residential DC, Comsource, Legum & Norman.
- NYC: Douglas Elliman, Cooper Square Realty, Wallack Management.
- Chicago: Sudler Property Management, FirstService Chicago.

This is the same pattern as `state_scrapers/ri/scripts/find_mgmt_companies.py`
and `harvest_mgmt_companies.py` from the keyword-Serper playbook, but used as
the **primary** entity-discovery path rather than a supplement.

### 2e. Output — the seed JSONL

Whatever source(s) you use, produce a deduplicated seed file at:
```
state_scrapers/{state}/leads/{state}_namelist_seed.jsonl
```
With one line per canonical entity:
```json
{
  "name": "218 Vista Condo",
  "state": "DC",
  "county": "DC",
  "metadata_type": "condo",
  "registry_id": 3014,
  "unit_count": 17,
  "address": {"street": null, "city": "Washington", "state": "DC", "postal_code": "20020"},
  "source": "dc-gis-cama-condo-regime",
  "source_url": "https://maps2.dcgis.dc.gov/dcgis/rest/services/.../FeatureServer/72"
}
```

**Required fields:** `name`, `state`, `county`, `source`, `source_url`. Address
optional but strongly recommended — even just `city` materially improves
later geo enrichment by ZIP centroid.

---

## 3. Phase 2 — Name-binding discovery

This is the load-bearing phase. **The bug the existing keyword-Serper script
has when used in name-list mode**: `benchmark/scrape_state_serper_docpages.py`
calls `infer_name(title, url, snippet)` to derive the HOA name from each SERP
result's metadata. When you query `"218 VISTA CONDO" "Washington DC"` and a
court filing PDF mentions that condo, `infer_name()` produces a name like
`"1 of 84 - Courts Hunterview Condominium Unit. Owners Association"` from the
filing's caption — and that becomes the bank manifest's `name`. The Phase 10
LLM rename pass then correctly identifies the document as not-a-governing-doc
and rejects the entity.

**The fix is structural**, not query-tweaking: when discovering for a known
entity, the canonical name from the registry must be **pinned** to the bank
manifest. The PDF (whatever it turns out to be) becomes a `document` under
that entity. Phase 10's filename audit later catches PDFs that don't actually
belong to that entity.

### 3a. Discovery script contract

Build a state-agnostic script
[`state_scrapers/_orchestrator/namelist_discover.py`](../state_scrapers/_orchestrator/namelist_discover.py)
with this CLI:

```bash
.venv/bin/python state_scrapers/_orchestrator/namelist_discover.py \
  --seed state_scrapers/dc/leads/dc_namelist_seed.jsonl \
  --state DC \
  --state-name "District of Columbia" \
  --bank-bucket hoaproxy-bank \
  --ledger state_scrapers/dc/results/{run_id}/namelist_ledger.jsonl \
  --max-pdfs-per-entity 4 \
  --max-results-per-query 8 \
  --probe-timeout 240 \
  --apply
```

**Per entity in the seed file:**

1. Build name-anchored queries (3 is a good default):
   ```
   "<NAME>" "<STATE_NAME>" filetype:pdf
   "<NAME>" "<STATE>" "Bylaws"
   "<NAME>" "<STATE>" "Declaration"
   ```
   Add jurisdiction-specific variants when the state's governing-doc
   vocabulary differs:
   - DC + HI (condo-heavy): add `"<NAME>" "Declaration of Condominium"`
   - NY + NJ: add `"<NAME>" "Offering Plan"`, `"<NAME>" "Cooperative Bylaws"`
   - CA: add `"<NAME>" "CC&Rs"`, `"<NAME>" "Declaration of Restrictions"`

2. Filter SERP results aggressively before downloading:
   - Must be a PDF (URL ends `.pdf` or has `format=pdf`).
   - URL/filename/snippet must hit a **governing-doc keyword**:
     `bylaws|by-laws|declaration|ccr|cc&rs?|covenants?|restrictions?|rules?|
     regulations?|articles?\s+of\s+incorporation|amend(?:ment)?|master\s+deed|
     condominium\s+(?:declaration|bylaws|association)|offering\s+plan|
     cooperative\s+(?:bylaws|declaration)`.
   - Junk-host blocklist (case-insensitive substring on the URL host):
     `casetext, courtlistener, justia, scholar.google, ssrn, jstor, papers.ssrn,
     law.cornell, leagle, casemine, govinfo.gov/content/pkg, congress.gov,
     pacer, lexis, westlaw, .news, news.,  reuters, bloomberg, nytimes,
     washingtonpost, wsj, cnn, marketwatch, foxbusiness, bizjournals,
     scribd, issuu, yumpu, dokumen, pdfcoffee, fliphtml5, zillow, redfin,
     trulia, realtor, homes.com, mls, 55places, apartments, rent`.
   - State-hint check (URL or snippet must mention `<STATE_NAME>`,
     `<STATE>`, or a known city in that state).

3. Download PDF with sanity caps:
   - 30 KB ≤ size ≤ 30 MB
   - Content-Type starts with `application/pdf` (or unknown — many
     misconfigured servers send octet-stream)
   - 30 second timeout; 1 retry then skip

4. **Bank with the canonical name pinned.** Call `hoaware.bank.bank_hoa()`
   directly with `name=<seed name>`. Do NOT route through
   `benchmark/scrape_state_serper_docpages.py`'s probe loop — that script's
   `infer_name()` will overwrite your name.

   ```python
   from hoaware.bank import DocumentInput, bank_hoa
   bank_hoa(
       name=seed["name"],                       # <-- pinned, not inferred
       metadata_type=seed.get("metadata_type", "condo"),
       address=seed.get("address") or {"state": state, "county": county},
       geometry={},
       website=None,
       metadata_source={
           "source": seed["source"],
           "source_url": seed["source_url"],
           "registry_id": seed.get("registry_id"),
           "unit_count": seed.get("unit_count"),
           "discovery_pattern": "name-list-first",
           "search_query": query,
       },
       documents=[
           DocumentInput(
               pdf_bytes=pdf_bytes,
               source_url=result_url,
               filename=derive_filename(seed["name"], result_url),
               category_hint=guess_category(result_url, snippet),  # bylaws|ccr|...
               text_extractable_hint=None,                          # let prepare decide
           )
       ],
       state_verified_via=f"{state.lower()}-namelist-first",
       bucket_name=bank_bucket,
   )
   ```

5. Per-entity ledger entry — log every query, every SERP result, every
   keep/skip decision, every download outcome, every bank result. This is
   how you debug yield discrepancies later.

### 3b. PDF-fit verification (optional but strongly recommended)

The biggest false-positive class with name-anchored Serper is "PDF mentions
the entity name once but isn't its governing doc." E.g., a real-estate
broker's listing PDF that includes "Walk Score: 88. The Watergate
Condominium." in its description.

Two complementary checks:

1. **Filename heuristic** (cheap, deterministic):
   Reject PDFs whose URL/filename contains `listing|brochure|appraisal|
   prospectus|annual\s+report|news|press|article|blog|review|inspection|
   invoice|receipt|coupon|estoppel|closing\s+statement|tax\s+return|990`.

2. **Page-1 OCR keyword check** (more expensive — uses DocAI; gate behind
   `--verify-page-1` flag): after download, OCR page 1 only and require it
   contain at least one of: `governing|covenants|declaration|bylaws|
   articles|condominium\s+(?:declaration|act)|recorded\s+in|recorder\s+of\s+
   deeds|register\s+of\s+deeds|HOA|homeowners|unit\s+owners`. Reject if not.

For DC's reference run with 3,289 entities, **page-1 OCR verification is too
expensive** ($0.0015/page × 3,289 × 3 PDFs avg = ~$15 just for page-1 of
candidates that haven't been deduped or filtered yet). We rely on filename
heuristic + Phase 10 LLM rename pass instead. For smaller registries (<500
entities), `--verify-page-1` becomes affordable and dramatically reduces
junk-doc banking.

### 3c. Wall-time and budget shape

Per entity, with 3 queries × ~8 SERP results × 1 PDF download avg:
- Serper: ~3 calls × $0.001 = **$0.003 / entity**
- PDF downloads: ~5 attempts × ~1.5s each = ~8s wall
- Bank write: ~2s (GCS roundtrip + manifest merge)

**Total: ~10–15s per entity, $0.003 Serper + $0.005–0.02 DocAI (page-1 check
if enabled)**. For a 3,000-entity registry that's:
- ~9 hours wall (single-threaded; acceptable overnight)
- ~$10 Serper + ~$0–60 DocAI

**Parallelism**: the script can safely run with N=4 worker threads (Serper
allows it, GCS bank writes are atomic per slug). 3,000 entities at N=4 →
~2.5 hours. Don't go above N=4 — Serper rate-limits at 60 QPS but our
account is on a slower tier.

### 3d. Idempotency and resume

The bank dedups by `(state, county, slug)` — same canonical name → same slug
→ same manifest path. Re-running discovery for the same entity merges new
documents into the existing manifest (deduped further by SHA). So:

- Re-running the whole script after a partial failure is **safe**.
- The seed JSONL can grow over time — append new entities, re-run; old
  entities just rediscover (cheap; mostly cache hits because GCS dedups).
- A separate `--skip-existing` flag scans `gs://hoaproxy-bank/v1/{STATE}/`
  for existing slugs and skips entities already banked.

---

## 4. Phase 3 — Prepare + import + Phase 10 (unchanged from keyword playbook)

These phases are **identical** to the keyword-Serper playbook. Just point
them at the same `gs://hoaproxy-bank/v1/{STATE}/` prefix and they'll pick up
both keyword-discovered and name-list-discovered manifests indiscriminately.

```bash
# Phase 7 — prepare bundles
.venv/bin/python scripts/prepare_bank_for_ingest.py \
  --state DC --max-docai-cost-usd 25 \
  --ledger state_scrapers/dc/results/{run_id}/prepared_ingest_ledger.jsonl \
  --geo-cache state_scrapers/dc/results/{run_id}/prepared_ingest_geo_cache.json \
  --bank-bucket hoaproxy-bank --prepared-bucket hoaproxy-ingest-ready

# Phase 8 — import (loop until results is empty; cap 50/call)
curl -sS -X POST "https://hoaproxy.org/admin/ingest-ready-gcs?state=DC&limit=50" \
  -H "Authorization: Bearer $LIVE_JWT_SECRET"

# Phase 9 — location enrichment (HERE primary, ZIP-centroid fallback)
# Requires HERE_API_KEY in settings.env. Free tier 250k/mo.
.venv/bin/python state_scrapers/_orchestrator/dc_stub_addresses_here.py \
  --apply  # state-agnostic; same pattern works for any state
# Fallback when no HERE key configured — slower (~1 req/s) but free:
# .venv/bin/python state_scrapers/_orchestrator/dc_stub_addresses.py --apply

# Phase 10 — close (rename + delete + audit + retrospective)
.venv/bin/python scripts/phase10_close.py \
  --state DC --bbox-json '{"min_lat":38.79,...}' \
  --run-id {run_id} --apply
```

The orchestrator pattern from
[`state_scrapers/_orchestrator/run_overnight.py`](../state_scrapers/_orchestrator/run_overnight.py)
can be adapted to drive a single state through all phases including a
name-list-first discovery step before the standard runner.

### Differences in expected Phase 10 behavior

The LLM rename pass behaves slightly differently for name-list-first state
runs because the bank already carries clean canonical names from the
registry. Specifically:

- `is_dirty()` regex hits should drop dramatically (registry names are
  clean by construction).
- `--no-dirty-filter` LLM pass mostly confirms `canonical_name == old_name`
  for real entities. The remaining `canonical_name=null` rejects are entities
  where the banked PDF turned out to belong to a different entity (page-1
  text doesn't mention the registry name) — these should be rare with good
  Phase 3b filtering.
- **Hard-delete rate target: < 10%** of imported entities (vs. 35–47% for
  keyword-Serper-discovered states like WY/SD per the keyword playbook
  Phase 10 retrospective bullet 14).

If your hard-delete rate is higher than 10%, the issue is upstream: either
the registry contains non-HOA entries (rare — registries are usually
authoritative) or your Phase 3b PDF filter is letting through too many
unrelated docs. Tighten 3b before re-running Phase 10.

---

## 5. Risks specific to this pattern

### 5a. Registry quality issues

Some registries include entities that aren't actually HOAs in the
hoaproxy sense:
- "Project not built" / "Withdrawn registration" entries — registry rules
  vary by state.
- Test/admin entries with placeholder names ("XXX TEST CONDOMINIUM").
- Sub-projects of master condos that share governing docs with the parent.

Filter at seed-load time:
- Drop names matching `\b(test|sample|placeholder|withdrawn|cancelled|
  rescinded|inactive)\b`.
- Drop names < 4 chars.
- Drop names with no alphabetic content.

For DC, the CONDO REGIME table had no test entries; for NY DOS the
withdrawal rate is ~5% so filter matters more.

### 5b. PDF impostor problem

Same name appearing in multiple states/jurisdictions (e.g., "Park Place
Condominium" exists in 30+ cities). Mitigations:

1. **Always include state in the search query** (already in the default
   query template).
2. **Page-1 OCR cross-validation** in Phase 5 (existing playbook §Phase 5
   "Post-OCR content cross-validation" — applies here too): if the manifest
   is under `v1/DC/...` but page-1 OCR text says "Pittsburgh, PA," reject
   with `decision: "manifest_rejected", reason: "ocr_state_mismatch:PA"`.
3. **Bucket-binds-bbox invariant** at Phase 9 (existing playbook §Phase 6):
   demote to `city_only` any pin whose lat/lon falls outside the state bbox.

### 5c. Registry rate limiting

DC GIS has been generous (no rate limit hit at 1000-record paginated pulls).
But state DCCA databases sometimes throttle. Use exponential backoff and
respect `Retry-After` headers. If the registry blocks scraping, fall back to
a manual one-time CSV download (ask data team or open data portal) rather
than scraping in production.

### 5d. Stale registries

Registries lag reality by 6–24 months. Newly-built condos won't appear; just
this year's new construction is missing. Document the registry's
"as-of" date in the retrospective. Plan to re-run name-list-first discovery
quarterly to pick up new registrations.

### 5e. Doc-less entities

Many registry entities won't have any public docs. The pipeline currently
requires at least one document for an HOA to appear on the live site. So the
fraction of registry entities that make it live is the **doc-discovery hit
rate** — for DC's CAMA pipeline empirically that's ~5–25% depending on how
aggressive the SERP filter is.

**This is fine and expected.** The bank manifest still records the entity
(name + registry metadata), and a future re-run with a relaxed filter, a
new SERP hit, or a manual upload will lift it onto the live site. Track:
- `bank_entity_count` (everything in the registry)
- `bank_with_docs_count` (manifests with ≥1 document)
- `live_entity_count` (post-prepare/import/Phase 10)

The conversion ratio `live / bank_entity_count` is the success metric.
For DC's reference run, target is ≥10% (≥330 of 3,289 condos live).

---

## 6. Reusable scripts reference

| Phase | Script | Purpose |
|---|---|---|
| 1 — Registry pull | (per-state, follow `dc_cama_pipeline.py` pattern) | Paginated ArcGIS REST pull or HTML scraper, writes seed JSONL |
| 2 — Name-binding discovery | `state_scrapers/_orchestrator/namelist_discover.py` | State-agnostic; takes seed JSONL, runs name-anchored Serper, banks with canonical name pinned |
| 3 — Prepare | `scripts/prepare_bank_for_ingest.py` | Unchanged (shared with keyword playbook) |
| 4 — Import | `POST /admin/ingest-ready-gcs?state=XX&limit=50` | Unchanged |
| 5 — Location enrichment | `state_scrapers/ri/scripts/enrich_ri_locations.py` | Unchanged; pulls from `/admin/extract-doc-zips` and ZIP-centroid backfills |
| 6 — Phase 10 close | `scripts/phase10_close.py` | Unchanged (LLM rename + null-canonical hard-delete + doc-filename audit) |
| Reference orchestrator | `state_scrapers/_orchestrator/run_overnight.py` | Adapt to add a Phase 1+2 pre-step before the standard runner |

The DC reference run produced its seed pipeline at:
[`state_scrapers/_orchestrator/dc_cama_pipeline.py`](../state_scrapers/_orchestrator/dc_cama_pipeline.py).
That file demonstrates: ArcGIS REST pagination, seed-JSONL emission, query
file generation. Copy and adapt for new states.

---

## 7. Per-state launch checklist

1. Confirm the state belongs in the suitability matrix (§0). If not, use
   the keyword-Serper playbook instead.
2. Spend ≤60 minutes finding the registry source (§2). If unsuccessful,
   fall back to keyword-Serper.
3. Write a one-off pull script (model on `dc_cama_pipeline.py`); produce
   `state_scrapers/{state}/leads/{state}_namelist_seed.jsonl`.
4. Run `namelist_discover.py --seed {seed} --state {STATE}
   --state-name {Full Name} --apply`. Wall time ~10–15s/entity at N=1,
   ~3s/entity at N=4.
5. Standard Phases 7–10 (prepare, import, enrich, Phase 10).
6. Verify `live / bank_entity_count` ≥ 10%; investigate if not. **If below
   10%, do not declare done — try a second discovery pass with widened
   query templates (statute-vocabulary variants, relaxed filename
   heuristic), mgmt-company harvest as a supplement, and/or page-1 OCR
   verification on the unmatched subset.** A run sitting at 5% with
   budget unused is a partial run, not a finished one.
7. **Pre-retro: complete `state_scrapers/{state}/notes/source-inventory.md`**
   listing every document-discovery source considered (Serper templates,
   mgmt-co portfolios, jurisdictional governing-doc vocabulary variants,
   second-pass with relaxed filters, page-1 OCR verification, …) —
   productive / sterile / untried-with-reason. The retrospective
   cross-references this file.
8. Write `state_scrapers/{state}/notes/retrospective.md` covering:
   - Registry source, "as-of" date, total entities pulled.
   - PDF discovery hit rate (entities with ≥1 banked PDF / total entities).
   - Phase 10 hard-delete rate (target < 10%).
   - **Budget-envelope utilization** per cost line (DocAI / Serper /
     OpenRouter). Any line < 70% utilized requires a one-sentence
     diminishing-returns justification.
   - **What didn't I try and why** — cross-referenced against
     `source-inventory.md`.
   - Final `live_entity_count`, map coverage, total cost.
   - What broke during the registry pull and how you fixed it.

---

## 8. Multi-pattern coexistence

A state can use **both** playbooks. Run order:
1. Keyword-Serper-per-county first (catches HOAs in suburban counties +
   any condo with a public website).
2. Name-list-first second, against a registry that covers urban condos
   (catches the dense urban stock the keyword sweep missed).

The bank dedups by slug — if both passes produce a manifest for "Park View
Condominium," they merge into one with documents from both passes attached.
Phase 10 cleans up resulting duplicates if name normalization differs
slightly between the two sources.

DC was the first state to use both: the original neighborhood-anchored
keyword-Serper sweep banked 63 manifests; the CAMA name-list pass added
102 more (after dedup). The combined bank fed a single Phase 7–10 run.

---

## 9. Doc status

- **First written:** 2026-05-08 (DC reference run completed earlier same
  day).
- **Canonical reference state:** DC (CAMA-Condo CONDO REGIME table,
  3,289 entities; first state where this pattern was deployed end-to-end).
- **Companion playbook:**
  [`docs/multi-state-ingestion-playbook.md`](./multi-state-ingestion-playbook.md)
  — read it for Phase 5 (OCR cross-validation), Phase 6 (geo enrichment
  beyond ZIP centroid), Phase 8 (import internals), Phase 10 (rename pass +
  hard-delete + bbox audit).
- **Future updates:** add a per-state suitability column to Appendix D
  of the keyword playbook so a new operator picking up a state knows
  which playbook to read.
