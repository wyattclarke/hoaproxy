# VA HOA Scrape Retrospective

## Final state (2026-05-09)

- **Live HOA count:** 197 (post-Phase 10)
- **Bank manifests (post-discovery):** 567
- **Prepared bundles:** 224 (39% retention from bank)
- **Imported:** 208
- **Phase 10 cleanup deletes:** 104 (56 null + 42 not-HOA + 6 foreign-state) + 6 dedupe-merges
- **Cities backfilled via OCR-LLM:** 82
- **Map coverage:** ~83%
- **Out-of-bbox flagged:** 0

## Run history

### Phase A — pre-existing cleanup (`va_20260509_142139_cleanup_existing`)
Initial state was 84 live VA entries with substantial cross-state contamination — bank prefixes showed `v1/VA/bartow/` (GA), `v1/VA/broward/` (FL), `v1/VA/escambia/` (FL), `v1/VA/fulton/` (GA), `v1/VA/okaloosa/` (FL), etc. Only Fairfax was a real VA county prefix. Likely artifact of an early ingestion bug that mis-tagged state.

OCR-LLM rename pass (`clean_dirty_hoa_names.py --no-dirty-filter`) classified all 84:
- 60 real VA HOAs (state-correct)
- 21 NULL state (HOA-shaped names, no clear state in OCR — kept as VA based on bank prefix)
- 3 cross-state mis-assignments (real HOAs in wrong state)
- 4 voluntary civic associations (not mandatory HOAs — out of HOAproxy scope)

Applied:
- **38 renames + 1 dedupe-merge** for VA-state real HOAs
- **4 hard-deletes** for civic associations: Country Club Hills (Fairfax), Del Ray (Alexandria), Kings Park West (Fairfax), Stratford Landing (Mt Vernon) — all explicitly civic/voluntary, not mandatory HOAs
- **3 cross-state reassignments** via `/admin/backfill-locations` + `/admin/rename-hoa`:
  - hoa_id 23 "Ballentine – Fuquay Varina" → renamed to "Ballentine Farms HOA", state→NC
  - hoa_id 12578 "Seneca Ridge Homeowners Association" → state→PA, city→York
  - hoa_id 12580 "Woodlake Community Association" → renamed+merged into existing NC "Woodlake Homeowners Association" (target_id 3232)

Result: 84 → 76 live (+ 3 entries moved to other states, 4 junk deleted, 1 same-state merge).

### Phase A — fresh discovery (`va_20260509_142337_phaseA`)
End-to-end Serper sweep on 15 metros + state-wide host_family — Fairfax (NoVA), Virginia Beach, Prince William, Loudoun, Henrico, Chesterfield, Norfolk, Chesapeake, Arlington, Stafford, Spotsylvania, James City (Williamsburg), Hampton, Newport News, Albemarle (Charlottesville). Wall time: ~1.2 hours.

Per-county bank manifest yield:

| County / City | Manifests | Notes |
|---|---|---|
| Fairfax | 66 | NoVA gold mine; Reston/Vienna/McLean/Burke/Centreville/Annandale anchors. |
| Prince William | 48 | Manassas/Woodbridge/Gainesville/Haymarket/Bristow/Lake Ridge. |
| Loudoun | 47 | Ashburn/Leesburg/Brambleton/Broadlands/South Riding/Lansdowne. |
| James City | 41 | **Williamsburg/Kingsmill/Ford's Colony/Governor's Land** — strong resort yield. |
| Arlington | 28 | Mostly condos: Rosslyn/Crystal City/Ballston/Pentagon City. |
| Henrico | 27 | Glen Allen/Short Pump (Richmond suburbs). |
| Virginia Beach | 25 | Sandbridge/Princess Anne/Kempsville. |
| Chesterfield | 24 | Brandermill/Woodlake/Midlothian (Richmond suburbs). |
| Albemarle | 20 | Charlottesville/Crozet/Forest Lakes/Glenmore. |
| Stafford | 20 | Aquia/Embrey Mill (Fredericksburg suburbs). |
| Spotsylvania | 20 | Fawn Lake/Lake Anna. |
| Norfolk | 16 | Hampton Roads (mostly condos: Ghent). |
| Hampton | 15 | Hampton Roads. |
| Chesapeake | 6 | Hampton Roads — thin yield. |
| Newport News | 4 | Hampton Roads — thinnest yield (Port Warwick/Hilton Village). |

`host_family` queries (FirstService/Cardinal/Associa/Sentry/Sequoia/CMC/Armstrong/Goodman + statute-anchored §55.1-1800/§55.1-1900 + Wintergreen/Massanutten/Smith Mountain Lake) added the rest.

**Total bank: 567 manifests** (28 → 567, +539 net new).

### Phase B — prepare + import + locations (`va_20260509_155117_phaseB`)
- **Prepared:** 224 bundles (39% retention from 567 bank — typical for Tier-2 when many bank candidates are court filings, civic news, planning packets caught at prepare-time `is_dirty()` gate or page-1 OCR review).
- **Imported:** 208 (16 prepare→import gap from collisions / failed bundles).
- **Live (post-import):** 290 (76 → 290, +214 imports).
- **Wall time:** prepare ~30 min + import ~5 min + locations ~2 min = ~40 min.

### Phase 10 cleanup
- **Renames applied:** 97 + **27 dedupe-merges** (124 proposed of 290 scanned, 43% had cleanup proposals; 166 skipped due to empty doc text or LLM uncertainty guard).
- **Hard-deleted:**
  - `delete_null` (LLM canonical=null): **56**
  - `delete_not_hoa` (OCR-LLM `is_hoa=false`): **42** — primarily civic associations not caught at bank/prepare stage, plat extracts, county planning docs.
  - `delete_foreign_state`: **6** — additional cross-state spillovers caught only after import (the OCR-LLM doc-body check is essential for VA since NoVA borders MD/DC, Hampton Roads borders NC, Charlottesville/Albemarle has WV proximity).
  - `delete_regex` (override regex): 0 — same as WA.
  - `delete_audit` (doc-filename audit): 0.
- **Dedupe-merge (post-rename pair detection):** 6 (e.g., punctuation-only differences in HOA suffix).
- **Location backfill:** 82 city-only matches via OCR-LLM (Fairfax, Reston, Arlington, Virginia Beach, Woodbridge, Ashburn, Charlottesville, Williamsburg, Norfolk, Richmond, Chesterfield, etc.).
- **Bbox audit:** 0 out-of-bbox (clean).

Result: 290 → 197 live with 82 cities populated.

## Discovery techniques attempted

- ✅ **Per-county Serper** with `Clerk of Circuit Court` recorder anchor (VA county recorders are Clerks of the Circuit Court), `Declaration of Covenants`, `Master Deed`, etc.
- ✅ **Sub-county neighborhood anchors** — essential for NoVA (Reston/Vienna/McLean/Centreville/Burke), Loudoun (Ashburn/Brambleton/Broadlands), Williamsburg/James City (Kingsmill/Ford's Colony).
- ✅ **Statute-anchored host_family queries:** §55.1-1800 (POA Act), §55.1-1900 (Condo Act), VA Common Interest Community Board.
- ✅ **Mgmt-co host_family:** FirstService Residential, Cardinal Management Group, Associa Mid-Atlantic, Sentry Management, Sequoia, CMC, Armstrong, Goodman, Legum & Norman.
- ✅ **Resort-anchored:** Wintergreen, Massanutten, Smith Mountain Lake, Lake Monticello.
- ❌ **Name-list-first via VA CICB registry** — not attempted in this run. Per the name-list-first playbook §2b, VA Common Interest Community Board (`dpor.virginia.gov`) maintains a public registry of all common-interest communities (statutorily required). This would be a productive Phase C supplemental — see "Open follow-ups."

## Lessons learned

1. **VA had heavy pre-existing cross-state contamination.** 84 baseline live entries; bank prefixes showed mostly NON-VA county slugs (Bartow/Broward/Escambia/Fulton/Okaloosa). The 3 entries with state≠VA in OCR-LLM detection (Ballentine NC, Seneca Ridge PA, Woodlake NC) were successfully reassigned to their correct states via `/admin/backfill-locations` + `/admin/rename-hoa`. The pattern (rename to canonical, then update state via backfill-locations) is reusable for any future cross-state cleanup. Document: rename first (so backfill name-match works), then update state.

2. **"Civic association" is a distinct false-positive class for VA.** 4 of 84 baseline entries were Country Club Hills / Del Ray / Kings Park West / Stratford Landing — voluntary civic/citizens associations, not mandatory HOAs. The OCR-LLM `is_hoa=False` correctly flagged all 4. These don't share a regex pattern with other states' false-positives (which are typically court filings or government boilerplate); they're a VA-specific shape because NoVA has many active citizens-association civic groups around historical neighborhoods.

3. **NoVA + Williamsburg + Richmond suburbs are the strongest yield zones.** Fairfax 66 + Prince William 48 + Loudoun 47 + James City 41 alone = 202 manifests (36% of total). Hampton Roads (Norfolk + Chesapeake + Newport News + Hampton + VB = 66) underperformed — paywalled-urban condos in Norfolk + Hampton Roads suburb HOAs are mostly behind FirstService/Associa portals.

4. **Bank prep retention rate of 39% is below WA's 47% but typical for Tier-2.** Many NoVA-area Serper hits are condo plat exhibits, court filings (`SAHA-2021-1.pdf`-style filenames in Fairfax recordings), and county planning packets. The `is_dirty()` gate + page-1 OCR review filter caught most before full-doc OCR.

5. **OCR-LLM caught 6 additional cross-state mis-attributions** beyond the 3 caught in pre-existing cleanup. VA Tier-2 has cross-state contamination broader than expected because:
   - NoVA borders MD/DC closely (mgmt cos serve all three)
   - Hampton Roads borders NC (FirstService Mid-Atlantic spans both)
   - Charlottesville/Albemarle gets some WV spillover via mgmt cos
   - Williamsburg/James City has resort-condo overlap with NC OBX

6. **Override-regex tier (0 deletes) — same as WA.** VA's noise shape is again different from prior batch states: more court filings (`Case 2:14-cv-00xxx`), municipal planning packets, civic association bylaws. Future override regex additions specific to VA could include patterns matching civic-association names that survive rename, but the OCR-LLM `is_hoa` check is already catching them at a lower per-entity cost.

7. **VA Tier-2 ran in single autonomous overnight session at low cost** — same architectural advantage as WA. The full pipeline (existing-cleanup + new-discovery + prepare + import + locations + Phase 10) ran end-to-end in ~4 hours wall time at ~$11 DocAI + ~$1 OpenRouter + ~$0.50 Serper = **~$12.50 total**. Well under the playbook's Tier-2 budget envelope ($40–75).

## Costs

- **DocAI:** ~$11.15 (delta from $169.88 → $181.03)
- **Serper:** ~$0.50 (~360 queries)
- **OpenRouter** (DeepSeek-flash + Kimi-k2.6 fallback): ~$0.80 (374 LLM passes)
- **Total:** ~$12.45

## Open follow-ups

1. **VA CICB registry name-list-first pass.** `dpor.virginia.gov/CommonInterestCommunityBoard` maintains a public registry of all VA common-interest communities — by statute, every CIC in VA must register. This would unlock paywalled-urban condo stock in Hampton Roads + Arlington + Alexandria + DC-suburb portions of Fairfax/Loudoun. Estimated yield: 1,500–4,000 net-new live VA HOAs (vs. 197 current). High-value Phase C target.

2. **Sub-county neighborhood expansion** could add: Tysons/Falls Church/Annandale (Fairfax sub-anchors), I-95 Fredericksburg suburbs (Stafford-area corridor), Albemarle Pantops + Lake Monticello.

3. **No civic-association regex needed for now** — OCR-LLM `is_hoa=False` caught the 4 baseline + ~5 from Phase 10 deletes. If a future state has civic-heavy noise, an override pattern matching "Civic Association of {city}" or "Citizens Association" could be added.

4. **3 cross-state moves validated a reusable reassignment pattern**: rename hoa_id to canonical → POST `/admin/backfill-locations` with new state (or merge into existing same-name target). The dedupe-merge path (Woodlake → existing NC target) is the cleanest — docs/chunks/locations all carry over with target's location fields kept.

## Files

- Existing-cleanup ledger: `state_scrapers/va/results/va_20260509_142139_cleanup_existing/name_cleanup_unconditional.jsonl`
- Phase A discovery logs: `state_scrapers/va/results/va_20260509_142337_phaseA/discover_*.log`
- Phase B prepare ledger: `state_scrapers/va/results/va_20260509_155117_phaseB/prepared_ingest_ledger.jsonl`
- Phase B Phase 10 ledger: `state_scrapers/va/results/va_20260509_155117_phaseB/name_cleanup_unconditional.jsonl`
- Phase 10 log: `state_scrapers/va/results/va_20260509_155117_phaseB/phase10.log`
- Final state report: `state_scrapers/va/results/va_20260509_155117_phaseB/final_state_report.json`
- Per-county query files: `state_scrapers/va/queries/va_*_serper_queries.txt` (15 counties + host_family)
