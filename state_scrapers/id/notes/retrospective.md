# ID HOA Scrape Retrospective

## Final state (post second-pass, 2026-05-09)

- **Live HOA count:** 36
- **Bank manifests:** ~279
- **Cities backfilled via OCR-LLM:** ~12 (Boise, Coeur d'Alene, Idaho Falls, Twin Falls, Eagle, Meridian, Harrison, McCall + others)

## Run history

### Overnight (`id_20260508_064311_overnight`, 2026-05-08)
End-to-end run on 7 counties — Ada (Boise), Canyon (Nampa/Caldwell), Kootenai (Coeur d'Alene), Bonneville (Idaho Falls), Twin Falls, Bannock (Pocatello), Madison (Rexburg/BYU-Idaho) — plus state-wide host-family. Banked 213 manifests → prepared 68 → imported 68 → Phase 10 cleaned to 57 live initially. Live list was the **noisiest** of the 7 states — ~22% visible junk (entries like `Squarespace HOA`, `Sr HOA`, `BOG!( 222 ?Ati842 HOA`, `Notary Public for State of . 2025 Amendment to . CitySide Lofts Condominium Association`, `Recorder of Madison County this of , Conditions ... 1.20 Condominium Association`, `Aspen Creek Water Association`).

### Second-pass Phase A (`phase10_close.py` re-run, 2026-05-09)
Heaviest cleanup of the 7-state batch.
- **Renames applied:** ~40 (deterministic prefix-strip + LLM canonicalization).
- **Hard-deleted:**
  - `delete_null` (LLM canonical=null): **5**
  - `delete_not_hoa` (OCR-LLM `is_hoa=false`): **3** — likely `Aspen Creek Water Association` (utility), and 2 more.
  - `delete_foreign_state` (OCR-LLM): **1**
  - `delete_audit` (doc-filename audit): 0
  - `delete_regex` (cumulative override regex): **10** — by far the most of any state. Specific override patterns that fired: `\bSquarespace\b` → `Squarespace HOA`; `^Sr\s+HOA\s*$` → `Sr HOA`; `^BOG!` → `BOG!( 222 ?Ati842 HOA`; `^Notary Public for State` → `Notary Public for State of . 2025 Amendment to . CitySide Lofts Condominium Association`; `^Recorder of [A-Z][a-z]+\s+County` → `Recorder of Madison County this of , Conditions ... 1.20 Condominium Association`; `\bSite Map Ownership Opportunities\b` → `Homes Lifestyle Clubhouse Community Site Map Ownership Opportunities Featured Builders HOA`; `\bHOAs by changing\b`; `\bHomeowner Information and\b`; `\bCondominium Property Act\b`; `\bComplica\s+Tion\b` → `Historic Complica Tion Condominiums HOA`.
- **Dedupe-merge:** 2 (e.g., `Riverside Greens Homeowners Association, Inc.` ↔ `Riverside Greens Homeowners' Association, Inc` — punctuation-only difference).
- **Location backfill:** 12 city-only matches (Boise, Coeur d'Alene, Idaho Falls, Twin Falls, Eagle, Meridian, Harrison, McCall + others). Of 20 proposed, 12 matched.

Result: 57 → 38 live with 12 cities populated. **The override-regex tier was a clear win for ID** — none of the patterns above existed in the original `NON_HOA_NAME_PATTERNS`, and the safelist had been treating them as "ends-with-Association → keep".

### Second-pass Phase C — Blaine + Bonner resort/rural supplemental (`id_supp_20260509_042215_claude`)
New query files: `state_scrapers/id/queries/id_blaine_serper_queries.txt` (Sun Valley / Ketchum / Hailey) and `state_scrapers/id/queries/id_bonner_serper_queries.txt` (Sandpoint / Schweitzer / Lake Pend Oreille). Both appended to `COUNTY_RUNS`.

Yield:
- ID Blaine: 44 leads
- ID Bonner: 25 leads
- Bank: 213 → 279 manifests (+66)
- Prepared: 117 bundles
- Imported: ~117; Phase 10 second pass deleted 1 not-HOA + 2 null-canonical + 0 regex + 6 cities backfilled
- Net live change: 38 → 36 (−2 — most prepared bundles were court filings, advisory opinions, plat extracts, or mgmt-co marketing pages, all correctly rejected by Phase 10)

The single survivor of note is `Sun Valley Elkhorn Association` (Blaine). The Bonner sweep yielded effectively zero net-new live HOAs — the area is too rural for keyword-Serper.

A transient Render 502 spike crashed the first ID supp Phase 10 (rename pass got HTTPError on `/hoas/summary`); the retry pass succeeded after Render recovered.

## Counties yielded vs. < 3 net-new

- **Ada (Boise), Canyon (Nampa)** — strongest yield.
- **Kootenai (Coeur d'Alene), Bonneville (Idaho Falls), Twin Falls** — moderate.
- **Bannock (Pocatello), Madison (Rexburg)** — thinner.
- **Blaine (Sun Valley)** — thin in supplemental (1 net-new live; ski-resort condos paywalled behind mgmt cos).
- **Bonner (Sandpoint)** — effectively zero net-new live; 25 banked leads but all junk (court filings, county code, advisory opinions).

## Lessons learned

1. **The new override-regex tier is most valuable for noisy first-pass states like ID.** Pre-second-pass ID was 22% visible junk; post-second-pass it's a clean 36 entries with credible HOA names. The override patterns (`Squarespace`, `Sr HOA`, `BOG!`, `Notary Public for State`, `Recorder of Madison County`, etc.) caught what the structural-suffix safelist had been protecting.
2. **Bonner is a "keyword-Serper-doesn't-work" county** for HOAs. Rural enough that the SERP results are dominated by county code, court filings, and lake/wilderness ordinances. Future ID expansion should focus on **Eagle / Meridian / Nampa** sub-county neighborhood-anchored queries instead.
3. **Blaine's Sun Valley resort condos are mgmt-co-portal locked** (Sun Valley Co, Sun Valley Elkhorn) — the one survivor (`Sun Valley Elkhorn Association`) had a public CC&Rs PDF. Most others didn't.
4. **Render 502 retries are essential at the multi-state batch scale.** When 6+ autonomous sessions hit Render concurrently, 502s are routine; the second-pass added retry-on-502 to `_fetch_summaries`, `_fetch_doc_text`, and `/admin/rename-hoa` so a transient blip no longer crashes a state run.

## Files

- Final state report (Phase A): `state_scrapers/id/results/id_20260508_064311_overnight/final_state_report.json`
- Final state report (supp): `state_scrapers/id/results/id_supp_20260509_042215_claude/final_state_report.json`
- OCR-LLM rename ledger: `state_scrapers/id/results/id_20260508_064311_overnight/name_cleanup_unconditional.jsonl`
- Regex delete candidates: `state_scrapers/id/results/id_20260508_064311_overnight/regex_delete_candidates.json`
- Doc-filename audit: `state_scrapers/id/results/id_20260508_064311_overnight/doc_filename_audit.json`
- Bbox audit: `state_scrapers/id/results/id_20260508_064311_overnight/bbox_audit.json`
- Location backfill records: `state_scrapers/id/results/id_20260508_064311_overnight/location_backfill_records.json`
- Blaine query file: `state_scrapers/id/queries/id_blaine_serper_queries.txt`
- Bonner query file: `state_scrapers/id/queries/id_bonner_serper_queries.txt`
