# Tier-1 Second-Pass Batch Retrospective — 2026-05-09

Second-pass cleanup + targeted supplemental sweeps over the 7 Tier-1 states whose first-pass live counts came in below ~80% of expected metro coverage: **NV, LA, IA, ID, KY, UT, AL**.

## Bottom-line per-state results

| State | Live (overnight) | Live (after second-pass) | Δ live | Cleanup deletes | Cities backfilled | Supp work |
|---|---|---|---|---|---|---|
| NV | 26 | 26 | +0 | −5 (Phase A) − 9 (NV supp) = −14 | 9 | Vegas master-planned-community keyword sweep (40 leads → 11 prepared → +5 net new live; +8 dedupe) |
| LA | 41 | 32 | −9 | −8 (4 null + 3 regex + 1 dedupe) | 13 | none |
| IA | 51 | 44 | −7 | −9 (6 null + 2 not-HOA + 1 foreign-state) | 27 | none |
| ID | 57 | 36 | −21 | −19 (Phase A: 5 null + 3 not-HOA + 1 foreign-state + 10 regex + 2 dedupe; supp: 1 not-HOA + 2 null) | ~12 | Blaine (Sun Valley) + Bonner (Sandpoint) supplementals: 69 leads → 117 prepared → +1 net live (Sun Valley Elkhorn Association) |
| KY | 53 | 40 | −13 | −14 (5 null + 1 not-HOA + 4 foreign-state + 4 dedupe) | 20 | none |
| UT | 83 | 70 | −13 | −13 (Phase A: 1 null + 1 foreign-state + 7 regex + 2 dedupe; supp: 1 foreign-state + 7 renames) | 12 (+22 pending Render-recovery retry) | Wasatch (Park City / Deer Valley) supplemental: 31 leads → 147 prepared → +0 net live (mostly mgmt-co-portal-locked) |
| AL | 96 | 86 | −10 | −7 (2 null + 1 foreign-state + 2 regex + 2 dedupe) | 13 |  none |
| **TOTAL** | **407** | **334** | **−73** | **~84 entries cleaned** | **~106 cities** | 4 supplemental discovery sweeps |

## What changed in this second-pass

### Code

1. **`scripts/phase10_close.py`** — added an **override-tier regex set** (`NON_HOA_NAME_PATTERNS_OVERRIDE`) that runs BEFORE the structural-suffix safelist. This catches doc-title fragments and recording stamps with HOA-shape suffix (e.g., `Recorder of Madison County … Condominium Association`, `Notary Public for State of … Condominium Association`, `OF PROTECTIVE FOR HERITAGE HOA`, `Of Condominium for Vivante HOA`) that the original `NON_HOA_NAME_PATTERNS` was prevented from flagging.
2. **`scripts/phase10_close.py`** — added `parse_not_hoa_ids`, `parse_foreign_state_ids`, and `parse_location_backfills` parsers + corresponding pipeline steps (`delete_not_hoa`, `delete_foreign_state`, location-backfill via `/admin/backfill-locations`). These read the new fields emitted by the LLM rename pass.
3. **`state_scrapers/ga/scripts/clean_dirty_hoa_names.py`** — overhauled the `is_dirty()` LLM rename pass:
   - Stricter SYSTEM prompt asking the LLM to validate `is_hoa: bool` (rejecting medical / professional societies, water utilities, court filings, plat extracts, OCR garbage even when entity name ends in "Association").
   - New JSON schema: `{is_hoa, canonical_name, city, state, county, confidence, reason}`.
   - `is_hoa=false` defaults to True for safety; only treated as False when LLM explicitly returns False.
   - Defensive `uncertainty` reason guard: if reason mentions "no document", "cannot verify", "unable to verify", `is_hoa` is forced back to True (catches Kimi/DeepSeek's canned uncertainty responses when the model ignored the supplied doc text).
   - `skip_delete` flag for entries with `< 500 chars` of doc text — these are preserved (not deleted), since absence of text isn't evidence of non-HOA-ness.
4. **`state_scrapers/ga/scripts/clean_dirty_hoa_names.py`** — added retry-on-5xx to `_fetch_summaries` (6 retries, exponential backoff), `_fetch_doc_text` (3 retries per endpoint), and `/admin/rename-hoa` apply (6 retries per chunk). This survives the routine 502 spikes from concurrent multi-state autonomous runs hitting Render together.

### Discovery + COUNTY_RUNS

5. **`state_scrapers/nv/queries/nv_vegas_communities_serper_queries.txt`** — 38-query master-planned-community + Henderson-neighborhood anchor file. Appended to NV's `COUNTY_RUNS` with `default_county="Clark"`.
6. **`state_scrapers/id/queries/id_blaine_serper_queries.txt`** — Sun Valley / Ketchum / Hailey anchored. Appended to ID's `COUNTY_RUNS`.
7. **`state_scrapers/id/queries/id_bonner_serper_queries.txt`** — Sandpoint / Schweitzer / Lake Pend Oreille anchored. Appended to ID's `COUNTY_RUNS`.
8. **`state_scrapers/ut/queries/ut_wasatch_serper_queries.txt`** — Heber City / Midway / Jordanelle / Deer Valley anchored. Appended to UT's `COUNTY_RUNS`.

## Cross-state lessons

1. **The original brief's premise — "first passes used 3-5 counties per state" — was wrong.** All 7 states already had 7-18 counties in their `COUNTY_RUNS` after the May 2026 overnight batch (`0e221b0` removed the lead-cap; `896eced` scaffolded all 9 states with substantial county breadth). The real bottleneck was **Phase 10 cleanup leak**, not discovery breadth. Bank-to-live retention was 14-25% across the batch; aggressive Phase 10 deleted 60-80% of bank manifests as junk. The leverage for live-count cleanup was tightening Phase 10's regex + adding OCR-LLM `is_hoa` validation, not running more county sweeps.

2. **OCR-LLM cross-state validation is the highest-value feature added.** 9 cross-state mis-attributions caught across the batch (NV: 2, KY: 4, IA: 1, ID: 1, AL: 1, UT: 1) — entities whose doc text plainly states a different state, banked under the wrong state due to keyword-Serper confusing similarly-named HOAs across state lines. KY is a hotspot (Cincinnati/OH spillover via Northern KY queries; Memphis/TN spillover via Paducah; Louisville/IN spillover). No regex pattern would catch these; only the doc body reveals them.

3. **OCR-LLM `is_hoa` validation catches the residual non-HOA "Association" class.** `Kentucky Dermatological Association, Ltd.` (medical society), `Iowa Parks and Recreation Association, Incorporated` (state agency), `Aspen Creek Water Association` (utility), and similar HOA-shape-suffixed but non-residential-community entities slipped past the structural safelist in the original Phase 10. The new `is_hoa=false` LLM signal + 0.85 confidence threshold catches them.

4. **The override-regex tier is the structural fix for OCR-fragment leaks with HOA-shape suffix.** ID was the noisiest first-pass state (~22% visible junk); the override tier deleted 10 junk entries from ID alone (`Squarespace HOA`, `Sr HOA`, `BOG!( ...`, `Notary Public for State of ...`, `Recorder of Madison County ...`, `Site Map Ownership Opportunities`, `HOAs by changing the ability of the association to fine homeowners`, `Homeowner Information and HOA`, `Condominium Property Act`, `Complica Tion`). UT next (7 catches), AL (2), LA (3). This is by far the highest-density-of-junk subset of the second-pass cleanup.

5. **Render 502s under concurrent multi-state load require defensive retries.** The May 2026 second-pass had ≥6 autonomous sessions running concurrently against the same Render service; routine 5-10% 502 rate on any given request. Without retries, `_fetch_summaries` would crash the rename pass and Phase 10 would silently report `all-no_candidates`. The retry patches added in this pass make a state run resilient to transient blips.

6. **Keyword-Serper has a low ceiling for paywalled-doc states.**
   - **NV (Las Vegas)**: 38-query Vegas-anchored sweep yielded ~5 net-new live HOAs. Vegas is dominated by FirstService Residential Nevada / Terra West / Associa portals which Google doesn't index. Expected.
   - **ID Bonner (Sandpoint)**: ~0 net-new live; too rural.
   - **ID Blaine (Sun Valley)**: ~1 net-new live; ski-resort condos mostly mgmt-co-portal-locked.
   - **UT Wasatch (Park City/Deer Valley)**: ~0 net-new live (mostly Stockton-town and Wasatch-county ordinances surfacing, not community HOAs).
   These three confirmed the playbook's section §0 "When the keyword-Serper playbook fails" matrix. Future leverage in these areas is name-list-first via state CIC/condo registries (NV NRED, UT DOPL, ID Real Estate Commission), but the registries aren't publicly bulk-downloadable. Open follow-up.

7. **Phase B (NV name-list-first via NRED CIC registry) was deferred because the registry isn't operationally accessible.** NRED's public site at `red.nv.gov/Content/CIC/Registration/` provides forms only — no search/download tool. NV SoS Silverflume (`esos.nv.gov/EntitySearch/OnlineEntitySearch`) is bot-protected (returns captcha to non-browser clients). Substituted with the Vegas keyword-Serper supplemental documented above. A future Phase B for NV would need either a back-channel CSV from NRED's data team OR a browser-automated SoS scrape (Playwright); both are too high-bar for an autonomous overnight pass.

## Costs (all approximate)

- DocAI: ~$2-5 per state for the supplemental imports (NV $1, ID $4, UT $3); rest ran at $0 (re-used existing prepared bundles).
- Serper: ~$0.04 per supplemental county sweep; total ~$0.30.
- OpenRouter (DeepSeek-flash + Kimi-k2.6 fallback): ~$0.001/entity × ~407 entities × 2 passes × 7 states ≈ $0.50 total for OCR-LLM validation.
- **Total batch cost: ~$10-15 across all 7 states.**

## Outstanding follow-ups

1. **UT Wasatch location backfill retry**: 22 city records queued in `state_scrapers/ut/results/ut_supp_20260509_043244_claude/location_backfill_records.json`; pending a clean Render moment for the POST. Scripted retry running in background (`/tmp/apply_ut_locations.py`).

2. **NV Phase B proper (name-list-first)**: NRED CIC registry data-team request OR a Playwright-based NV SoS Silverflume scraper, fed into `state_scrapers/_orchestrator/namelist_discover.py`. Estimated yield: 200-800 net-new live NV entries (vs. current 26).

3. **Future override-regex additions** (patterns suggested by this batch's residuals — not yet added):
   - `^The-[A-Z][a-z]+-` (filename-leak slug — caught `The-Seven-at-Fox-Run-Landing-` in IA but only ~50% confident at protecting real HOAs from `The-something-something-` legitimate names). Borderline.
   - `\bRouzan Residential$` or `\bResidential$` post-suffix-strip (LA had `Rouzan Residential` survive — too narrow to generalize).
   - Patterns for `Th Restrictions` already in override; consider expanding `^Th\s+` to a few more nouns.

4. **Bonner ID** is a "keyword-Serper-doesn't-work" county. Should be removed from the next round's COUNTY_RUNS unless the methodology changes.
