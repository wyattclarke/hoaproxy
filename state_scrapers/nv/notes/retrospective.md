# NV HOA Scrape Retrospective

## Final state (post second-pass, 2026-05-09)

- **Live HOA count:** 26
- **Bank manifests:** ~179 (post Vegas-communities supplemental discovery)
- **Cities backfilled via OCR-LLM:** ~9 (Reno-area + Vegas)

## Run history

### Overnight (`nv_20260508_130113_overnight`, 2026-05-08)
End-to-end run from 6 county-anchored Serper sweeps (Clark, Washoe, Carson City, Lyon, Douglas, Elko) + state-wide host-family sweep at unlimited leads-per-county. Banked 142 manifests → prepared 43 → imported 43 → Phase 10 cleaned to 26 live. Most live entries were Reno/Sparks-area (ArrowCreek, Caughlin Ranch, Camden at Virginia Lake Crossing, Carriage House) with thin Las Vegas representation aside from the Summerlin master associations.

### Second-pass Phase A (re-Phase10 with tighter regex + OCR-validation, 2026-05-09)
Re-ran `phase10_close.py` against the same overnight run_id with the May 2026 second-pass code:
- New cumulative-regex sweep (added in 5261c0a, post-overnight) caught nothing new on NV (NV's first-pass cleanup was already thorough).
- New override-tier regex set caught `DocuWare Generated PDF HOA` via `\bDocuWare\b`.
- New OCR-LLM `is_hoa` validation: 0 not-HOA flags (high-confidence threshold + uncertainty-reason guard required).
- New OCR-LLM cross-state detection: **2 deletes** — `Hot Springs Village Property Owners' Association` (state=AR per the doc body, mis-attributed to NV) and `Chadwick Estates Homeowners Association, Inc.` (state=IL per doc body).
- Dedupe-merge: 2 (the three ArrowCreek variants — `ArrowCreek Homeowners Association, Inc.`, `Arrowcreek Homeowners Association, Inc.`, `ArrowCreek Homeowners' Association` — collapsed via /admin/rename-hoa's merge-on-collision path).
- Location backfill: 8 city-only matches via OCR-LLM extraction (mostly Reno).

Result: 26 → 21 live with cleaner names + 8 cities populated.

### Second-pass Phase B/C — Vegas master-planned community keyword-Serper supplemental (`nv_supp_20260509_035058_claude`)
Phase B was originally scoped as name-list-first via the Nevada Real Estate Division Common-Interest Community registry. Investigation showed **NRED's CIC registry has no public bulk download** (the page at `red.nv.gov/Content/CIC/Registration/` provides forms only) and **NV SoS Silverflume entity search is bot-protected** (returns a captcha to non-browser clients). Substituted with a pragmatic Vegas-anchored keyword-Serper run targeting master-planned communities and Henderson neighborhoods.

New query file: `state_scrapers/nv/queries/nv_vegas_communities_serper_queries.txt` (38 queries anchored on Summerlin, Sun City [Summerlin/Anthem/Aliante/MacDonald Ranch], Anthem Country Club, Lake Las Vegas, Aliante, Mountain's Edge, Inspirada, Seven Hills, MacDonald Highlands, Tuscany, Green Valley Ranch, The Lakes, Spanish Trail, Queensridge, Red Rock Country Club, Southern Highlands, Rhodes Ranch, Providence, Skye Canyon, Cadence + Boulder City, Henderson, North Las Vegas).

Yield:
- 40 Serper-derived leads → 11 prepared bundles after Phase 7 filter
- Bank: 142 → 179 manifests (+37)
- Live: 21 → 32 after import
- Phase 10 second pass: −1 dedupe + −5 null-canonical + −4 not-HOA = 32 → 26 live
- Net new genuinely-live entries: Sun City Summerlin, Red Rock Country Club, Sun City Anthem, plus a couple sub-associations.

Many of the imported bundles were master-planned-community master associations or repeated sub-association references; the Phase 10 LLM correctly flagged most of the noise and the regex sweep caught web-scraping artifacts (`Squarespace HOA`, `DocuWare Generated PDF HOA`).

## Counties yielded vs. < 3 net-new

- **Clark** (Las Vegas + Henderson + Boulder City + N. Las Vegas): yielded most of the new bundles in the Vegas supplemental. Clark already covered in overnight; Vegas supplemental added Summerlin/Anthem/Red Rock targeted finds.
- **Washoe** (Reno/Sparks): heavy yield in the original overnight (most live entries are Reno-area).
- **Carson City, Lyon, Douglas, Elko**: thin yield. Carson City + Lyon + Elko returned <3 net-new manifests each in the overnight; Douglas (Lake Tahoe / Stateline) yielded only one or two real condos.
- **Did NOT add Nye / Storey** in this pass: the kickoff brief recommended them but their population density doesn't justify the Serper spend; the overnight's host-family sweep already covered NRS-Chapter-116-anchored statewide queries.

## Lessons learned

1. **NV's keyword-Serper ceiling is low**. Even with Vegas-anchored queries on every major master-planned community, only ~5 net-new live entries surfaced. The Vegas HOA universe is dominated by mgmt-co-portal-hosted documents (FirstService Residential Nevada, Terra West, Associa Sierra North) which Google doesn't index. Future yield comes from **management-company portfolio harvesting**, not better keyword queries.
2. **NRED's CIC registry exists but is operationally inaccessible**. The Office of the Ombudsman holds the database internally; public access requires either a records request or a paid data product. Open question for a future operator: is there a back-channel CSV via the data team? If so, NV becomes a name-list-first state per the playbook.
3. **OCR-LLM cross-state validation is worth the cost.** Two cross-state mis-attributions caught (Hot Springs Village AR, Chadwick Estates IL) that no regex pattern would have caught. The cost is ~$0.001/entity at DeepSeek/Kimi rates — a rounding error against the value.
4. **The cumulative override-regex tier (post-suffix-safelist) is the right architectural fix** for fragments like `Recorder of Madison County … Condominium Association` that have HOA-shape suffix but are clearly non-HOA fragments. Adding patterns to the override list is a one-line PR per pattern.

## Files

- Final state report: `state_scrapers/nv/results/nv_supp_20260509_035058_claude/final_state_report.json`
- OCR-LLM rename ledger: `state_scrapers/nv/results/nv_supp_20260509_035058_claude/name_cleanup_unconditional.jsonl`
- Doc-filename audit: `state_scrapers/nv/results/nv_supp_20260509_035058_claude/doc_filename_audit.json`
- Bbox audit: `state_scrapers/nv/results/nv_supp_20260509_035058_claude/bbox_audit.json`
- Dedupe pairs: `state_scrapers/nv/results/nv_supp_20260509_035058_claude/dedupe_pairs.json`
- Location backfill records: `state_scrapers/nv/results/nv_supp_20260509_035058_claude/location_backfill_records.json`
