# HERE Geocoder Enrichment Pass 2026-05-09

After Phase A/B/C left map coverage at ~17% across 20 states (much lower
than the playbook's ≥70–80% target), this pass extracts addresses from
indexed OCR text and geocodes via the HERE API to backfill locations.

## Result

| | Before | After | Δ |
|---|---:|---:|---:|
| Total live HOAs | 1,459 | 1,456* | (cleanup pruned 3) |
| Mapped (lat/lon visible) | 247 (17%) | ~382 (26%) | **+135** |
| New address-quality pins | — | ~150 | (street-level) |
| New place-centroid pins | — | ~80 | (POI/subdivision) |
| New zip-centroid pins | — | ~30 | (ZIP point) |

*Approximate; live counts shifted slightly during the pass due to
phase10_close cleanups running in parallel.

## Per-state rates (post-enrichment)

| State | Live | Map | Rate |
|---|---:|---:|---:|
| MA | 33 | 25 | **76%** |
| WV | 16 | 8 | **50%** |
| SD | 12 | 6 | **50%** |
| MN | 39 | 18 | 46% |
| OH | 158 | 70 | 44% |
| NM* | 60 | 23 | 38%* |
| NJ | 30 | 11 | 37% |
| PA | 63 | 21 | 33% |
| AR | 36 | 11 | 31% |
| MD | 73 | 22 | 30% |
| MI | 107 | 32 | 30% |
| OR | 121 | 31 | 26% |
| MO | 123 | 32 | 26% |
| AK | 31 | 7 | 23% |
| WI | 114 | 25 | 22% |
| MS | 42 | 9 | 21% |
| ND | 20 | 3 | 15% |
| NE | 106 | 14 | 13% |
| OK | 126 | 8 | 6% |
| MT | 146 | 6 | 4% |

*NM rate-limited during final tally; ~38% from earlier readings.

## Why some states stayed low

**OK and MT (4-6%)** are the worst offenders — both have many HOAs whose
OCR text is dominated by plat-reference covenants ("Lot 5, Block 2 of
the Foo Subdivision as recorded in Plat Book X, Page Y") without modern
street addresses. HERE can't geocode plat references; would need either
an LLM-based extractor that resolves the subdivision against a county
GIS layer, or owned-website crawling for the HOA's modern contact info.

**NE and ND (13-15%)** have similar plat-prevalence + many small
subdivision HOAs that share city centroids with no distinct street
boundary.

**MA, WV, SD (50-76%)** were lifted highest because they had the
clearest street-address mentions in their OCR text + small enough
universes that HERE could resolve most candidates.

## Mechanism

`scripts/enrich_locations_from_ocr_here.py` (484 lines, committed in
this session):

1. List unmapped HOAs via `/hoas/summary` ∖ `/hoas/map-points`. The diff
   catches `city_only`-quality entries with lat/lon set but hidden from
   the map — a class my initial implementation missed.
2. Per HOA: fetch the searchable HTML page for each of its (up to 5)
   documents, strip tags, accumulate up to 8KB of OCR text.
3. Extract candidate query strings via four patterns, in priority order:
   a. Street + city + state + ZIP (e.g., "100 Main St, Cary, NC 27513")
   b. HOA-name + manifest-city + state-name (subdivision search)
   c. Street + manifest-city + state (when only the street is in OCR)
   d. Most-frequent ZIP + state-name (coarse fallback)
4. Query HERE (`geocode.search.hereapi.com/v1/geocode?in=countryCode:USA`)
   for each candidate, in-state filter the results, take the first match
   inside the state bbox.
5. Map HERE `resultType` → location_quality:
   - `houseNumber|intersection|street` → `address`
   - `place` → `place_centroid`
   - `postalCodePoint` → `zip_centroid`
   - `locality|administrativeArea|...` → SKIP (would create stacked
     city-centroid pins, which the playbook explicitly forbids)
6. POST batch to `/admin/backfill-locations`.

## Cost

- **HERE API calls**: ~3,000 (well under the 30k/month free tier).
- **Local cache**: `data/here_geocode_cache.json` short-circuits repeat
  queries on re-runs; the second pass after the unmapped-detection bug
  fix was mostly cache hits.
- **DocAI/OpenRouter/Serper**: $0 incremental — this is a pure backfill
  pass against already-OCR'd text.

## Operational notes

- **Render fragility surfaced again** at peak parallelism (20 enrichers
  + simultaneous `/hoas/summary` fetches). The script stays light but
  bursts of `/admin/backfill-locations` post-runs can still trigger 502s
  on the verification queries. Keep concurrent enrichers at ≤4 in
  future runs.
- **The `--cache` arg is critical**. Re-running on a state hits HERE
  only for new candidates; the rest are filesystem reads.
- **`fetch_unmapped_hoas` bug**: initial version only checked for
  null lat/lon; missed entries with `location_quality=city_only` that
  had real lat/lon set by the runner's location_enrichment step. Fixed
  mid-pass by computing `summary ∖ map-points`.

## What would push coverage to 70%+

The remaining gap is structural, not engineering:

1. **LLM-based plat extractor** that reads "Lot 5, Block 2 of the Foo
   Subdivision as recorded in Plat Book X, Page Y" and resolves Foo
   Subdivision against the relevant county's GIS layer for a polygon.
   Each county has one; would need ~50 separate ingestion paths.
2. **Owned-website crawling** for the HOA's "About Us" / "Contact"
   pages where modern street addresses live (the recorded covenants
   often predate the HOA's web presence).
3. **Manual operator review** for the top metros — Cuyahoga, Franklin
   OH, Maricopa-equivalents — where the marginal yield of one operator
   hour is high.

None of these are autonomous-batch shape. Flagged for a future engineering
project, not a one-shot script.

## Artifacts

- `scripts/enrich_locations_from_ocr_here.py` — the canonical enricher
- `data/here_geocode_cache.json` — response cache (gitignored unless
  small enough to track)
- `/tmp/here_*.log` and `/tmp/here2_*.log` — per-state run logs
  (transient; not tracked)
