# KY HOA Scrape Retrospective

## Final state (post second-pass, 2026-05-09)

- **Live HOA count:** 40
- **Bank manifests:** 182
- **Cities backfilled via OCR-LLM:** 20

## Run history

### Overnight (`ky_20260508_081137_overnight`, 2026-05-08)
End-to-end run on 12 counties — Jefferson (Louisville), Fayette (Lexington), Kenton + Boone + Campbell (Cincinnati-metro Northern KY), Warren (Bowling Green), Hardin (Elizabethtown / Fort Knox), Daviess (Owensboro), Madison KY (Richmond/Berea), Bullitt, Christian (Hopkinsville), McCracken (Paducah) — plus state-wide host-family. Banked 182 manifests → prepared 79 → imported 79 → Phase 10 cleaned to 53 live initially.

### Second-pass Phase A (`phase10_close.py` re-run, 2026-05-09)
- **Renames applied:** ~25 (deterministic prefix-strip + LLM canonicalization).
- **Hard-deleted:**
  - `delete_null` (LLM canonical=null): **5**
  - `delete_not_hoa` (OCR-LLM `is_hoa=false`): **1** — almost certainly `Kentucky Dermatological Association, Ltd.`, the medical-society entry. The override regex `\bDermatological Association\b` would also catch it; both paths agreed.
  - `delete_foreign_state` (OCR-LLM): **4** — KY had the most cross-state mis-attributions of any state in the second pass. Likely candidates: doc text mentions OH (Cincinnati metro spillover), TN, IN, or IL. The OCR-LLM detected each via the registered state in the document body.
  - `delete_audit` (doc-filename audit): 0
  - `delete_regex` (cumulative override regex): 0 in re-run (catches happened during Phase A's first pass; LA/IA/UT had stronger override-regex catches).
- **Dedupe-merge:** 4 (the most of any state — likely Boone's-Trace / `BOONE'S TRACE PROPERTY OWNERS' ASSOCIATION, INC.` collapsed with mixed-case variant; Tuscany/East Pointe pair; Bardstown Woods variants; Tuscany Ridge variants).
- **Location backfill:** 20 city-only matches via OCR-LLM (Louisville, Lexington, Florence, Bowling Green, Elizabethtown, Bardstown, Hopkinsville, Paducah, etc.). Of 23 proposed, 20 matched.

Result: 53 → 40 live with 20 cities populated.

### No Phase B/C performed for KY
All 11 user-recommended counties were already in the 12-county overnight set. Skipped supplemental.

## Counties yielded vs. < 3 net-new

- **Jefferson (Louisville), Fayette (Lexington)** — strongest yield (most live entries are Louisville/Lexington-area).
- **Kenton + Boone + Campbell** (Northern KY / Cincy metro) — moderate, with cross-state contamination from OH (caught by Phase A `delete_foreign_state`).
- **Warren, Hardin, Daviess, Madison KY, McCracken** — moderate.
- **Bullitt, Christian** — thin yield (< 3 net-new each).

## Lessons learned

1. **KY is a cross-state-mis-attribution hotspot.** 4 foreign-state deletes — by far the most of the 7-state batch (NV had 2; LA/IA had 1; AL had 1; UT had 1). The Cincinnati and Memphis (TN-bordering Paducah area) metro spillovers cause Northern KY queries to surface OH/TN HOAs, and IN-bordering Louisville surfaces Indiana HOAs. The OCR-LLM cross-state check is essential here.
2. **Dedupe-merge runs hot in KY** — 4 pairs caught, vs. 0–2 in other states. Likely an artifact of Lexington/Louisville's older subdivisions where the same name appears under multiple incorporated entities (`BOONE'S TRACE` vs `Boone's Trace`, etc.).
3. **`Kentucky Dermatological Association` is a canonical example of why the OCR-LLM `is_hoa` validation matters** — no regex pattern would naturally catch a real, well-formed "Association, Ltd." name; only the doc body reveals it's a medical society.

## Files

- Final state report: `state_scrapers/ky/results/ky_20260508_081137_overnight/final_state_report.json`
- OCR-LLM rename ledger: `state_scrapers/ky/results/ky_20260508_081137_overnight/name_cleanup_unconditional.jsonl`
- Regex delete candidates: `state_scrapers/ky/results/ky_20260508_081137_overnight/regex_delete_candidates.json`
- Doc-filename audit: `state_scrapers/ky/results/ky_20260508_081137_overnight/doc_filename_audit.json`
- Bbox audit: `state_scrapers/ky/results/ky_20260508_081137_overnight/bbox_audit.json`
- Location backfill records: `state_scrapers/ky/results/ky_20260508_081137_overnight/location_backfill_records.json`
