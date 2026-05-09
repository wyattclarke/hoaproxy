# HI HOA Scrape Retrospective

Two-pass run history. Pass 1 (overnight 9-state orchestrator, 2026-05-08)
ran per-county keyword Serper and landed 12 live HOAs. Pass 2
(name-list-first via DCCA AOUO PDF, 2026-05-08 → 2026-05-09) migrated HI
to the [name-list-first playbook](../../../docs/name-list-first-ingestion-playbook.md)
using the public AOUO Contact List.

## TL;DR

- **Final state:** 206 live HOAs, 188 mapped (91%), 0 out-of-bbox.
- **Coverage of estimated universe:** ~12% of the 1,668-condo AOUO
  registry; ~14% of the 1,445 entities we successfully parsed from the
  PDF.
- **Why so much better than DC's pass 2:** Hawaiiana Management and a
  handful of other Hawaii management companies publish association
  bylaws/declarations to public-web URLs; LLM acceptance rate at Phase
  10 was 79% (vs. DC's ~10%).

## Pipeline numbers

| Stage | Count |
|---|---|
| AOUO PDF parsed | 1,729 raw, 1,445 unique condos |
| Entities with at least one PDF banked | 858 (59%) |
| Total docs banked | 1,204 |
| Bundles prepared (post DocAI) | 953 |
| Bundles imported | 249 (limited by import-loop wall time; see future work) |
| Live HOAs after import | 267 |
| LLM rename `is_hoa: true` accepted | 211 (79%) |
| LLM rename `is_hoa: false` rejected | 56 |
| Live HOAs after Phase 10 hard-delete | 206 |
| Mapped (zip_centroid) | 188 (91%) |
| Out-of-bbox map points | 0 |

## Source

- DCCA AOUO Contact List PDF, revised 2026-04-29:
  https://cca.hawaii.gov/wp-content/uploads/2026/04/AOUO-Contact-List-4.29.26.pdf
- Per-condo extraction: name, registry ID, managing company, mailing
  address (used as soft geo seed when in-state).
- Discovery: `namelist_discover.py` against `hi_aouo_seed.jsonl` with
  `pinned_name=True`.

## Why high acceptance vs DC

Hawaii's condo ecosystem differs from DC's:

- **Hawaiiana Management** (~325 of the 1,445 entities) publishes
  governing-docs PDFs to its public website, which Google has indexed.
- **Associa Hawaii**, **Touchstone**, **Cadmus**, **Hawaii First**,
  **Certified Management** also publish to public URLs.
- **AOUO names ARE the legal names** of the unit-owners associations.
  My queries with `"<NAME>" "Hawaii" "Bylaws"` matched the actual
  documents directly. No need for the loose-match expansion that DC
  required.

## Lessons learned

1. **Name-list-first works phenomenally well when the management cos
   publish public docs.** The 79% LLM acceptance rate is in line with
   GA/TN keyword-Serper acceptance — the discovery pattern doesn't
   matter as much as whether the docs are public.
2. **PDF text extraction on a multi-column registry list is fragile.**
   The `hi_aouo_pipeline.py` parser misses some records (1,729 raw →
   1,445 unique) and creates some artifacts ("Kewalo" instead of "1616
   Kewalo"). Phase 10's regex sweep + LLM rename catches the worst
   cases. Future improvement: pdfplumber-based table extraction would
   be cleaner.
3. **Import-loop hit a 50-bundle-per-call cap** and the finalizer's
   200-iteration loop landed 249 of 953 prepared bundles before
   stopping. The remaining ~700 bundles are still in `gs://hoaproxy-
   ingest-ready/v1/HI/` ready to import in a follow-up call. Run
   `POST /admin/ingest-ready-gcs?state=HI` until empty to lift HI's
   live count further (likely to 600-800).
4. **Map coverage at 91% is excellent** — Hawaii ZIPs (96xxx) embedded
   in OCR'd governing docs gave reliable zip_centroid backfills. The 18
   unmapped entities have no ZIP in their docs (likely older PDFs or
   short bylaws-only files without metes-and-bounds).
5. **No out-of-bbox map points** — Hawaii's geographic isolation means
   no spillover from cross-state contamination.

## Future work

- **Drain remaining HI bundles:** ~700 prepared bundles in
  `gs://hoaproxy-ingest-ready/v1/HI/` are ready to import. Each
  /admin/ingest-ready-gcs call moves 50; ~14 calls would clear them.
- **Page-1 OCR verification** would catch the few "wrong condo's
  PDF" cases the LLM still accepts (when a governing doc for ANOTHER
  Hawaii condo gets banked under a similarly-named entity).

## Files

- Source PDF: `state_scrapers/hi/leads/hi_aouo_2026_04.pdf`
- Seed: `state_scrapers/hi/leads/hi_aouo_seed.jsonl` (1,445 entities)
- Discovery ledger: `state_scrapers/hi/results/hi_namelist_v2_*/namelist_ledger.jsonl`
- Prepare ledger: `state_scrapers/hi/results/*_finalize/prepared_ingest_ledger.jsonl`
- Phase 10 ledger: `state_scrapers/hi/results/*_finalize/name_cleanup_unconditional.jsonl`
- Final state report: `state_scrapers/hi/results/*_finalize/final_state_report.json`
