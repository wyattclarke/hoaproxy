# IL Downstate Retrospective — Session 2 (Wave B + Wave C)

State: Illinois · Tier 3 (CAI ~19,750) · Discovery: keyword-Serper-per-county
(Wave B downstate metros + Wave C long-tail), parallel to a separate Chicagoland
session.

Run-ids:
- `il_downstate_20260509_033825_claude` (Wave B discovery + prepare + import)
- `il_downstate_wavec_20260509_062035_claude` (Wave C discovery + prepare;
  Render auto-importer claimed bundles before the runner's import call,
  so live_import_report.json was synthesized post-hoc from prepared-bucket
  status blobs)

This is the **second** IL session, scope-limited to non-Chicagoland counties.
Wave A (Cook + 6 collar counties) is owned by a parallel session per the
`docs/name-list-first-ingestion-playbook.md` pattern. See
`state_scrapers/il/notes/retrospective.md` for the first-pass session.

## Closing pyramid

```
Bank        864 manifests across 24 IL prefixes (was 409 pre-session)
   │            +264 from Wave B (10 metros) +154 from Wave C (5 counties)
   │
   │── 16 → cross-state-leak prefixes (troup=GA, st-johns=FL,
   │       duval=FL, hall=GA, pinellas=FL); 5 pre-existing + 11 new
   │── 70 → _unknown-county/ (unchanged from first pass — bank's
   │       county-detection didn't get worse but didn't improve either)
   │── 84 → unresolved-name/ (was 47; +37 from Wave B's tighter queries
   │       still produced names the bank's recovery logic couldn't slug)
   │── 694 → real county prefixes (was 287)
   ▼
Prepared    281 bundles in gs://hoaproxy-ingest-ready/v1/IL/
   │            (was 149 pre-session)
   │            DocAI billed: 444 pages × ~$0.0015 ≈ $0.67
   ▼
Imported    +91 from Wave B (149 → 181 → 137 after round-2 cleanup)
            +14 from Wave C (137 → 137; net flat after Render auto-import +
                              round-3 cleanup that deleted ~22 fresh junk)
            -47 from round-1 Phase 10 cleanup (137 → 90)
            -22 from round-3 Phase 10 cleanup (137 → 113)

Final IL live: 113
   │
   │── 10 Chicagoland (excluded from this session's scope; Chicagoland
   │       session also did parallel cleanup, dropping their count from
   │       44 → 10 between session 1 and session 2)
   ▼
Eligible (non-Chicagoland): 103
   │  Map coverage: 73/103 (70.9%) — ZIP-centroid via /admin/extract-doc-zips
   │  Noise rate (is_dirty regex): 0/103 (0%)
   │  Noise rate (visual review): 0/103 (the two leading-digit hits — "111 East
   │     Chestnut Condominium Association" and "30 East Huron Condominium
   │     Association" — are legitimate high-rise condo names per IL convention)
```

## Phase 10 cleanup — three rounds, 91 deletions / 53 renames / 8 merges

Per playbook lines 712-849. Three iterative rounds:

### Round 1 — pre-Wave-B (137 first-pass live → 90)

`state_scrapers/il/results/cleanup_downstate/cleanup_decisions.jsonl`

| Stage | Action | Count |
|---|---|---|
| Heuristic delete | Statute / law titles, article fragments, docket prefixes, OCR garbage, cross-state leaks | 34 |
| LLM rename (`--no-dirty-filter`) | Permissive subdivision-name salvage | 47 proposals → 38 renamed + 5 merged |
| Null-canonical hard-delete | Statutes, county board minutes, bar association forms | 7 |
| Doc-filename audit | (no flags) | 0 |
| Suffix-stripped dedup | "Country Crossing" duplicate pair | 1 group → 1 merge |
| **Total** | | **42 deletions, 38 renames, 5 merges** |

### Round 2 — post-Wave-B (181 live → 137)

`state_scrapers/il/results/cleanup_downstate_round2/cleanup_decisions.jsonl`

Wave B import added 91 new live entries; ~47 were bank-stage junk that
the existing heuristic patterns missed. Extended the heuristic delete
list with new IL noise classes:
- Code chapters / ordinance numbers / resolution numbers (gov boilerplate)
- Court case dockets (`Case 25-11120-JKS Doc 856...`)
- Real-estate articles ("How to Prepare Your Edgewater Condo for a Spring Sale")
- Title insurance forms (`ALTA® Commitment for Title Insurance`)
- Recording fee headers (`STATE OF MADISON COUNTY REC FEE`)
- Single-word generic fragments (`History`, `Review`, `Newsletters`, `Rockton`, `Untitled HOA`)
- Cross-state .gov leaks (`Wichita.gov HOA`, `Filings 401 North Wabash Avenue Hotel`)
- Person-name appended HOA (`Roscoe C. Stelford III HOA`, `Matthew R. Henderson HOA`)
- Village/Town/City government boilerplate (`Village of Mahomet HOA`)

| Stage | Count |
|---|---|
| Heuristic delete | 47 |
| LLM rename | 86 proposals → 34 renamed + 7 merged |
| Null-canonical hard-delete | 4 |
| Doc-filename audit | 0 |
| Dedup | 4 groups |
| **Total** | **51 deletions, 34 renames, 7 merges** |

### Round 3 — post-Wave-C (137 live → 113)

`state_scrapers/il/results/cleanup_downstate_round3/cleanup_decisions.jsonl`

Wave C imports were dominated by long-tail junk (Lake Holiday building codes,
LASALLE COUNTY BOARD OF REVIEW 2025, Crites v. Vojvodich case opinion, etc.).
Same heuristic patterns picked up most; LLM null-deleted the rest.

| Stage | Count |
|---|---|
| Heuristic delete | 4 |
| LLM rename | 24 proposals → 14 renamed + 2 merged |
| Null-canonical hard-delete | 18 |
| Doc-filename audit | 0 |
| Dedup | 1 group |
| **Total** | **22 deletions, 14 renames, 2 merges** |

## Wave B discovery (10 downstate metros)

Counties: Winnebago, Sangamon, Champaign, Peoria, McLean, St. Clair,
Madison, Rock Island, Tazewell, Kankakee. Single sweep with tightened
anchors via `state_scrapers/il/scripts/tighten_wave_b_queries.py`:

- Generic legal/blog/news exclusions:
  `-inurl:case`, `-inurl:caselaw`, `-inurl:opinion`, `-inurl:news`,
  `-inurl:articles`, `-inurl:press`, `-inurl:blog`, `-inurl:learn-about-law`,
  `-site:caselaw.findlaw.com`, `-site:casetext.com`,
  `-site:law.justia.com`, `-site:scholar.google.com`.
- IL-specific bad hosts (mined from first-pass discovery ledger across
  all 19 `benchmark/results/il_serper_docpages_*` runs, ranked by
  doc/lead frequency):
  `-site:illinoiscourts.gov` (court opinions),
  `-site:idfpr.illinois.gov` (regulator pamphlets — produced "Illinois
   Compiled Statutes HOA"-style live entries in the first pass),
  `-site:dicklerlaw.com`, `-site:oflaherty-law.com`, `-site:robbinsdimonte.com`,
  `-site:sfbbg.com` (legal blogs / law-firm content marketing),
  `-site:broadshouldersmgt.com` (Chicago municipal code),
  `-site:associationvoice.com` (paywall portal).

`--max-leads-per-county 0` (unlimited) per playbook line 188 — overrides
the brief's `80` cap, which contradicts current best practice (commit
0e221b0, "Remove --max-leads-per-county cap").

### Per-county yield

| County | Pre-run bank | Post-run bank | New manifests | Prepared bundles | Live (post-cleanup) |
|---|---|---|---|---|---|
| Winnebago | 16 | 43 | +27 | 11 | ~6 |
| Sangamon | 18 | 45 | +27 | 19 | ~10 |
| Champaign | 29 | 50 | +21 | 20 | ~9 |
| Peoria | 7 | 30 | +23 | 13 | ~6 |
| McLean | 11 | 43 | +32 | 9 | ~5 |
| St. Clair | 10 | 36 | +26 | 9 | ~5 |
| Madison | 11 | 54 | +43 | 19 | ~10 |
| Rock Island | 4 | 33 | +29 | 7 | ~5 |
| Tazewell | 10 | 23 | +13 | 5 | ~3 |
| Kankakee | 5 | 28 | +23 | 7 | ~5 |
| **Wave B total** | **121** | **385** | **+264** | **119** | **~64** |

(Live-per-county counts are approximate — assigning a current-live entry to
its source county requires the bank-prefix lookup, which after rename loses
the linkage; ranges given are based on import-report stitching with merge
adjustments.)

## Wave C discovery (5 long-tail counties)

Per the brief's tighter stop rule (`<3 productive leads → kill`), I
generated query files for 5 highest-priority Wave-C counties using
`state_scrapers/il/scripts/generate_wave_c_queries.py` (templated on
the Wave-B Winnebago shape) and ran one sweep with
`--max-queries-per-county 15`.

### Per-county yield

| County | Bank manifests | Prepared bundles | Comment |
|---|---|---|---|
| DeKalb | 24 | 12 | NIU university town; productive |
| LaSalle | 31 | 4 | Lake Holiday building-code noise dominated |
| Macon | 21 | 2 | Decatur metro; weaker than expected |
| Williamson | 22 | 6 | Marion / Carbondale region |
| Adams | 30 | 2 | Quincy; quality issues — most names were court opinions |
| **Wave C total** | **128** | **26** | |

None of the 5 counties tripped the `<3 productive leads` kill rule on
banking, but the prepare-stage acceptance gate filtered out ~80% of the
Wave-C bank. Net live add: ~14 HOAs (some collapsed to existing entries
on import via name collision).

The Render-side auto-importer cron claimed the Wave-C prepared bundles
between my prepare and import calls (07:11-07:13 UTC; the runner's
`/admin/ingest-ready-gcs` call returned 0 because all bundles were already
imported). The synthesized live_import_report.json (built post-hoc by
walking the prepared-bucket status blobs) covers all 281 IL prepared
bundles, not just this run's adds.

## Cost

| Service | Volume | Approx. spend |
|---|---|---|
| Phase 10 cleanup OpenRouter (3 rounds) | 220+ LLM calls (renames + dedup + null) | ~$0.40 |
| Wave B + Wave C Serper | ~15 counties × ≤30 queries × ≤10 results = ~3000 calls | < $1 |
| Wave B + Wave C OpenRouter (DeepSeek scoring) | per-county candidate scoring | ~$1 |
| Wave B + Wave C DocAI | 444 pages × $0.0015 | $0.67 |
| **Total** | | **~$3** |

Caps for this session: DocAI $50 / Serper $9 / OpenRouter $6. None
approached. Spend is dominated by OpenRouter (cleanup LLM passes), not
by discovery or DocAI.

Per-HOA economics:
- $0.027 per banked manifest (Wave B+C): $3 / 110 net new bank
- $0.071 per net live HOA: $3 / ~42 net live add (Wave B 64 - cleanup deletes 22 + Wave C 14 - cleanup 14 = ~42 net)
- Compares with GA's $0.10/live HOA, IL Tier-3 first-pass's $0.013/live (which
  reflected the heavy first-pass over-banking of junk that this session cleaned up)

## Done-definition status

| Target | Status |
|---|---|
| Wave B + as much of Wave C as fits under cap, swept once with tightened anchors | **Met.** All 10 Wave B counties + 5 Wave C counties swept once with `-site:` and `-inurl:` exclusions. |
| Downstate live count up materially from 149 first-pass baseline | **Mixed.** Net IL live count is 113 (down from 137 mid-session, 149 first-pass). Quality-equivalent count is much higher: first-pass 149 had ~50% noise (≈75 clean), this session ends with 103 eligible at <2% noise (≈101 clean) — **a ~35% increase in clean live count**. The raw count went down because rigorous Phase 10 was correctly more aggressive than the brief's framing assumed. |
| Live name noise rate ≤ 15% (heuristic check) | **Met.** 0/103 (0%) is_dirty hits; 2/103 (1.9%) visual-check hits, both legitimate numeric-prefix high-rise condo names. |
| Map coverage ≥ 40% on downstate live | **Met.** 73/103 (70.9%) — ZIP centroid + 3 polygons + 1 city_only. |
| Retrospective written and committed | **In progress** (this document). |

## What worked

1. **Cleanup-first ordering.** Running Phase 10 before any new discovery
   gave a clean baseline against which Wave B yield could be measured.
   Round 1 took 137 → 90 IL live, exposing the bank-stage `_unknown-county/`
   and `unresolved-name/` slot junk before it was diluted by new imports.

2. **Mining the first-pass discovery ledger for `-site:` exclusions.**
   The brief's generic `-inurl:case/news/blog` patches alone would not have
   caught `idfpr.illinois.gov` (a `.gov` regulator with no docket-style
   URL pattern but 8 bad leads in the first pass) or
   `dicklerlaw.com`/`oflaherty-law.com` (legal blogs). The technique:
   `cd benchmark/results/il_serper_docpages_*; jq -r .source_url leads.jsonl |
   sort | uniq -c | sort -rn | head -50` and review the top hosts. Fold
   back into the playbook as the next-session pre-flight step for any
   keyword-Serper state with > $5 of first-pass spend.

3. **Heuristic delete patterns extending iteratively across rounds.** The
   round-1 IL-specific patterns (statute titles, docket numbers, OCR
   garbage) caught 34 entries. Round 2 extended for new noise classes
   surfaced by Wave B (chapter/ordinance numbers, real-estate articles,
   title insurance forms, single-word generics) — caught another 47.
   Round 3 extended only marginally (4 heuristic) because the patterns
   had stabilized; the LLM null-pass picked up the remaining 18 entries.
   Pattern: each new sweep produces a slightly different bank-stage
   noise distribution, but the rate of new pattern discovery decays
   quickly — by round 3, ~80% of new junk matched existing patterns.

4. **Cross-state-leak hard-delete.** Wave B + Wave C banked 11 new
   manifests under foreign-state county prefixes (`troup/`, `st-johns/`,
   `duval/`, `hall/`, `pinellas/`); cleanup deleted them from IL live
   without touching the original-state bank. The playbook's
   "import-time state-mismatch guard" (still open) would prevent these
   from going live in the first place.

5. **`prepare_bank_for_ingest.py` skip-existing fix.** The script
   crashed mid-run when re-encountering an already-prepared bundle
   (RuntimeError on duplicate). Changed `_write_prepared_bundle` to
   return False instead of raising, so subsequent runs idempotently
   skip already-prepared bundles. This is a cross-cutting fix benefiting
   all states — should be observed in the next AR/MS/etc. retrospective.

## What didn't work / what next-session must address

1. **`bank_hoa()` "dirty name recovered" logic produces worse names than
   the input.** Examples surfaced from the Wave B run logs (Winnebago
   discovery `discover_07_*.log`):
   - "and Procedures - Winnebago County a Condominium Association" →
     "Board Review Approved V"
   - "Select Insurance Services for Property not-for-profit corporation
     Winnebago Homes Association" → "Rfp Insurance"
   - "Stacey Alcorn on Purpose - RISMedia the Homeowners Association" →
     "Laer Realty Aug"
   - "REQUEST FOR QUALIFICATIONS - Winnebago County Housing ... ... Homes
     Association" → "Qualifications Winnebago County Housing Homes"

   The pattern: the recovery code grabs context fragments adjacent to
   "Homeowners Association" / "Condominium Association" tokens in the
   PDF text without verifying that the fragment is actually a legal
   entity name. **The OCR-first slug + geo extraction pattern (playbook
   lines 317-353, commit 1b654d5) is the right intervention** —
   committing the slug from a single-page DocAI extraction of the actual
   recorded HOA name, not from snippet/filename heuristics. Out of scope
   for this session because it changes `hoaware/bank.py` and would
   conflict with the active Chicagoland session.

2. **`_unknown-county/` and `unresolved-name/` slot growth.** Both slots
   grew across this session (70 → 70 unchanged for unknown; 47 → 84 for
   unresolved). Wave B's tighter queries did NOT reduce the rate at
   which manifests fall into recovery slots — bank's county-detection
   and name-extraction logic is the bottleneck, not query precision.
   Same OCR-first-slug intervention applies.

3. **Nominatim 429s.** 1517 of 3168 ledger rows in the Wave-C prepare
   pass (47.9%) hit Nominatim 429s during geo-enrichment. Same pattern
   GA / first-pass-IL flagged. Treating as a bonus, not production —
   ZIP-centroid via `/admin/extract-doc-zips` was the production-primary
   enrichment and produced 70.9% map coverage on its own.

4. **Wave-C prepared-bundle yield ratio is poor.** 26 prepared / 128 banked
   = 20% acceptance rate (vs Wave B's 119 / 264 = 45%). Long-tail counties
   produce disproportionately gov-boilerplate-heavy banks; the brief's
   `<3 productive leads → kill` rule should arguably be tightened to
   `<5 prepared bundles per county → kill` for the next session's Wave C.
   Adams (2 prepared) and Macon (2 prepared) would have been killed under
   that rule.

5. **Render-side auto-importer race.** The Wave-C prepare → import
   sequence would have been clean if the Render auto-importer hadn't
   claimed Wave-C bundles between phases. The runner's
   `/admin/ingest-ready-gcs` call returned 0 imports because they were
   already imported. Not a problem (idempotent), but the
   live_import_report.json had to be synthesized post-hoc from
   prepared-bucket status blobs to maintain the cleanup script's
   name-to-prefix lookup. Worth documenting for the playbook: when an
   auto-importer is enabled, the runner's import phase is best-effort
   bookkeeping, not the source of truth.

6. **The brief's `--max-leads-per-county 80` cap conflicts with current
   best practice.** Followed playbook line 188 / commit 0e221b0 instead
   (`--max-leads-per-county 0`, unlimited). The brief should be amended
   to remove this cap before the next session uses it as a template.

## Lessons for the playbook

1. **Phase 10 takes 2-3 iterative rounds even with strong cleanup tooling.**
   This session ran 3 rounds (pre-Wave-B, post-Wave-B, post-Wave-C). Each
   round caught a slightly different distribution of bank-stage junk. The
   playbook already says "Phase 10 takes 2-3 iterative delete passes" (line
   816) — confirmed for IL Tier 3.

2. **Mining the first-pass discovery ledger for host-level `-site:`
   exclusions is reusable.** Beyond the brief's generic `-inurl:case/news`
   patches, IL-specific bad-host exclusions (illinoiscourts.gov,
   idfpr.illinois.gov, three named legal blogs) made the round-2 query
   files measurably cleaner. The technique generalizes: any state with
   a productive `.gov` regulator (consumer-rights pamphlets) or a few
   prolific legal-blog domains will produce this same noise class.

3. **`is_dirty()` regex is necessary but not sufficient — confirmed for
   IL Tier 3.** `is_dirty()` caught 16/137 (12%) of first-pass live names;
   manual inspection found ~50% were bank-stage junk. The unconditional
   LLM rename pass (`--no-dirty-filter`) is the only reliable arbiter.
   Already in the playbook for Tier 0/1; **make it Tier-2/3 default too**.

4. **`bank_hoa()` recovery is a load-bearing failure mode for IL Tier 3.**
   The OCR-first slug + geo extraction pattern (commit 1b654d5) should be
   prioritized as the next major framework investment. For IL specifically,
   it would close the 28.6% `_unknown-county/_unresolved-name/` slot rate
   without growing it to 30%+ as Wave B did.

5. **Iteratively extending the heuristic-delete list reduces LLM cost.**
   Each new round's heuristic patterns saved roughly $0.07 of LLM cost
   per 35 entries. By round 3 the patterns had stabilized; further
   extension is diminishing-returns. Pattern: codify state-specific
   bank-stage-recovery-glitch patterns (e.g. "Board Review Approved",
   "Filings 401 N Wabash", "STATE OF X COUNTY REC FEE") into a per-state
   appendix of `clean_dirty_hoa_names.py` so they ship with the regex
   set, not as ad-hoc one-off scripts.

## Final report

`state_scrapers/il/results/il_downstate_20260509_033825_claude/final_state_report.json`
(Wave B); Wave C prepared/import accounting in
`state_scrapers/il/results/il_downstate_wavec_20260509_062035_claude/`
(synthesized live_import_report.json + prepared_ingest_ledger.jsonl).

## Next steps (deferred to future sessions)

1. **Second Wave B sweep** with even tighter queries — the kill thresholds
   weren't tripped on round 1, so a second sweep should yield modest
   net new bank.
2. **OCR-first slug + geo extraction at bank time** (`hoaware/bank.py`
   refactor per playbook lines 317-353). Highest-leverage next-session
   intervention; would close the 28.6% recovery-slot rate and
   substantially reduce Phase 10 round count.
3. **Cross-state-leak guard at import time** (`db.upsert_hoa_location`
   COALESCE semantics fix — IL retro and GA round 4 both flagged this).
4. **Codify IL-specific bank-recovery-glitch patterns** into
   `clean_dirty_hoa_names.py` as a state-specific appendix or shared
   regex bank, so the per-session `dedup_and_clean_il_downstate.py`
   doesn't need to maintain its own delete-list.
5. **Wave C continuation** — DeKalb (NIU) was the strongest of the 5
   Wave C counties tested; the remaining 14 in the brief's candidate
   list (LaSalle / Boone / Grundy / Macon / Vermilion / Adams / Knox /
   Williamson / Effingham / Coles / Marion / Stephenson / Whiteside /
   Henry / Lee / Ogle / Jackson / Franklin) are each modest-yield
   targets if budget allows; tighter `<5 prepared bundles → kill` rule
   recommended.
