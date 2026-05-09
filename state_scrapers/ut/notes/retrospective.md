# UT HOA Scrape Retrospective

## Final state (post second-pass, 2026-05-09)

- **Live HOA count:** 70 (after Phase A: 71; after Wasatch supp + Phase A retry: 70)
- **Bank manifests:** ~340 (post Wasatch supplemental)
- **Cities backfilled via OCR-LLM:** ~12 confirmed (+22 pending one Render-recovery retry)

## Run history

### Overnight (`ut_20260508_135443_overnight`, 2026-05-08)
End-to-end run on 10 counties — Salt Lake, Utah (Provo/Orem — BYU), Davis (Layton/Bountiful), Weber (Ogden), Washington UT (St. George + Snow Canyon — fastest-growing UT metro, condo-heavy), Cache (Logan — USU), Tooele, Iron (Cedar City), Box Elder, Summit (Park City — major ski resort) — plus state-wide host-family. Banked 311 manifests → prepared 129 → imported 129 → Phase 10 cleaned to 83 live initially.

### Second-pass Phase A (`phase10_close.py` re-run, 2026-05-09)
- **Renames applied:** ~25 (deterministic prefix-strip + LLM canonicalization).
- **Hard-deleted:**
  - `delete_null` (LLM canonical=null): **1**
  - `delete_not_hoa` (OCR-LLM `is_hoa=false`): 0
  - `delete_foreign_state` (OCR-LLM): **1**
  - `delete_audit` (doc-filename audit): 0
  - `delete_regex` (cumulative override regex): **7** — patterns that fired included `\bCc Rs(?:\s+Redline)?\s*$` (`Cedar Highlands Cc Rs Redline`); `^COLLECTIONS RESOLUTION\b` (`COLLECTIONS RESOLUTION HOA`); `^Of [A-Z][a-z]+ for\s+[A-Z]` (`Of Condominium for Vivante HOA`, `OF CONDOMINIUM FOR KNIGHT BUNGALOW HOA`); `\b\d{3,5}\s+TOWNHOMES\s+HOA\s*$` (`2100 TOWNHOMES HOA`); `^conditions and\b` (`of , conditions and HOA`).
- **Dedupe-merge:** 2.
- **Location backfill:** 12 city-only matches via OCR-LLM (Salt Lake City, Provo, Ogden, St. George, Logan, Park City, Cedar City, etc.). Of 13 proposed, 12 matched.

Result: 83 → 71 live with 12 cities populated.

### Second-pass Phase C — Wasatch resort county supplemental (`ut_supp_20260509_043244_claude`)
New query file: `state_scrapers/ut/queries/ut_wasatch_serper_queries.txt` (Heber City / Midway / Jordanelle / Deer Valley anchored). Appended to `COUNTY_RUNS`.

Yield:
- Wasatch: 31 leads
- Bank: 311 → 340 manifests (+29)
- Prepared: 147 bundles (full pre-prepare cleanup pass over the entire UT bank, not just Wasatch)
- Imported: ~147 with most colliding into existing slugs or being routed to `_unresolved-name`
- Phase 10 second pass: hit Render 502 spike during rename pass (`_fetch_summaries` + `/admin/rename-hoa` 502'd), then succeeded on retry. Final retry: 7 renames, 1 foreign-state delete, 0 not-HOA, 0 null-canonical (most flagged with `skip_delete=True` due to mid-run 502 on doc-text fetch), 22 city backfills proposed (apply pending).

Net live change: 71 → 70 (the foreign-state delete). The 22 pending Wasatch-area cities (Park City, Heber City, Midway, Sandy, etc.) are queued in `state_scrapers/ut/results/ut_supp_20260509_043244_claude/location_backfill_records.json` for a final POST to `/admin/backfill-locations` once Render is fully stable.

The Wasatch-specific entries that survived include a couple Park City / Deer Valley HOAs (e.g., `Lodges at Snake Creek Owners Association`, `Sunburst Ranch HOA - Midway City Homeowners Association`).

## Counties yielded vs. < 3 net-new

- **Salt Lake, Utah, Davis, Weber, Washington UT** — strongest yield (largest metros).
- **Cache (Logan), Summit (Park City)** — moderate. Summit yielded a few real Park City condos in the overnight.
- **Tooele, Iron (Cedar City), Box Elder** — thin yield.
- **Wasatch (Heber/Midway/Deer Valley)** — thin in the supplemental; ~3-5 net-new live entries despite 31 banked leads. Most Wasatch-county leads were town-of-Stockton / county-of-Wasatch ordinances, not community HOAs.

## Lessons learned

1. **UT had heavy override-regex catches (7 patterns).** The `Of Condominium for X` patterns are a UT-specific OCR artifact (declarations of condominium drafted in older Utah-recorder style start with "OF CONDOMINIUM FOR <NAME>" as the title). The override regex correctly catches them even though they end in "HOA" / "Condominium".
2. **Park City / Deer Valley resort condos are partially mgmt-co-portal-locked**, but more publicly-visible than Sun Valley (Blaine ID). Wasatch yielded a few real entries; Blaine yielded essentially zero.
3. **The UT supp encountered the most severe Render 502 turbulence of any state in the second-pass batch.** This drove the addition of retry-on-502 to `_fetch_summaries`, `_fetch_doc_text`, and `/admin/rename-hoa` apply chunks (now committed in `clean_dirty_hoa_names.py`). Without those retries, UT's supp Phase 10 would have reported all-`no_candidates` and silently skipped the cleanup of the 22 city backfills.

## Files

- Final state report (Phase A): `state_scrapers/ut/results/ut_20260508_135443_overnight/final_state_report.json`
- Final state report (supp): `state_scrapers/ut/results/ut_supp_20260509_043244_claude/final_state_report.json`
- OCR-LLM rename ledger: `state_scrapers/ut/results/ut_20260508_135443_overnight/name_cleanup_unconditional.jsonl`
- Regex delete candidates: `state_scrapers/ut/results/ut_20260508_135443_overnight/regex_delete_candidates.json`
- Doc-filename audit: `state_scrapers/ut/results/ut_20260508_135443_overnight/doc_filename_audit.json`
- Bbox audit: `state_scrapers/ut/results/ut_20260508_135443_overnight/bbox_audit.json`
- Pending location backfill records: `state_scrapers/ut/results/ut_supp_20260509_043244_claude/location_backfill_records.json`
- Wasatch query file: `state_scrapers/ut/queries/ut_wasatch_serper_queries.txt`
