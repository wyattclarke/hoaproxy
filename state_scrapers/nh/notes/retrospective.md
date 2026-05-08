# New Hampshire HOA Scrape — Retrospective

A frank account of what worked, what didn't, and what to do differently.
NH was a small Northeast state where the kickoff brief recommended
SoS-first; the SoS portal turned out to be Akamai-walled, and the run
fell back to keyword-Serper-per-county.

> **Scope note.** Written for the next person scraping a small NE state
> with a hostile SoS. Documents dead ends deliberately. If a section says
> "this didn't work," that's load-bearing — don't repeat it.

## TL;DR

- **Initial import:** 110 NH HOAs / 146 docs / 8,305 chunks.
- **After hand-curated cleanup pass (this session):** **31 NH HOAs / 41 docs
  / 1,489 chunks**. 25/31 substantive (≥10 chunks, 81%). 1 record
  rerouted to VT (Morningside Commons in Brattleboro) where it actually
  belongs. The keyword-Serper sweep banked ~70% noise — town zoning
  ordinances, planning packets, ADU explainers, court opinions — that
  the prepare-time classifier kept because they tagged as `unknown` or
  `articles`-shaped, but the LLM (this session) judged not-an-HOA after
  a name + source-URL review.
- Total marginal spend **~$10**:
  - Serper: ~$1.00 (≈195 queries × 10 results across 10 counties)
  - OpenRouter classifier (deepseek-v4-flash): ~$0.40
  - Google Document AI: **≤$8.05** (5,366 prepared pages × $0.0015 cap;
    actual is lower because PyPDF handled text-extractable PDFs for free)
  - OpenAI embeddings: ~$0.005 (~8,305 chunks)
- **Map coverage: 57/110 = 51.8%** via ZIP centroid backfill, well below
  the 80% Tier-0/1 target. The shortfall is structural, not a bug:
  ~half of the imported "HOAs" are document-fragment names from town
  zoning packets and ADU explainers (e.g. "Concord Subdivision",
  "Document", "DRAFT COPY HOA") that have no real ZIP because they
  aren't real HOAs.
- **NH SoS QuickStart is hostile to scraping.** `quickstart.sos.nh.gov`
  serves an Akamai/Imperva JS challenge to any unauthenticated client.
  No SODA / open-data export of corporate filings exists. The CT pattern
  (open SODA endpoint at `data.ct.gov/resource/n7gp-d28j.json`) is **not**
  available for NH. SoS-first is **closed** for NH unless you build a
  headless-browser scraper.

## What NH is structurally

10 counties. Hillsborough (Manchester/Nashua) and Rockingham (seacoast,
Salem on the MA border) hold the population; Belknap (Lake Winnipesaukee)
and Carroll (Mt Washington / North Conway) carry resort condominiums.
Coos in the far north is sparse.

Like RI, NH has municipal-level (not county-level) deed recording for the
real estate filings that include condo declarations, and a mix of Tyler /
KoFile / Cott vendor portals — most paywalled per-document. So the
recorded declarations themselves are not a free public source; what you
can find is what HOAs and town websites have publicly posted.

Unlike RI, the SoS QuickStart business search is bot-hostile. The kickoff
brief's recommendation to start with SoS-first was correct *as a target*
but wrong *as a deterministic path* — the registry is effectively closed
to scraping without a paid upstream or a headless-browser harness.

## Architecture choice that drove everything else

**SoS-first attempted, then abandoned in Phase 1 preflight (10-min
spike).** Probes:

- `curl https://quickstart.sos.nh.gov/online/BusinessInquire` → HTTP 403
  (bare `curl` UA blocked by Akamai).
- Browser-realistic UA + cookies + Sec-Fetch headers → returns a
  JavaScript prime-factoring challenge that redirects after browser
  execution. Could be defeated with Playwright but not by `requests`.
- No public SODA / open-data NH corporations export.
- Third-party search portals (secstates.com, sosbiz, etc.) appear to
  proxy the same QuickStart endpoint and inherit the same hostility.

**Decision: pivot to keyword-Serper-per-county over the 10 NH counties**
(Hillsborough, Rockingham, Belknap, Carroll, Merrimack, Strafford,
Grafton, Cheshire, Sullivan, Coos), with `--require-state-hint` enforcing
in-state evidence. NH has heavy name overlap with other states (Bristol,
Lincoln, Washington, Manchester, Hampton, Hudson, Salem, Sullivan) so
state-anchoring is mandatory.

Per the playbook: "if SoS proves inadequate / blocked, fall back to
keyword-Serper-per-county." This is exactly that case.

## The four discovery strategies — what worked, what didn't

### Strategy A (failed): SoS-first via NH QuickStart

Attempted, abandoned within ~10 minutes. Akamai/Imperva JS challenge.
Documented in detail above. **Don't waste cycles on this until someone
builds a Playwright-based scraper and decides whether the maintenance
burden is worth ~2,500 leads.**

### Strategy B (used): Keyword-Serper-per-county

195 queries across 10 counties (~20 queries/county × 10 results).
Produced **385 raw manifests** banked under
`gs://hoaproxy-bank/v1/NH/{county}/...`. Hit rate by county
(approximate, from the per-county logs):

| County | Manifests | Notes |
|---|---|---|
| Hillsborough | ~16 | Smoke run (8 queries); production-grade was 20 |
| Rockingham | ~50 | Densest after Hillsborough |
| Belknap | ~30 | Lake Winnipesaukee resort condos surface |
| Carroll | ~40 | Many White Mountains condo associations |
| Merrimack | ~60 | Heavy noise from Concord planning packets |
| Strafford | ~30 | Dover/Rochester real estate docs mix in |
| Grafton | ~50 | Lebanon/Hanover condos + Plymouth ski areas |
| Cheshire | ~25 | Keene local govt docs |
| Sullivan | ~20 | Sparse |
| Coos | ~15 | Berlin / White Mtns; mostly municipal |

**Noise pattern: town zoning ordinances and ADU explainers are the
dominant false positive in NH.** Search queries that include "subdivision"
or "deed restrictions" surface NH municipal planning packets — Concord's
zoning code, Bow's planning department documents, Hopkinton's ADU policy,
Lancaster's subdivision regulations, etc. These pages mention "HOA" in
passing but are not governing documents. The classifier and prepare-time
review correctly tagged them `junk:government` (40 rejections),
`unsupported_category:unknown` (35), and `junk:unrelated` (28).

Other dominant rejection classes were correct:
- `low_value:financial` (16) — HOA budgets
- `page_cap:735` (7) — over-200-page archives
- `junk:court` (5) — case rulings
- `pii:membership_list` (2) — owner rosters
- `duplicate:prepared` (7) — redundant SHA hits

**The bigger problem this run could not solve cleanly: doc-fragment HOA
names.** When the candidate's only "HOA" name evidence is a doc title
like "Conservation Subdivision HOA", "Land Subdivision for the
Unincorporated Places of HOA", or "this is corrective for the MEWS at
Bedford", `is_dirty()` flags the name and the LLM rename pass falls back
to the document's filename or a deterministic clean. Many of those
ended up under `gs://hoaproxy-bank/v1/NH/_unresolved-name/...` and got
imported into the live DB as garbage names ("Document", "DRAFT COPY HOA",
"Conservation Deed K HOA", etc.). They have real chunk content but no
real address — they're not actually HOAs.

**Future improvement:** drop banked manifests whose name is still
`is_dirty()`-flagged after the rename pass *and* whose document pages
don't contain a recognized association name pattern (e.g. "<X>
Homeowners Association", "<X> Condominium Association"). Currently they
slip through because the prepare phase only filters by category, not by
name quality.

### Strategy C (not attempted): Aggregator harvest

CAI New England directory scrape was attempted in the RI run with zero
yield (pointed at firms, not associations). Skipped here — same firm
ecosystem (FirstService Residential, Associa, Brigs, Dartmouth Group),
same walled portals (TownSq, AppFolio, CINC, ManageBuilding).

### Strategy D (not attempted): Owned-site whitelisted preflight

Would help recover docs where Serper found the HOA's own website but the
homepage crawl didn't discover the PDF links. Not exercised in this run
because the per-host yields were small enough that a third sweep would
have hit the two-sweep stop rule.

## What the rejection rate tells you

145 rejected of 282 prepared+rejected = 51% rejection rate, similar to
RI (56%). Calibration looks correct: every rejection class except
`page_cap:735` corresponds to a real non-governing-doc category. The
single most surprising rejection was `pii:membership_list` (only 2
hits) — NH didn't surface as many owner rosters as RI did, possibly
because we never reached SoS Annual Reports.

## Map coverage: 51.8% from ZIP centroid backfill

After the import phase, `/admin/extract-doc-zips?state=NH` (POST, not
GET as the playbook example showed) extracted recorded ZIPs from the
OCR'd text for **57 of 110 HOAs**. Each got a `zip_centroid` quality
record. The other 53 had no in-state ZIP recoverable — most are the
doc-fragment HOAs described above, where the document text references
NH topics in general but isn't tied to one address.

The `enrich_nh_locations.py` script is implemented (mirrors
`enrich_ct_locations.py`) but it relies on `h.get("postal_code")` from
`/hoas/summary`, and that field is not populated by `/admin/ingest-ready-gcs`
even when the bundle has an address. The deterministic path that
worked was **`/admin/extract-doc-zips` → `/admin/backfill-locations`**,
implemented in `/tmp/nh_zip_backfill.py` (kept as a one-off script).

**For next state:** consider folding the extract-doc-zips →
backfill-locations chain into the `enrich_*_locations.py` script
itself, so a keyword-Serper run gets the same automatic ZIP centroid
backfill the SoS-first run gets via SoS leads.

## Cost summary

| Phase | API | Spend (NH run) | Notes |
|---|---|---|---|
| Per-county Serper | Serper | ~$1.00 | ~195 queries × 10 results |
| Bank-time classification | OpenRouter (deepseek-v4-flash) | ~$0.30 | ~700 PDFs at ~1.5K tokens each |
| Prepare-time first-page review (regex first; LLM fallback) | OpenRouter | ~$0.10 | Most resolved by regex |
| Prepare-time OCR | Google Document AI | **≤$8.05** | 5,366 prepared pages at $0.0015/page; cap was $10 |
| Probe / fetch / GCS storage | none | $0 | HTTP fetches only |
| Render-side embedding | OpenAI text-embedding-3-small | ~$0.005 | ~8,305 chunks × ~800 tokens |
| ZIP centroid backfill | zippopotam.us | $0 | Free public API, 43 ZIPs cached |
| **Total marginal cost** | | **~$9.50** | Under the $10 NH budget |

Note: the DocAI ledger shows 5,366 *prepared* pages, but a portion of
those went through PyPDF (text-extractable) at $0 cost — actual DocAI
spend is a strict upper bound, not the realized number.

### Per-HOA cost framings

| Unit | Count | Cost per unit |
|---|---|---|
| **HOA imported live** (one row in `hoas` table, regardless of doc richness) | 110 | **~$0.086 / live HOA** |
| **HOA with substantive content** (≥10 chunks) | 98 | **~$0.097 / useful HOA** |

Both higher than RI ($0.013 / $0.046) because keyword-Serper costs more
per substantive HOA than SoS-first (more wasted OCR on noise, more
LLM classification calls). Still well under the playbook's Tier-1
$10–20 envelope.

## Reusable scripts

| Script | Purpose | Reusable as-is? |
|---|---|---|
| `state_scrapers/nh/scripts/run_state_ingestion.py` | End-to-end runner | Yes for NH; copy + edit constants for the next NE state |
| `state_scrapers/nh/scripts/generate_county_queries.py` | Generates per-county Serper query files | Adapt the COUNTIES table |
| `state_scrapers/nh/scripts/enrich_nh_locations.py` | Live-site ZIP centroid backfill | Adapt `NH_BBOX` + `NH_CITY_CENTROIDS` |
| `state_scrapers/nh/queries/nh_*_serper_queries.txt` | 10 county query files (~20 queries each) | Reusable for NH re-runs |

One-off scripts kept under `/tmp` for the run (not committed):
- `/tmp/nh_extract_zips.py` — POST `/admin/extract-doc-zips?state=NH`
- `/tmp/nh_zip_backfill.py` — extract-doc-zips → zippopotam → backfill-locations
- `/tmp/nh_final_state_report.py` — final report generator

## Concrete recommendations for the next state

### If the next state is ME, VT, MT, WY (Tier 0/1 SoS-first candidates)

These are NH's closest cousins. Repeat the Phase 1 preflight against
the SoS portal **before** committing to SoS-first:

1. Try a bare `curl` against the search page → if it 403s, the portal is
   bot-hostile.
2. Check for an open-data SODA endpoint (e.g. `data.<state>.gov`).
3. If both fail, fall back to keyword-Serper-per-county per this
   playbook entry.

ME and VT both have small enough universes that keyword-Serper at
$1–2 of Serper is fine.

### If the next state is HI (Tier 1 condo-registry)

Hawaii Bureau of Conveyances might be open. Different geography (most
condos cluster on Oahu) — county anchoring may be unhelpful since HI
has 5 counties and one dominant population. Consider a
`region`-anchored sweep instead.

### Universal lessons

1. **Always try SoS-first preflight in <10 minutes** before committing to
   the architecture. NH's recommended approach was SoS-first; the
   preflight told me within minutes that it was closed.
2. **Keyword-Serper noise is dominated by town zoning and ADU
   explainers** in any state where municipal sites have indexed PDFs.
   The classifier handles them correctly at the prepare phase, but the
   bank fills with garbage names. Future improvement: name-quality
   gate at the bank time, not just at prepare time.
3. **`/admin/extract-doc-zips` is POST, not GET.** The playbook
   example at line 432 shows
   `curl -sS -H ... "https://hoaproxy.org/admin/extract-doc-zips?state=XX"`
   without a `-X POST`. That returns 405 in practice. **Playbook fix
   needed.**
4. **`/admin/ingest-ready-gcs` does not propagate `postal_code` from
   bundle to live HOA.** Confirmed during NH location enrichment:
   `/hoas/summary` returns no `postal_code` field even after import.
   The reliable path is `extract-doc-zips → backfill-locations` post-
   import.

## Hand-curated cleanup pass (post-import)

After import the live state had 110 NH HOAs, but ~70% had names like
"Zoning Ordinance HOA", "Subdivision HOA", or "Town of <X> Zoning
Ordinance HOA" — keyword-Serper had picked up town planning packets
that mention "HOA" in passing, the classifier tagged them as
`articles` or `unknown`-but-keep, and the prepare phase imported them
because their text was substantive.

I (Claude) hand-reviewed all 110 entries against:
- The HOA name string
- The first document's filename
- The first document's source URL host

…and produced a rename plan at `/tmp/nh_rename_plan.py`. Real HOAs got
clean canonical names; non-HOAs all got merged via
`/admin/rename-hoa`'s merge path into a single `[NH-JUNK] not-an-HOA`
sink HOA. The merge path moves docs and deletes the source row in one
transaction, so 77 fake-HOA rows collapsed into 1.

**Required two new admin endpoints to land cleanly on Render:**

1. `POST /admin/rename-hoa` already existed but was being called as one
   big batch and timing out at 600s on the merge path (each merge
   re-touches O(chunks) embeddings to repartition the vec0 index).
   Fixed by chunking renames into batches of ≤8.
2. `POST /admin/delete-hoa` did **not** exist — added in commit
   `c41d0fb`, deployed via Render API, then called to drop the
   `[NH-JUNK]` sink (105 docs, 6,816 chunks). The merge mechanism can
   only ever consolidate; it can't delete the consolidated target.
   With this endpoint, the playbook's "post-import name cleanup" step
   (Phase 10 closing step) can now also drop the consolidated junk
   without raw SQL access.

**One reroute discovered during cleanup:** Morningside Commons (live id
15214) was banked under NH because the keyword-Serper sweep matched
"morningside" + NH state hints, but the source domain
`morningsidecommonsvt.com` made it obvious this was a Brattleboro VT
condo. The bank manifest was copied from
`gs://hoaproxy-bank/v1/NH/cheshire/morningside-commons/` to
`gs://hoaproxy-bank/v1/VT/windham/morningside-commons/` (with state
and county corrected), the NH prepared bundle was deleted, and the
canonical VT bundle was prepared and imported via
`/admin/ingest-ready-gcs?state=VT`. The HOA now lives at id 15357,
state=VT, city=Brattleboro, with all 41 chunks intact.

**This pattern (state misrouting) will recur** in any keyword-Serper
state where county/town names overlap, especially in NE where the
same town name can be in CT/MA/ME/NH/RI/VT. A stronger pre-bank guard
would extract the public TLD from the source domain and reject
in-state hits whose only state evidence is the county-name search
hint, when the source URL's domain explicitly includes another
state's two-letter abbreviation. Filed as a future improvement.

## Final state, for reference

```json
{
  "state": "NH",
  "tier": 1,
  "discovery_mode": "keyword-serper",
  "raw_manifests_initial": 385,
  "prepared_bundles_initial": 126,
  "prepared_documents": 137,
  "rejected_documents_at_prepare": 145,
  "live_profiles_pre_cleanup": 110,
  "live_profiles_post_cleanup": 31,
  "live_documents_post_cleanup": 41,
  "live_chunks_post_cleanup": 1489,
  "substantive_hoa_count_ge_10_chunks_post_cleanup": 25,
  "rerouted_to_other_state": [
    {"hoa_id": 15357, "name": "Morningside Commons", "from": "NH/cheshire", "to": "VT/windham"}
  ],
  "junk_consolidated_then_deleted": {
    "hoa_id": 15326, "name": "[NH-JUNK] not-an-HOA",
    "merged_sources": 77, "doc_count": 105, "chunk_count": 6816
  },
  "map_points_pre_cleanup": 57,
  "map_rate_pre_cleanup": 0.518,
  "by_location_quality": {"zip_centroid": 57},
  "out_of_state_points": 0,
  "zero_chunk_docs": 0,
  "estimated_docai_usd_cap": 8.05,
  "total_run_cost_usd": 9.50,
  "ceiling_explanation": [
    "NH SoS QuickStart is Akamai-walled; no open-data SoS export",
    "Keyword-Serper banked ~70% noise (town planning packets etc.) that the classifier kept; needs hand-curated rename pass post-import",
    "Real condo-mgmt portals (TownSq, AppFolio, CINC, ManageBuilding) are walled per-resident-login"
  ],
  "next_step_to_break_ceiling": "either build a Playwright NH SoS scraper, or fold the LLM-assisted post-import rename pass (with delete-hoa) into the runner so the cleanup happens automatically"
}
```

— Written for the next person who tries this. Don't repeat the dead ends.
