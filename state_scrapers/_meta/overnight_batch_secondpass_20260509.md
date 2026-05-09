# Second-Pass Batch 2026-05-09 — Meta Retrospective

Follow-up to `overnight_batch_20260508` covering the same 10 states with:
1. The `--max-leads-per-county` cap removed (default `0`/unlimited).
2. County expansion to ~80% population coverage (8 new counties for SD,
   5 for ND, 6 for AR, 6 for MS, 4 for WV, 5 for NM = 34 new counties).
3. Unified `scripts/phase10_close.py` cleanup with cumulative regex set,
   dedupe-merge, and doc-filename audit (Phase A of this session built it
   on top of the existing scaffolding).
4. AK/NE/OK/MT got phase10_close v2 cleanup only (no new discovery; the
   May 2026 first pass had already covered ~80%+ population for them).

## Per-State Delta

| State | First-pass live | Second-pass live | Δ | Notes |
|---|---:|---:|---:|---|
| SD | 7 | **12** | +5 | 8 new counties added; Watertown/Mitchell/Pierre yielded |
| ND | 12 | **20** | +8 | Ward (Minot) added — that was the obvious miss; Williams/Morton also helped |
| AK | 33 | **31** | -2 | Just cleanup; no new boroughs needed (8-borough coverage was already ~93% pop) |
| AR | 38 | **36** | -2 | 6 new counties; supp added little new beyond cleanup |
| MS | 56 | **43** | -13 | Cleanup-driven; phase10_close caught 13 residuals the manual pass missed |
| WV | 22 | **16** | -6 | 4 new counties (incl. Greenbrier/Pocahontas resort); cleanup tighter |
| NE | 109 | **107** | -2 | Cleanup only; supplemental not needed (8-county coverage was ~85% pop) |
| NM | 79 | **60** | -19 | 5 new counties; LLM rename pass + new regex set pruned residuals heavily |
| OK | 134 | **131** | -3 | Cleanup only |
| MT | 153 | **150** | -3 | Cleanup only |
| **Total** | **643** | **606** | **-37** | |

## Read this delta carefully

The headline `-37` is misleading. Two effects stacked:

1. **County expansion produced clear wins** for the under-covered states:
   SD +5, ND +8 (with Ward/Minot) are real new genuine HOAs.
2. **Tighter cleanup pruned the over-counted states.** NM lost 19, MS lost
   13, WV lost 6 — all because the unified `phase10_close.py` caught
   residual junk (recording stamps, "Of " fragments, plat-page extracts,
   utility/news source-URL hits) that the iterative manual cleanup during
   the May 2026 batch had missed. The data is cleaner now, not smaller in
   any meaningful sense. A future operator can verify by re-running
   phase10_close on any first-pass-only state and seeing similar shrinkage.

The state-by-state map_points + chunk_count numbers (in each state's
`retrospective_auto.md`) confirm that the surviving entries are
higher-quality on average.

## Cost

Phase B incremental DocAI: ~$5–8 (just the new-county manifests + a few
re-OCR passes for entries the regex caught).
OpenRouter: ~$1.50 (LLM rename runs over the 10 states).
Serper: ~$0.20 (34 new query files × ~16 queries each).
**Phase B total: ~$8 incremental.**

Cumulative GCP DocAI billing remains around $170–175 against the $600
project cap.

## Tooling outcomes

- **`scripts/phase10_close.py` v2**: now bundles LLM rename + null-canonical
  delete + cumulative-regex delete + doc-filename audit + dedupe-merge +
  bbox audit + retrospective scaffold + location backfill into a single
  command. Preserves manually-curated `retrospective.md` (writes
  `retrospective_auto.md` instead). Per-state cleanup is now ~3 minutes of
  operator time, down from 30+ on the original batch.
- **Cap removal is permanent** in the runner template + all 25 state
  runners + the underlying `scrape_state_serper_docpages.py`. Default is
  `0`/unlimited; `--max-leads N` with N>0 now requires explicit operator
  opt-in.

## What didn't work

- **NM "Of " fragments leaked again on the first supplemental run.** The
  bank-stage `is_dirty()` regex still doesn't catch them at probe time,
  so the prepared bundle imports them and the LLM rename has to clean
  them post-import. This is a known issue documented in the playbook;
  the OCR-first slug+geo pipeline (memo'd but not implemented) is the
  proper fix.
- **Render flakiness during the second-pass run** caused a couple of
  503/502s on `/hoas/summary` calls. The phase10_close hard-delete loop
  has 6× retries built in and recovered; the live verification queries
  used in this retrospective sometimes timed out and were retried.

## Recommendations for the next batch

The "next 10" recommendation list (DC/HI/IA/ID/KY/AL/LA/NV/UT/ME) is
running in a parallel session per the kickoff prompt I generated. The
fresh Tier-2 batch (MA/MD/NJ/PA/OH/MI/MN/MO/WI/OR) is starting in this
session as Phase C. Both should benefit from the same canonical defaults:
- 10-county minimum, ~80% pop coverage, resort-county inclusion
- `--max-leads-per-county 0` (default, unlimited)
- `phase10_close.py --apply` per state for cleanup
- Hard-delete (no `[non-HOA]` tagging)

## Artifacts

- `state_scrapers/{sd,nd,ar,ms,wv,nm}/results/{state}_supp_*_claude/` —
  supplemental run artifacts
- `state_scrapers/{ak,ne,ok,mt}/results/{state}_phase10_v2_*/` — cleanup-
  only artifacts
- `state_scrapers/{state}/notes/retrospective_auto.md` per state — auto-
  generated cleanup summary alongside the preserved manual retrospective
