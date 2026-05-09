# LA HOA Scrape Retrospective

## Final state (post second-pass, 2026-05-09)

- **Live HOA count:** 32
- **Bank manifests:** 226
- **Cities backfilled via OCR-LLM:** 13

## Run history

### Overnight (`la_20260508_113435_overnight`, 2026-05-08)
End-to-end run on 16 parishes — East Baton Rouge, Jefferson, Orleans, St. Tammany, Lafayette, Caddo, Calcasieu, Ouachita, Livingston, Rapides, Tangipahoa, Ascension, Bossier, Terrebonne, Lafourche, Iberia — plus state-wide host-family. Banked 226 manifests → prepared 75 → imported 75 → Phase 10 cleaned to 41 live initially. Names dominated by Baton Rouge / north shore communities.

### Second-pass Phase A (`phase10_close.py` re-run, 2026-05-09)
- **Renames applied:** ~30 (deterministic prefix-strip + LLM canonicalization). Mid-run a transient Render 502 caused the rename apply to error on one chunk; recovered without manual intervention.
- **Hard-deleted:**
  - `delete_null` (LLM canonical=null): **4**
  - `delete_regex` (cumulative override regex): **3** — `OF THE ST. CHARLES PLACE HOA` (override `^OF [A-Z]+ FOR\s+[A-Z]` variant), `Th Restrictions` (`^Th\s+Restrictions`), `Conditions and Oak Harbor Subdivision HOA` (`^conditions and\b` / `\bConditions and Oak Harbor\b`).
  - `delete_audit` (doc-filename audit): 0
  - `delete_foreign_state` (OCR-LLM): 0
  - `delete_not_hoa` (OCR-LLM): 0
- **Dedupe-merge:** 1 (a near-duplicate parish-name variant collapsed via /admin/rename-hoa merge-on-collision).
- **Location backfill:** 13 cities matched (8 of 21 LLM proposals didn't resolve due to mid-run rename re-keying).

Result: 41 → 32 live. Still has a couple residual fragments (`Th Restrictions` was caught but other fragment-shaped names like `Rouzan Residential` and `X C Ae` remained — `X C Ae` was deleted via the `^[A-Z]\s+[A-Z]\s+[A-Z][a-z]?\s*$` override, but `Rouzan Residential` is now the only doc for "Rouzan" — preserved via dedupe-merge into the canonical entry).

### No Phase B/C performed for LA
The kickoff brief flagged LA for an 8-parish supplemental sweep, but cross-referencing the existing COUNTY_RUNS showed all 11 user-recommended parishes (East Baton Rouge, Orleans, Jefferson, St. Tammany, Lafayette, Caddo, Bossier, Calcasieu, Ouachita, Livingston, Ascension) were ALREADY in the 16-parish overnight set. Re-running the same parish queries would produce ~0 net-new manifests. Skipped.

## Parishes yielded vs. < 3 net-new

The 16-parish overnight set held up well — most parishes contributed at least a few live HOAs. **Caddo, Calcasieu, Ouachita, Rapides, Iberia, Lafourche, Bossier** yielded thinner than St. Tammany / Jefferson / East Baton Rouge / Lafayette. NOLA (Orleans Parish) underperformed expectations — only ~5 live entries despite ~1.3M-population metro; this is the keyword-Serper-failure-mode mentioned in the name-list-first playbook (NOLA condo stock is paywalled / behind portals).

## Lessons learned

1. **`Parish` vs `County` query phrasing matters in LA.** The existing overnight queries already used "Parish" anchor — confirmed working. No regression.
2. **Render 502 mid-run is now non-fatal.** The patches added in this second-pass (retry-on-502 in `_fetch_summaries`, retry on `_fetch_doc_text`, and retry on /admin/rename-hoa apply chunks) survive the kind of intermittent 502 that previously crashed the LA run and reported all-"no_candidates".
3. **NOLA opportunity for a future name-list-first.** LA Department of State has a registered-nonprofit search; LA Office of Financial Institutions has a Common-Interest-Community quasi-registry (under Louisiana Condominium Act §9:1121). If NOLA is prioritized, that's the registry to pull.

## Files

- Final state report: `state_scrapers/la/results/la_20260508_113435_overnight/final_state_report.json`
- OCR-LLM rename ledger: `state_scrapers/la/results/la_20260508_113435_overnight/name_cleanup_unconditional.jsonl`
- Regex delete candidates: `state_scrapers/la/results/la_20260508_113435_overnight/regex_delete_candidates.json`
- Doc-filename audit: `state_scrapers/la/results/la_20260508_113435_overnight/doc_filename_audit.json`
- Bbox audit: `state_scrapers/la/results/la_20260508_113435_overnight/bbox_audit.json`
- Location backfill records: `state_scrapers/la/results/la_20260508_113435_overnight/location_backfill_records.json`
