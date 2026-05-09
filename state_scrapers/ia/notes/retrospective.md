# IA HOA Scrape Retrospective

## Final state (post second-pass, 2026-05-09)

- **Live HOA count:** 44
- **Bank manifests:** 190
- **Cities backfilled via OCR-LLM:** 27

## Run history

### Overnight (`ia_20260508_052419_overnight`, 2026-05-08)
End-to-end run on 10 counties — Polk (Des Moines), Linn (Cedar Rapids), Scott (Davenport), Johnson (Iowa City), Black Hawk (Waterloo/Cedar Falls), Woodbury (Sioux City), Dubuque, Story (Ames — ISU), Dallas, Pottawattamie (Council Bluffs) — plus state-wide host-family. Banked 190 manifests → prepared 80 → imported 80 → Phase 10 cleaned to 51 live initially.

### Second-pass Phase A (`phase10_close.py` re-run, 2026-05-09)
- **Renames applied:** ~30 (deterministic prefix-strip + LLM canonicalization).
- **Hard-deleted:**
  - `delete_null` (LLM canonical=null): **6**
  - `delete_not_hoa` (OCR-LLM `is_hoa=false` w/ confidence ≥ 0.85): **2** — likely `Iowa Parks and Recreation Association, Incorporated` (caught by override regex `\bParks and Recreation Association\b`) plus one other professional-society-shaped entry.
  - `delete_foreign_state` (OCR-LLM): **1** — entity whose doc text states a non-IA state.
  - `delete_audit` (doc-filename audit): 0
  - `delete_regex` (cumulative override regex): 0 in this re-run (most regex catches happen against live names; `Council-Bluffs-Whispering-Oaks- -lot-198-295.pdf HOA` would match `^Council-[A-Z][a-z]+-` if it survived rename, but the LLM rename pass renamed it first so the regex-delete didn't fire).
- **Dedupe-merge:** 0 (no near-duplicates found in IA).
- **Location backfill:** 27 city-only matches via OCR-LLM (Des Moines, Cedar Rapids, Iowa City, Ankeny, Council Bluffs, Sioux City, Dubuque, Ames, West Des Moines, etc.). Of 31 LLM-proposed, 27 matched (4 not_found due to mid-run rename re-keying).

Result: 51 → 44 live with 27 cities populated.

### No Phase B/C performed for IA
The kickoff brief recommended adding Cerro Gordo (Mason City) and confirmed the other 10 user-recommended counties were already in COUNTY_RUNS. Cerro Gordo is rural (pop ~42k) and the expected yield from a Serper sweep is < 3 net-new manifests vs. the cost. Skipped.

## Counties yielded vs. < 3 net-new

- **Polk (Des Moines), Linn (Cedar Rapids), Scott (Davenport), Johnson (Iowa City)** — strong yield, dominate the live list.
- **Black Hawk, Woodbury, Story, Dallas, Dubuque, Pottawattamie** — moderate yield.
- **None below 3 net-new** in the overnight; all 10 counties contributed at least a few real HOAs.

## Lessons learned

1. **Iowa cleanup is uneventful** — IA's first-pass had relatively clean names already (no DC-style condo-fragment pattern, fewer all-caps-shouting entries than Southern states). The second-pass Phase A delivered a modest 7-entry trim (mostly utility/agency mis-attributions) and a healthy 27-city location backfill — the location backfill is arguably the bigger win for IA.
2. **OCR-LLM is the right mechanism for non-HOA professional society catches.** `Iowa Parks and Recreation Association, Incorporated` is HOA-shape-suffixed (ends in "Association, Incorporated" → safelisted from regex sweep) but is unambiguously a state agency, not a community HOA. The OCR-LLM pass correctly identified `is_hoa=false` from the doc body.

## Files

- Final state report: `state_scrapers/ia/results/ia_20260508_052419_overnight/final_state_report.json`
- OCR-LLM rename ledger: `state_scrapers/ia/results/ia_20260508_052419_overnight/name_cleanup_unconditional.jsonl`
- Regex delete candidates: `state_scrapers/ia/results/ia_20260508_052419_overnight/regex_delete_candidates.json`
- Doc-filename audit: `state_scrapers/ia/results/ia_20260508_052419_overnight/doc_filename_audit.json`
- Bbox audit: `state_scrapers/ia/results/ia_20260508_052419_overnight/bbox_audit.json`
- Location backfill records: `state_scrapers/ia/results/ia_20260508_052419_overnight/location_backfill_records.json`
