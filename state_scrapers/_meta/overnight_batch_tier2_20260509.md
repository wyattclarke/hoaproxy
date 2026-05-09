# Tier-2 Batch 2026-05-09 — Meta Retrospective

10 fresh Tier-2 states (MA, MD, NJ, PA, OH, MI, MN, MO, WI, OR) banked,
ingested, cleaned, and deployed in one session. The bank-coverage check
that prompted this batch showed all 10 below 30 manifests; after this
run, all 10 are between 238 and 527 manifests with 30–158 genuine live
HOAs each.

## Final Counts

| State | Bank | Live | Map | Cost |
|---|---:|---:|---:|---:|
| MA | 238 | 33 | 3 | ~$1.00 |
| MD | 387 | 73 | 18 | ~$0.80 |
| NJ | 402 | 30 | 9 | ~$0.70 |
| PA | 402 | 63 | 17 | ~$0.90 |
| OH | 527 | **158** | **57** | ~$1.20 |
| MI | 448 | 107 | 28 | ~$0.90 |
| MN | 259 | 39 | 4 | ~$0.60 |
| MO | 411 | 123 | 11 | ~$0.90 |
| WI | 430 | 114 | 24 | ~$0.85 |
| OR | 480 | 121 | 26 | ~$0.95 |
| **Total** | **3,984** | **861** | **197** | **~$8.80** |

## Headline

**861 genuine live HOAs banked, ingested, cleaned, and mapped across 10
Tier-2 states for ~$9 of DocAI** — about $0.010 per genuine HOA. The
playbook's Tier-2 OCR budget was $40-75 per state ($400-750 total); we
came in 50× under that envelope because the keyword-Serper pattern only
OCRs the page-1 of each candidate (not the playbook's old SoS-bulk-
extracted universe).

## Highlights

- **OH (159 → 158 live, 57 map points)** is the standout. Cuyahoga
  (Cleveland) + Franklin (Columbus) + Hamilton (Cincinnati) + Summit
  (Akron) all dense; Mason/Lebanon (Warren) + Delaware (Columbus N)
  added growing-suburb HOAs. Highest map rate of the batch.
- **MD (73 live, 18 map)** is unusually well-mapped because DC-suburb
  counties (Montgomery + Prince George's + Howard) have polished GIS
  layers Nominatim resolves to polygons.
- **OR (121 live, 26 map)** caught Bend (Deschutes) ski-resort HOAs and
  Hood River wine-country condos, plus Portland metro density.
- **MO + WI + OR all hit 100+ live** — Tier 2 keyword-Serper at
  10–14 counties yields 80-160 genuine HOAs per state when Render
  doesn't drop imports (see "Render fragility" below).

## Render fragility — observed twice in this batch

Both MN and MO had their initial `/admin/ingest-ready-gcs` calls fail
with HTTP 502 because Render was hammered by a concurrent state's
import phase. Symptoms:

- The runner's `import_ready()` function gets one 502 response and
  exits the loop (it doesn't retry).
- The `live_import_report.json` shows `total_imported: 0` despite the
  bundles being prepared in GCS.

**Workaround applied:** post-runner re-import loop with 6× retry +
exponential backoff per call. After ~5 minutes of Render-load relief,
the imports succeeded:

- MN: 97 imports recovered (pre-cleanup live = 90)
- MO: 166 imports recovered (pre-cleanup live = 197)

**Long-term fix needed (not done this session):** the runner template's
`import_ready()` should retry on 5xx with backoff, same shape as the
phase10_close.py hard-delete loop.

## Retrospective vs. May 2026 Tier-0 batch

Tier-2 was not the cost monster the playbook framed it as. Comparison
on per-state shape:

| Metric | May 2026 Tier-0 (10 states) | Tier-2 batch (10 states) |
|---|---:|---:|
| Wall time / state | 30–90 min | 1.5–3 h |
| Bank manifests | 79–394 | 238–527 |
| Live HOAs | 7–153 | 30–158 |
| DocAI / state | $0.40–$4.11 | $0.60–$1.20 |
| Map rate (avg) | 6.8% | 22.9% |

Tier-2 doubled the wall-time per state but hit a much higher map rate
(22.9% vs 6.8%) because metro-county HOAs land in well-mapped
Nominatim/Census polygons. Worth the 2× wall-clock cost.

## What worked

1. **Pair-of-2 batching kept Render alive.** Five rounds × 2 states =
   ~12h wall clock total. Trying 4-at-a-time would have caused
   sustained 502s (per the pair MN/MO that hit the cap when OH/MI's
   import was running concurrent).
2. **Resort-county must-include paid off.** OH didn't have a major
   resort, but PA Monroe (Pocono), MI Grand Traverse + Emmet, WI Door +
   Walworth (Lake Geneva), MO Camden (Lake of the Ozarks), OR Deschutes
   (Bend) + Hood River all contributed disproportionately to their
   state's yield. NJ Atlantic + Cape May caught Jersey Shore condos
   that wouldn't have surfaced from Bergen/Essex sweeps.
3. **`scripts/phase10_close.py` (Phase A of this session) was a 10×
   speedup.** Each state's cleanup was ~3 min of operator time vs the
   30+ min iterative manual cleanup pattern from the May 2026 batch.
4. **`--max-leads-per-county 0` (the cap removal earlier this session)**
   meant high-yield counties (Cuyahoga, Franklin OH, Camden MO,
   Multnomah OR) banked 50–80+ candidates each instead of getting
   truncated at 80.

## What didn't work

1. **Runner's import phase isn't 502-resilient.** MN and MO needed
   manual re-import. Long-term fix: add retry-on-5xx to
   `run_state_ingestion.py::import_ready()`.
2. **NJ Atlantic / Cape May resort sweeps yielded less than expected.**
   Expected ~30+ shore condo associations from Atlantic City and Cape
   May; got modest counts. Possible reason: NJ shore HOAs use third-
   party-managed websites (Associa, FirstService) that bury PDFs behind
   logged-in pages.
3. **MN's St. Louis County (Duluth) and Stearns (St. Cloud) came up
   thin.** Possible reason: small towns + lower-density suburbs don't
   match the keyword-Serper pattern as well; would need owned-domain
   crawls to extract more.

## Cumulative session ledger

This session (Phase A + B + C):

| Phase | Outcome | Wall time | Cost |
|---|---|---|---:|
| A | Build `scripts/phase10_close.py` (regex set + dedupe-merge + retrospective preservation) | ~30 min | $0 |
| B | Second pass on May 2026 batch (10 states); +34 supplemental counties for SD/ND/AR/MS/WV/NM | ~6 h | ~$8 |
| C | Fresh Tier-2 batch (10 states); 119 new counties scaffolded | ~12 h | ~$9 |
| **Total** | **20 states processed (10 second-pass + 10 fresh)** | **~18 h** | **~$17** |

## Final session totals (combined with May 2026 first pass)

| | Live | Map | Bank manifests |
|---|---:|---:|---:|
| May 2026 batch (Tier 0/1, 10 states) | 598 | 50 | 2,790 |
| Phase C Tier-2 batch (10 states) | 861 | 197 | 3,984 |
| **Combined** | **1,459** | **247** | **6,774** |

20 states out of 51 jurisdictions covered. The next obvious work is the
Tier-3 set (AZ, CO, IL, MA, NC, NY, TX, WA) — the playbook says these
need operator-supervised plans rather than autonomous batches. NC has
documented aggregator sources (Closing Carolina, CASNC) that could
plausibly run autonomously; the rest probably warrant the human in the
loop.

## DocAI ledger

Cumulative GCP DocAI billing: **$181.03** (was $146.88 at May 2026 start;
session delta $34, of which ~$17 is Phase A/B/C and ~$17 was the May
2026 first pass + cap fix). $419 of headroom remains against the $600
GCP project cap.
