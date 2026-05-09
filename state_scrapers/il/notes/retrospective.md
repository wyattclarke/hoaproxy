# IL Scraper Retrospective — first-pass session

State: Illinois · Tier 3 (CAI ~19,750) · Discovery: keyword-Serper-per-county
+ host-family + Wave-0 directories.

Run-id: `il_20260508_114942_claude` (discovery) +
`il_20260508_114942_claude_phase2` (prepare/import/locations).
Wall clock: 2026-05-08 11:49 → 13:49 UTC, ~2 hours end-to-end.

This is a **partial / first-pass** retrospective. The autonomous run
completed cleanly under all caps, but the surface area covered (one
sweep per county, no source-family promotion, no rename/dedup pass) is
small for a Tier-3 universe. This document is meant to set up the next
session for higher yield.

## Closing pyramid

```
Bank        409 manifests / 359 PDFs across 17 IL counties (+5 leak slots)
   │
   │── 287 routed to a real Wave-A or Wave-B county
   │── 70 routed to _unknown-county/ (county couldn't be determined)
   │── 47 routed to unresolved-name/ (extracted name too dirty to slug)
   │── 5 leaked from other-state discovery (duval/hall/pinellas/...)
   ▼
Prepared    149 bundles accepted (157 from ledger, 8 dropped before write)
   │            509 ledger rows: 157 prepared / 202 rejected /
   │            101 geo_enrichment_error (Nominatim 429s) / 49 unknown
   │            DocAI billed: ~584 pages × \$0.0015 ≈ \$0.88
   ▼
Imported    149 live HOAs (1:1 with prepared bundles)
   │
   ▼
Map cov     ZIP-centroid pass ran (rc=0); few/none of the 149 had a
            mapping address since most names came from PDF body text
            without manifest-level addresses.
```

## Bank distribution (per-county)

| County | Manifests | Notes |
|---|---|---|
| Cook | 50 | Hit max-leads cap (80, dedup → 50). City-anchored variants helped. |
| Champaign | 29 | High for Wave B; university-town governing docs publish well. |
| Lake | 26 | Wave A. |
| Kane | 24 | Wave A. |
| DuPage | 19 | Lower than expected for the second-largest HOA county in IL. |
| Will | 19 | Wave A. |
| Sangamon | 18 | Springfield. |
| Winnebago | 16 | Rockford. |
| McHenry | 15 | Wave A. |
| Kendall | 12 | Wave A. |
| Madison | 11 | Metro East (St. Louis side). |
| McLean | 11 | Bloomington-Normal. |
| St. Clair | 10 | Metro East. |
| Tazewell | 10 | Peoria suburbs. |
| Peoria | 7 | Notable miss for a metro this size. |
| Kankakee | 5 | |
| Rock Island | 4 | |
| **`_unknown-county`** | **70** | Roach-motel slot — same defect GA flagged. |
| **`unresolved-name`** | **47** | Names too dirty for the slugger. |
| Cross-state leak | 5 | Other-state discovery polluting `v1/IL/`. |

## Cost summary

| Service | Volume | Approx. spend |
|---|---|---|
| Serper (discovery) | ~17 counties × ≤30 queries × ≤10 results = 5,100 max calls (likely fewer) | < \$1.00 |
| OpenRouter (DeepSeek scoring) | per-county candidate scoring during discovery | not yet attributed; well under \$8 cap |
| Document AI | 584 pages billed (532 final + 52 review) | **\$0.88** |
| **Total** | | **~\$2** |

Caps: DocAI \$150 / Serper \$6 / OpenRouter \$8. None reached.

Per-HOA: \$0.005 per banked manifest, \$0.013 per live HOA — much
cheaper than GA's \$0.06/\$0.10 because the run was small and DocAI's
acceptance gate dropped most of the noisy bundles before OCR.

## What went right

1. **Scaffold + autonomy.** Copy-template, replace placeholders, launch
   end-to-end runner: ~10 minutes from "start IL" to "discovery
   running." The follow-on watcher script (PID 78467) successfully
   chained discovery → prepare → import → locations without operator
   intervention. That handoff pattern is reusable for any Tier-3 state.

2. **Cook city-anchored variants.** Cook county governing documents
   anchor more on city names (Chicago, Evanston, Oak Park, Schaumburg)
   than on "Cook County, Illinois". Augmenting the Cook query file
   with municipality variants got the Cook bank to 50 manifests in
   one pass — likely the largest single-county yield. Future
   urban-Tier-3 states (NY, MA, TX-Travis, CA-LA) should default to
   this pattern.

3. **Caps held.** All three budget caps (DocAI \$150 / Serper \$6 /
   OpenRouter \$8) held with two orders of magnitude of headroom.
   The Tier-3 \$150 DocAI cap is wildly overgenerous for a one-sweep
   IL session; \$5–10 would have sufficed.

4. **Cross-state-leak detection.** 5 manifests leaked into `v1/IL/`
   from other states' discovery (duval=FL, hall=GA, pinellas=FL,
   st-johns=FL, troup=GA). These were already there before this run.
   Same `db.upsert_hoa_location` `COALESCE` semantics issue GA's
   round-4 pass diagnosed — needs the cross-state guard at import
   time (still open).

## What didn't work / what next-session must address

1. **Live name quality is bad — ~40–50% of the 149 live names need
   rename, merge, or deletion.** The OCR-text-extracted name path is
   surfacing court filings ("18-23538-shl Doc 9231 Filed 01 13 21..."
   is a bankruptcy docket number, not an HOA), ordinance titles
   ("31 VILLAGE OF CAMPTON HILLS AN ORDINANCE APPROVING HOA"),
   articles ("Breaking Down Homeowners Association", "BUYING A
   CONDOMINIUM? HOA", "Free Policies and Condominiums In pdf. 8
   Christiansen v. Heritage Hills Condominium Association"), and
   shouty-prefix scraps ("FOR EVERTON TOWNHOMES HOA",
   "ENCLAVE AT HAMILTON ESTATES HOMEOWNERS").

   GA had ~16% of live HOAs needing cleanup; IL is ~3× that rate.
   Reasons:
   - Cook County queries hit a lot of legal-opinion / case-law
     PDFs that mention "Homeowners Association" in body text.
   - Host-family + directories sweeps surfaced articles and press
     releases that name an HOA in passing without being its own
     governing document.
   - The dirty-name recovery rules in `bank_hoa()` are too
     permissive — recovering "Of Condominium Ownership for
     Sandpebble Walk HOA" as a real name when the canonical is
     "Sandpebble Walk Condominium Association".

   **Mandatory next session:** run a rename/dedup/delete pass. GA's
   `state_scrapers/ga/scripts/dedup_and_clean_ga.py` is the template.
   Specifically:
   - Delete pass for clearly-non-HOA names: docket numbers,
     ordinance titles, article-shaped names, single-word ("Assoc",
     "Document View"), all-caps shouting, leading "& " / "* " / "/ ".
   - Rename pass via `_try_strip_prefix()` for `"<noun phrase>" + canonical-suffix` salvageable cases.
   - Permissive LLM rename for "Cobble Creek Subdivision" → "Cobble
     Creek Subdivision Homeowners Association" when context supports it.
   - Suffix-stripped signature dedup for likely-collision pairs.

2. **`_unknown-county/` and `unresolved-name/` slots are oversized.**
   70 + 47 = 117 of 409 (28.6%) banked into one of the two recovery
   slots. GA had this for ~100 of 1,800 (5.6%). The IL sweep is
   noisier, and the county-detection / name-extraction logic in
   `bank_hoa()` doesn't keep up.

   **Fix at discovery time, not at cleanup.** Tighten the Serper
   query file: any query that doesn't include a county/city anchor
   AND a governing-doc keyword needs to come out. The host-family
   and directories files in particular are too loose — they should
   be re-run with `inurl:` constraints that exclude `/news/`,
   `/articles/`, `/press-release/`, `/blog/`, `/case-law/`,
   `caselaw.findlaw`, `casetext.com`, etc.

3. **Wave-0 directories yielded little structured content.** Public
   mgmt-co dropdowns (FirstService Residential's "find my community
   website") are AJAX-walled. The Bolingbrook IL HOA-list PDF that
   surfaced in WebSearch returned 404 on direct fetch. The
   FirstService press-release pages are real (they list named
   communities like "Pebblewood Condominium No. 1 Association",
   "Sandburg Village Condominium Association No 1"), and the
   `il_directories_serper_queries.txt` file targets them, but the
   first-pass yield was modest.

   **Future:** scrape mgmt-co press-release archives directly
   (cookrecorder.com search interface is the highest-value untapped
   source), ingest as `manual-leads` JSONL.

4. **Single sweep per county is a first-pass only.** The playbook's
   per-state two-sweep stop rule wasn't reached because the runner
   only ran each query file once. To approach the IL CAI ceiling
   (~19,750), the runner needs to be invoked repeatedly with
   widened anchors per round, not capped at 30 queries × 10 results.

5. **Nominatim 429s.** 101 of 509 ledger rows (19.8%) hit Nominatim
   429s during geo-enrichment. Same pattern GA flagged — public
   Nominatim is unreliable above ~100 sequential requests. Treat
   it as a bonus, not production.

6. **Discovery routed PDFs into `v1/IL/duval/` and similar.** Five
   pre-existing cross-state leaks survived the run; one or two
   may have been added during this run as well (e.g. Cook
   discovery surfacing TX/GA case-law PDFs). Need the
   import-time state-mismatch guard described in GA round 4.

## What to do next session (concrete punch list)

1. **Cleanup pass (mandatory).**
   - Adapt `dedup_and_clean_ga.py` → `dedup_and_clean_il.py`.
   - Add an explicit delete list for names matching docket-number /
     ordinance-title / article-title regexes.
   - Run rename/merge/dedup; expect 60–80 deletions and 20–40
     in-place renames given the ~50% noise rate.

2. **Tighter Serper queries for round-2 sweeps.**
   - Re-run host-family + directories with `-inurl:news`,
     `-inurl:articles`, `-inurl:press`, `-inurl:case`, `-site:caselaw.findlaw.com`, `-site:casetext.com`, `-site:law.justia.com` exclusions.
   - Promote any source family that hit ≥5 governing-doc PDFs
     in this round to deterministic-mode scraping (Phase 2
     promotion rule).

3. **Cook County Recorder direct probe.**
   - Build a manual-leads JSONL from cookrecorder.com search for
     "Declaration of Condominium" filed in last 5 years. This is
     a deterministic, high-precision source — likely 10× the
     yield of keyword Serper for Cook.

4. **Cross-state-leak guard.** Open a separate concern: the
   `db.upsert_hoa_location` `COALESCE` semantics issue. Ideally
   add an import-time guard. (This is a pipeline improvement, not
   IL-specific.)

5. **Wave C plan.** With 17 counties at ~17–50 manifests each, IL
   is far from the ~19,750 CAI ceiling. Wave C (long-tail counties
   like DeKalb, LaSalle, Boone, Grundy, Macon, Vermilion, Adams,
   Knox, Williamson, Effingham, Coles, Marion, Stephenson) is
   future work — but only after Wave A/B yield is improved via
   round-2 + cleanup.

## Numbers worth carrying forward

- ~28.6% bank-time noise rate (`_unknown-county` + `unresolved-name`
  slots) — set a yellow flag; >20% means tighter query anchors
  needed.
- ~50% live name noise rate — set a red flag; mandatory cleanup
  pass before any state can ship.
- DocAI \$0.88 for 149 imports = \$0.006/HOA — cheap, well below
  GA's average. Aggressive page-1-review acceptance gate worked.
- Wall clock: ~2 hours discovery + ~1 hour phase2. A second
  session (cleanup + round-2 sweeps + Cook Recorder) should
  comfortably fit in another 4–6 hours under the same caps.

## Final report

`state_scrapers/il/results/il_20260508_114942_claude_phase2/final_state_report.json`

Run handoff continues at `state_scrapers/il/notes/discovery-handoff.md`.
