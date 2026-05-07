# GA Scraper Retrospective

State: Georgia · Final live numbers (2026-05-07): **1,139 HOAs · 2,055 docs ·
57.5k chunks · 60.2% mapped**.

This is a write-up of how the GA pipeline actually ran, what worked, what
didn't, and roughly what each part cost. It is meant for future state
runners to copy the wins and avoid the time sinks.

## Shape of the run

GA was the largest state attempted to date. Discovery, prepare, import,
and cleanup all took meaningful wall-clock time, and the bank/prepared/live
pyramid had real fall-off at every step.

```
Bank        1,800 manifests / 2,688 PDFs / 105 GA counties
   │
   ▼
Prepared    1,340 bundles / 2,074 docs accepted (737 rejected, 26.2%)
   │            │── filename precedence + page-1 review caught a real
   │               batch of plat/lot-map/phase-doc false negatives that
   │               would otherwise have been dropped on filename alone.
   ▼
Imported    1,289 bundles → 1,153 live HOAs (some merged on import)
   │            │── 51 prepared bundles marked failed: 50 _unknown-county
   │               with no usable address, 1 'Invalid HOA name'.
   ▼
Cleaned     1,139 live HOAs (after rename + merge of dirty-name rows)
              686 mapped (60.2%)
```

## What went right

1. **County-by-county Serper sweeps** as the main discovery harness. The
   per-county anchor (`"<county> County" "Georgia"` + governing-doc
   keywords) gave high precision. Multi-state name overlap in counties
   like Bulloch, Effingham, and Fulton was real but manageable; the
   cross-state re-routing logic inside `clean_direct_pdf_leads.py`
   meant any incidental hits in TX/FL/SC ended up correctly banked
   under their own state instead of polluting GA.

2. **Parameterized cleaner.** Once `BLOCKED_HOST_RE`, `OUT_OF_STATE_RE`,
   and `detect_state_county()` accepted the target state as input
   instead of hard-coding it, the same script ran for GA and got the
   "free wins" of populating 40 other state buckets along the way.

3. **Filename-precedence override** for the prepare worker. Stale
   text-classifier output ("junk") routinely overrode obvious
   filenames like `Fox-Lake-Lot-Map.pdf` or `Dunham_Marsh_phase_9.pdf`.
   Adding a precedence rule that lets `classify_from_filename()` win
   when the text classifier returns junk/unknown reclaimed a
   meaningful set of plats, lot maps, and phase documents that were
   being silently dropped.

4. **Three-tier map cleanup** post-import was the right shape:
   - Pre-import polygons via Nominatim during prepare (sparse, ~14%)
   - Post-import OCR ZIP centroids via Census ZCTA (cheap, +218)
   - Post-import Serper Places (highest yield, +268)
   This staged the high-cost calls behind cheap deterministic ones.

5. **`/admin/rename-hoa` with merge-on-collision.** Adding a small
   admin endpoint that handles either UPDATE-in-place or merge
   (move documents, re-touch chunks.embedding so the chunk_vec
   partition refreshes, drop dup paths, move location iff target has
   none, delete source) made it safe to clean 16% of live names with
   a single LLM-driven sweep. 106 in-place renames + 12 merges, 0
   errors. Future states will reuse this directly.

## What didn't work, or had to be reworked

1. **Statewide Serper sweeps** drift toward low-precision noise. Every
   time the runner skipped the explicit per-county anchor the lead
   pile got polluted with national HOA-news content. The playbook now
   bans bare statewide sweeps for this reason.

2. **Architectural-keyword anchors** (e.g., `"architectural review"`
   without `"declaration"` or `"covenants"`) returned a lot of
   voluntary neighborhood and civic-association noise. The fix was to
   anchor every architectural query on a real governing-doc word so
   only mandatory deed-restricted associations came through.

3. **Junk HOA names from OCR text.** Whenever discovery extracted a
   "name" from the body of a scanned PDF instead of the manifest's
   own `name` field, the result was often a sentence fragment like
   `"AND OF , ... Lochwolde Homeowners Association"`. The bank
   accepted these (correctly — bank is permissive), but they
   propagated through prepare and into live. 16% of live GA HOAs had
   names like this. The post-launch rename pass is what brought
   coverage back.

4. **Public Nominatim** is unreliable above ~100 sequential requests.
   It served some lookups, then started returning 429s for 15+
   minutes even with a >1 s delay. We treat it as a bonus, not a
   production geocoder.

5. **`_unknown-county/` is a roach motel.** Manifests with no county
   slug get bucketed there. If the same SHA also appears under a
   real-county manifest, the prepare worker may pick up the
   `_unknown-county` side first and mark the real-county side as a
   prepared duplicate. When `_unknown-county/` is later marked
   `failed` before live import (correctly — these can't be mapped),
   the doc is lost. This bit us for 11 PDFs across 8 GA HOAs (Iron
   Gate / Bulloch, Martin's Landing / Fulton, etc.). Recovery is
   mechanical (delete the failed `_unknown-county` bundles, re-run
   prepare on the affected counties with `--skip-live-duplicate-check`)
   but easy to miss.

6. **Live secret drift.** Render's UI rotates `JWT_SECRET` invisibly
   when env vars are touched, and `settings.env` keeps the old value.
   Any admin POST should resolve the bearer at runtime via the Render
   API (`/v1/services/<id>/env-vars`), not the local file. Both the
   import loop and the rename driver do this and that's why they
   worked across deploys.

## Cost per HOA

Counted across the full pipeline (discovery → prepare → import → cleanup),
the externally-billed components were Serper, OpenRouter, and Document AI.
Approximate figures from logs and `api_usage_log`:

| Service | Volume | Approx. spend |
|---|---|---|
| Serper (discovery) | ~15,840 queries logged in audit.jsonl across per-county / host-family / legal-phrase / find-owned passes | ~$15.84 |
| Serper (find-owned + map cleanup) | ~9,500 queries across the find-owned rounds + 3 Serper Places passes | ~$9.50 |
| OpenRouter (discovery validation) | per-county DeepSeek scoring + LLM-assisted backfills | ~$17.69 |
| OpenRouter (name cleanup) | 180 LLM calls (DeepSeek primary, Kimi fallback) | ~$0.20 |
| Document AI (prepare worker OCR) | 5/6 + 5/7 runs: 43,505 pages × $0.0015/page | ~$65.26 |
| **Total** |  | **~$108.50** |

Per-HOA denominators:

| Denominator | Count | $/unit |
|---|---|---|
| Raw bank manifests | 1,800 | **$0.060** |
| Prepared bundles | 1,340 | $0.081 |
| Imported bundles | 1,289 | $0.084 |
| Live HOAs (final) | 1,139 | **$0.095** |

So roughly **$0.06 per banked HOA, $0.10 per live HOA** for GA, all-in.
DocAI is the dominant single line item (~60% of spend); Serper and
OpenRouter combine to the remaining 40%. OpenAI embeddings during
import are paid by the live API and are tiny (~$5 over the same window
across all states).

The DocAI cap formula `max($5, $0.03 × manifest count)` produced a
$54 budget for 1,800 manifests; actual spend was $65 because the
post-hoc `_unknown-county/` recovery pass needed extra OCR. That
overrun is small and the formula is still the right starting target.

## Numbers worth carrying forward

- 26.2% reject rate at the OCR/filter step is healthy. Top reject reasons
  for GA: duplicate (347), junk (128), low_value (114), unsupported
  category (96), pii (46), page cap (6).
- ~14% of live HOAs need post-launch name cleanup. 66% of those are
  recoverable via the LLM extractor (current numbers: 180 dirty → 118
  high-confidence canonicalizations). Budget for it as a normal
  closing step, not an afterthought.
- Pre-import map coverage was 14%; post-import cleanup brought it to
  60%. Most of that lift came from Serper Places, not Nominatim. For
  any state larger than ~500 HOAs, plan for cleanup to be the bulk of
  the mapping work.
- 60% map coverage at close was the realistic ceiling for GA given the
  garbage-name tail. Future states with cleaner discovery (SoS-first or
  a strong aggregator) should be able to hit 80%+.

## Closing pyramid for GA

```
2,688  PDFs banked
2,074  PDFs accepted by prepare/OCR (73.8%)
2,055  PDFs live (after dedupe on import)
1,139  HOAs live
  686  HOAs on the map (60.2%)
```

Final report: `state_scrapers/ga/results/final_state_report.json`.
Full handoff narrative: `docs/ga-discovery-handoff.md`.
