# Wyoming HOA Scrape Retrospective

Run id: `wy_20260507_225444_claude` (Tier 0, agent: claude).
Wall time: ~2 hours end-to-end (preflight 22:54 UTC → import-finished 00:39 UTC
→ name cleanup + verification 00:53 UTC).

## Final Counts

| Metric | Value |
|---|---:|
| Raw bank manifests | 314 |
| Prepared bundles | 141 |
| Live HOA profiles | 133 |
| Live documents | 137 |
| Live chunks | 7,019 |
| Map points | 48 |
| Map rate | 36% |
| Out-of-state map points | 0 |
| Zero-chunk documents | 0 |
| Failed bundles | 0 |
| `budget_deferred` candidates | 0 |
| Names auto-cleaned | 7 renamed + 1 merged |

## Cost Per HOA

DocAI is the dominant cost as expected from the cross-state lessons.

| Channel | Spend | Per live HOA |
|---|---:|---:|
| Google Document AI | $2.20 | $0.0165 |
| OpenRouter (DeepSeek + Kimi) | ~$0.45 | $0.0034 |
| Serper | ~$0.05 | $0.0004 |
| **Total** | **~$2.70** | **~$0.020** |

DocAI: $0.0015/page × 1,467 pages (1,367 final-extract + 100 page-one review).
OpenRouter: estimated from `data/model_usage.jsonl` since 22:50 UTC; 725 DeepSeek
calls + 51 Kimi fallback calls (mostly `doc_classifier.classify_with_llm`).
Serper: 9 county query files × ~17 queries × ~$0.30/1000 searches.

All three channels finished well under the per-tier caps ($5 DocAI, $5
OpenRouter, $3 Serper).

## Discovery Branch Pivot

**SoS-first was the recommended primary branch but failed at preflight.**
`https://wyobiz.wyo.gov/Business/FilingSearch.aspx` returns an F5 BIG-IP / TSPD
CAPTCHA challenge for headless requests, the same blocker as Vermont
(`bizfilings.vermont.gov`). No public bulk-download or SODA-style export of
the WY business registry exists. Pivoted to keyword-Serper anchored on the
9 HOA-bearing counties (skipped 14 rural ranchland counties by design — the
precision/cost ratio of probing Niobrara, Crook, Weston etc. would be poor).

## Productive Sources

| County | Bank manifests | Notes |
|---|---:|---|
| Teton | 47 | Jackson Hole resort condos, as predicted; dominant county |
| Lincoln | 33 | Star Valley resort condos (Jackson-adjacent) |
| Sheridan | 32 | Foothill ranches + planned communities |
| Fremont | 32 | Wind River resort + Lander/Riverton |
| Albany | 31 | Laramie city + university |
| Natrona | 29 | Casper |
| Park | 27 | Cody/Yellowstone gateway |
| Laramie | 19 | Cheyenne (lower than expected — keyword sweep dry on planned-community CC&Rs) |
| Sublette | 18 | Pinedale + Big Piney ranches |
| `_unresolved-name/` | 44 | Bank-stage name-quality gate triggered |

The 9-county scope was the right shape: no county returned <15 manifests, and
no skipped county is plausibly producing real HOA documents that we missed.

## Main False-Positive Classes

Page-one OCR review caught the bulk; no class needs deterministic deny-list
follow-up.

| Reject class | Count | Notes |
|---|---:|---|
| `unsupported_category:unknown` | 24 | Mostly miscellaneous PDFs that didn't match any governing-doc shape after page-one OCR |
| `junk:unrelated` | 22 | Newsletters, marketing PDFs, planning brochures |
| `low_value:financial` | 15 | Audit/budget PDFs from city or county sites |
| `junk:government` | 12 | County land-use plans, town ordinances |
| `junk:court` | 12 | Court packets / orders involving HOAs |
| `pii:membership_list` | 8 | Correctly refused at API-level rejection |
| `page_cap:*` | 28 | Single huge PDFs above the 200-page MAX_PAGES_FOR_OCR guard |
| `duplicate:prepared` | 11 | Same SHA across counties |

The 44 `_unresolved-name/` items are the bank-stage name-quality gate doing
its job — the source HTML/snippet didn't yield a usable HOA name. Most of
those drop out at prepare via `unsupported_category` after page-one review;
the residual that survives ends up in the post-import name-cleanup pass.

## Source Families Attempted vs Productive

| Source family | Status | Notes |
|---|---|---|
| Wyoming SoS (wyobiz.wyo.gov) | blocked | F5/Akamai TSPD CAPTCHA; no headless bypass |
| Wyoming open-data portal | not present | No SODA-style WY business registry export exists |
| Per-county Serper (9 counties) | productive | All 9 yielded ≥18 manifests; primary discovery |
| Resort-condo management portals (FirstService, Bluegreen) | not pursued | Per playbook §10: walled, expected zero yield |

## Map Coverage Gap (36% vs ≥80% Tier-0 target)

Below target. Two structural reasons specific to WY:

1. **Keyword-Serper-discovered HOAs do not carry SoS ZIP/city.** RI's 99.5%
   coverage came from SoS-first leads that included `postal_code` directly in
   the lead JSON. WY had to extract ZIPs from doc text via
   `/admin/extract-doc-zips` after import. That endpoint only matched 53/133
   live HOAs (40%) — the rest are short rules/plat docs that don't repeat
   their own ZIP enough times to clear the top-3 frequency filter.
2. **Wyoming ZIP density is sparse.** Even the geocoded subset reduces to 48
   valid `zip_centroid` records (the other 5 had ZIPs whose first digit
   didn't pass the `WY -> "8"` sanity filter — likely management-company
   addresses in NV/CO/UT).

`enrich_wy_locations.py` falls back to a `WY_CITY_CENTROIDS` table for
city-only HOAs, but the playbook (§6) and `db.upsert_hoa_location` keep
`city_only` quality hidden from the map. The 85 city-only records remain in
the database with stable city/county metadata and surface in profile pages
and search; they just don't get a pin.

Recommendation for the next Tier 0 SoS-blocked state: budget for `city_only`
to be the dominant quality and consider a Census-ZCTA centroid path that
doesn't depend on extract-doc-zips frequency.

## Lessons Learned

1. **WY SoS is bot-protected like VT.** Update Appendix D: `WY` should read
   `keyword-serper (SoS blocked)` rather than `SoS-first`. Same applies to
   any future small Western state with an F5/Imperva-protected business
   search portal — preflight before committing to SoS-first.
2. **Resort-condo language gates work.** Including `"ranch"`, `"club"`, and
   `"village association"` in the per-county query patterns picked up real
   WY-shaped HOAs (Rafter J Ranch, Hidden Hills Ranches, Star Valley Ranch)
   that an HOA-only keyword set would have missed.
3. **9 of 23 counties is the right scope for sub-750 CAI states.** No
   skipped county would have justified the cost; no swept county was dry.
4. **Doc-ZIP extraction is necessary but not sufficient for Tier 0 map
   coverage when SoS is blocked.** Plan for ~35-40% map rate in this shape
   unless we add a Census ZCTA fallback that geocodes from the recorded
   `city` field even when doc text is too short to expose its own ZIP.
5. **The Phase 7 page-one review caught 131/272 candidates (48%) before any
   live ingest** — this is the right place to spend OCR budget.
6. **2 stale legacy manifests under `WY/broward/` and `WY/pike/`** exist
   from prior cross-state contamination (Florida and Pennsylvania county
   slugs respectively). Left untouched per the "do not edit shared files
   another run is touching" rule and because they're not actively harming
   the WY profile space (they don't show on map and have proper non-WY
   addresses inside their manifests). Worth a one-time cross-state audit
   pass at some point, but not in this run's scope.
7. **Bank-stage `is_dirty()` correctly bucketed 44 candidates into
   `_unresolved-name/`.** The post-import LLM rename pass picked up 7 of the
   ones that survived to live; the rest were rejected at prepare for being
   non-governing.

## Suggested Playbook Edits

- **Appendix D, WY row:** change `SoS-first` to `keyword-serper (SoS
  blocked: F5/TSPD CAPTCHA)`. Same for VT (already noted in VT handoff).
- **§6 OCR-Assisted Geography:** add a callout that for SoS-blocked Tier 0
  states, expect map coverage to land in the 35-45% band even after
  doc-ZIP extraction, unless a Census ZCTA city-centroid path is wired in.
- **§2 county scope guidance:** the "9 of 23" pattern (HOA-bearing counties
  only, no county-by-county sweep across rural ranchland) should be promoted
  from informal to a Tier 0 / sparse-population recommendation.

## Reusable Artifacts Committed

- `state_scrapers/wy/scripts/run_state_ingestion.py` — runner from template,
  `keyword-serper` discovery mode, Tier 0 budget defaults.
- `state_scrapers/wy/scripts/enrich_wy_locations.py` — adapted from NH;
  WY bbox + ~50-city centroid table covering the 9 HOA-bearing counties.
- `state_scrapers/wy/scripts/enrich_wy_from_doc_zips.py` — new utility:
  reads `/admin/extract-doc-zips`, geocodes via zippopotam.us, posts back
  via `/admin/backfill-locations` with `location_quality=zip_centroid`.
  Reusable for any keyword-Serper-discovered state where the prepare phase
  doesn't carry SoS ZIPs.
- `state_scrapers/wy/queries/wy_*_serper_queries.txt` — 9 per-county query
  files including ranch/club/village patterns specific to WY HOA naming.
- `state_scrapers/wy/notes/discovery-handoff.md` and this retrospective.
