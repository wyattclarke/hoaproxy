# AL HOA Scrape Retrospective

## Final state (post second-pass, 2026-05-09)

- **Live HOA count:** 86
- **Bank manifests:** 345
- **Cities backfilled via OCR-LLM:** 13

## Run history

### Overnight (`al_20260508_092739_overnight`, 2026-05-08)
End-to-end run on 18 counties — Jefferson AL (Birmingham), Madison AL (Huntsville), Mobile, Baldwin (Gulf Coast — Orange Beach / Gulf Shores resort condos), Shelby (Birmingham south suburbs), Tuscaloosa, Montgomery, Lee (Auburn — AU), Morgan, Calhoun (Anniston), Etowah, Houston, Limestone (Athens / Huntsville expansion), Marshall, St. Clair, Elmore, Cullman, Talladega — plus state-wide host-family. The most thoroughly-county-covered state in the batch. Banked 345 manifests → prepared 136 → imported 136 → Phase 10 cleaned to 96 live initially.

### Second-pass Phase A (`phase10_close.py` re-run, 2026-05-09)
- **Renames applied:** ~30 (deterministic prefix-strip + LLM canonicalization).
- **Hard-deleted:**
  - `delete_null` (LLM canonical=null): **2**
  - `delete_not_hoa` (OCR-LLM `is_hoa=false`): 0
  - `delete_foreign_state` (OCR-LLM): **1** — likely a Gulf-Coast / Florida-Panhandle spillover (Orange Beach / Pensacola border zone).
  - `delete_audit` (doc-filename audit): 0
  - `delete_regex` (cumulative override regex): **2** — almost certainly `OF PROTECTIVE FOR HERITAGE HOA` (override `^OF [A-Z]+ FOR\s+[A-Z]`) and `Th Phase Camden Ridge` (`^Th\s+Phase\b`).
- **Dedupe-merge:** 2.
- **Location backfill:** 13 city-only matches via OCR-LLM (Birmingham, Huntsville, Mobile, Orange Beach, Gulf Shores, Tuscaloosa, Auburn, Montgomery, Athens, etc.). Of 16 proposed, 13 matched.

Result: 96 → 86 live with 13 cities populated.

### No Phase B/C performed for AL
The kickoff brief recommended adding 6 counties for AL but cross-referencing showed **all 10 user-recommended counties were already in the 18-county overnight set**, plus 8 additional counties (Etowah, Houston, Marshall, St. Clair, Elmore, Cullman, Talladega, Morgan). AL was the most over-covered of the 7-state batch at the county level. Skipped supplemental.

## Counties yielded vs. < 3 net-new

- **Baldwin (Gulf Shores / Orange Beach resort condos)** — strongest yield, as expected for the AL equivalent of AR's Hot Springs Village. Live count carries many beach/condo names.
- **Jefferson AL (Birmingham), Shelby (Birmingham southern suburbs), Madison AL (Huntsville), Mobile, Montgomery, Tuscaloosa, Lee (Auburn)** — strong yield.
- **Morgan, Calhoun, Etowah, Houston, Limestone, Marshall** — moderate.
- **St. Clair, Elmore, Cullman, Talladega** — thinner yield (some had < 3 net-new each); diminishing returns past the 14th-county mark.

## Lessons learned

1. **AL is the strongest "keyword-Serper-just-works" state in the batch.** 86 live HOAs from 345 bank manifests is a 25% retention rate (vs. ~14% for ID, ~11% for NV pre-supplemental). The combination of (a) county recorders publishing PDFs, (b) Gulf-Coast resort condos with public docs, and (c) suburban-density Birmingham/Huntsville metros makes AL well-suited to the keyword playbook.
2. **The 18-county set is at or past the diminishing-returns frontier.** Future yield in AL probably comes from name-list-first via the AL Secretary of State nonprofit registry (registered HOAs file Articles of Incorporation), not from adding more counties.
3. **OCR-LLM caught a Gulf-Coast cross-state mis-attribution.** Florida Panhandle docs occasionally surface in AL queries (Orange Beach AL borders Pensacola FL). The single foreign-state delete was clean.

## Files

- Final state report: `state_scrapers/al/results/al_20260508_092739_overnight/final_state_report.json`
- OCR-LLM rename ledger: `state_scrapers/al/results/al_20260508_092739_overnight/name_cleanup_unconditional.jsonl`
- Regex delete candidates: `state_scrapers/al/results/al_20260508_092739_overnight/regex_delete_candidates.json`
- Doc-filename audit: `state_scrapers/al/results/al_20260508_092739_overnight/doc_filename_audit.json`
- Bbox audit: `state_scrapers/al/results/al_20260508_092739_overnight/bbox_audit.json`
- Location backfill records: `state_scrapers/al/results/al_20260508_092739_overnight/location_backfill_records.json`
