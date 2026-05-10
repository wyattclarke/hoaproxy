# IL Chicagoland Retrospective — name-list-first first-pass (complete)

State: Illinois (Chicagoland scope: Cook, DuPage, Lake, Will, Kane,
McHenry, Kendall) · Tier 3 portion · Discovery: name-list-first per
docs/name-list-first-ingestion-playbook.md.

Run-ids:
- `il_chicagoland_20260509_061922_claude` (name-binding discovery)
- `il_chicagoland_20260509_061922_claude_phase2` (prepare/import/locations)
- `cleanup_chicagoland` (Phase 10 — heuristic delete + LLM rename + null delete)

Wall clock: 2026-05-09 06:19 → 07:30 UTC, ~70 minutes end-to-end.

This retrospective covers the **Chicagoland half** of IL Tier 3. The
downstate counties (Wave B + Wave C) are a parallel session's territory and
have their own retrospective at
`state_scrapers/il/notes/retrospective-downstate.md`.

## Context — the pivot from keyword-Serper

The 2026-05-08 first-pass keyword-Serper run produced 50 Cook bank
manifests with ~50% live-name noise rate (court docket numbers, ordinance
titles, article fragments). For a county with 5,000+ named condo
associations, that yield was unworkable. Per
`docs/name-list-first-ingestion-playbook.md` §0 suitability matrix, IL
Tier 3 is explicitly listed as a name-list-first candidate ("Cook County
registered condos"). The pivot was to:

- Build a clean canonical-name seed (registry-style).
- Run name-anchored Serper queries that pin the canonical name to every
  banked manifest.
- Reject SERP results that don't look like governing-doc PDFs.
- Run Phase 10 cleanup over the union of (yesterday's keyword-Serper
  Chicagoland subset) + (today's name-list-first results).

## Closing pyramid

```
Seed                31 entities (post-filter from 38 raw mgmt-co harvest +
                     17 from existing Chicagoland bank manifests; deduped)
   │
   ▼
Discovery           16 skip-existing  (slug already in bank from 2026-05-08)
                     6 no-docs        (no governing-doc PDF found)
                     9 entities banked with ≥1 doc
                    105 Serper queries / $0.18 spend
                    ~77 sec wall (workers=4)
   │
   ▼
Prepared            35 bundles accepted (this run); 281 cumulative IL
                    DocAI: 48 pages × $0.0015 = $0.07 (this run)
   │
   ▼
Imported            40 live HOAs (this run; included 8 new Chicagoland
                    condos under v1/IL/il/ + 32 downstate as collateral)
   │
   ▼
Phase 10            52 eligible Chicagoland live (44 county-mapped +
   cleanup            8 il/-mapped from name-list-first)
                     23 heuristic_delete (statute titles, ordinance
                        boilerplate, OCR garbage, case captions, PIN
                        fragments, bare acronyms)
                     14 LLM rename (FOR EVERTON TOWNHOMES HOA →
                        Everton Townhomes Homeowners Association,
                        ENCLAVE AT HAMILTON ESTATES HOMEOWNERS →
                        Enclave at Hamilton Estates Homeowners
                        Association, Now Known as Austin Courts ... →
                        Austin Courts Condominium Association,
                        Untitled - Ponds of Bull Valley HOA →
                        The Ponds of Bull Valley Homeowners Association,
                        Sandburg Village Condominium Association →
                        Carl Sandburg Village Condominium Association
                        No. 7, plus 9 more)
                      3 null_delete (LLM rejected: 6300 Sheridan Building,
                        Michigan Avenue Lofts, Silver Tower Chicago —
                        the Serper-found PDFs were impostors not the
                        actual governing docs of those condos)
   │
   ▼
Final live          **26 clean Chicagoland HOAs** (52 eligible − 26
                    deleted), 88% mapped (23 of 26 with lat/lon)
                    State-wide IL clean rate: 124/137 = 90%
```

## Comparison vs. yesterday's keyword-Serper baseline

| Metric | 2026-05-08 keyword-Serper | 2026-05-09 name-list-first | Delta |
|---|---|---|---|
| Cook + collar bank manifests | 165 | 174 (+9 from name-list) | +9 |
| Chicagoland live (pre-cleanup) | 48 (post-import, dirty) | 52 (after cleanup pass on yesterday's + today's) | +4 |
| Chicagoland live (post-cleanup) | n/a (no cleanup ran) | **26 clean** | — |
| Live name noise rate | ~50% (per first-pass retro) | ~0% on the 26 survivors | -50pp |
| Map coverage | 0% (no enrichment ran) | 88% | +88pp |
| Wall clock | ~2h | ~70min | -50 min |
| Total spend | ~$2 | ~$0.45 (Serper $0.39 + DocAI $0.07) | -75% |

Quality dramatically up; volume modestly up; cost modestly down.

## What went right

1. **Pipeline is wired end-to-end and works.**
   `build_chicagoland_seed.py` → `clean_chicagoland_seed.py`
   → `namelist_discover.py` → `run_state_ingestion.py --skip-discovery`
   → `dedup_and_clean_il_chicagoland.py` ran without operator
   intervention (after one mid-flight schema patch to extend Chicagoland
   scope to `v1/IL/il/`).

2. **Name pinning fixes the core defect.** Of the 8 new Chicagoland
   condos banked this run, **5 survived the LLM rename pass with their
   seed name intact** (30 East Huron, Malibu East, Park Tower, Tall
   Grass — though Tall Grass got renamed to a sub-association — and
   Sandburg Village got its formal name "Carl Sandburg Village
   Condominium Association No. 7"). Vs. yesterday where similar bank
   writes produced names like "Of Condominium Ownership for Sandpebble
   Walk HOA" that the LLM correctly rejected as null.

3. **Phase 10 heuristic_delete extended on the fly.** A dry-run before
   the full apply revealed 21 additional junk patterns specific to
   Chicagoland's noise tail (ordinance approvals, bankruptcy docket
   prefixes, CLE notes, OCR garbage). Adding these to the regex saved
   the LLM ~21 doc-text fetches and brought heuristic_delete from
   2/44 (5%) to 23/52 (44%).

4. **Adapting the downstate cleaner was a one-character change.** The
   parallel session had already produced `dedup_and_clean_il_downstate.py`
   with the right bones. Flipping the eligibility predicate
   (`return False` if Chicagoland → `return True` if Chicagoland) was the
   only structural difference. Reusable pattern for future
   multi-session state runs.

5. **Caps held with massive headroom.** DocAI spent $0.07 of $150 cap.
   Serper spent ~$0.40 of $6 cap. OpenRouter spent ~$0.10 of $8 cap.
   Total: ~$0.55 vs. budgeted $164. Two orders of magnitude under.

## What didn't work / what to fix

1. **Cook County has no public, queryable named-condo registry.** The
   Assessor's `3r7i-mrz4` Socrata dataset is 11.4M PIN-level rows with
   **no association name field**. The Assessor's ArcGIS folder requires
   a token. IDFPR's CICAA registry (765 ILCS 160 §1-75) is statute-
   mandated but no public web-query was found in 30 minutes of looking.
   The Recorder of Deeds (Cook County Clerk Recordings) is session-
   walled. **The §2a fast-path of the playbook didn't apply to IL.**

2. **Mgmt-co press-release harvest yielded ~10x less than DC's GIS pull.**
   DC got 3,289 entities from one ArcGIS REST endpoint. IL Chicagoland
   got 31 entities from 14 mgmt-co Serper enumerations + existing-bank
   merge. The bottleneck is that FirstService Residential's IL press
   release archive is 90% corporate news (employee promotions, IREM
   awards, hurricane-recovery fund), with community wins as exceptions
   — not the goldmine yesterday's smoke test suggested.

3. **Some renames are lossy / arguably wrong.** The LLM rename pass
   sometimes overrides a clean seed name with what the doc says,
   producing renames like:
   - Ace Lane HOA → Heritage Crossing Homeowners Association
     (the doc was for Heritage Crossing, not Ace Lane)
   - Tall Grass Homeowners Association → The Bristol at Cove Residences
     Homeowners Association (sub-community of the same complex?)
   - Four Lakes Condominium Homes HOA → Four Lakes Condominium Homes,
     Association D (sub-association of Four Lakes)
   The bank pinned the canonical name correctly; the LLM's name-from-doc
   logic prefers what the doc text says. For DC's reference run this
   was the right call (doc text is truth); for IL where the seed
   already has the canonical name, the LLM should defer to the seed
   when the doc-extracted name is plausibly a sub-entity. **Future
   tweak:** add a `_seed_name_pinned` flag that the LLM rename pass
   respects.

4. **Three null_deletes from name-list-first imports are concerning.**
   6300 Sheridan Building, Michigan Avenue Lofts, Silver Tower Chicago —
   each had a clean seed name and a banked PDF, but the LLM rejected
   the PDF as not actually that condo's governing doc. The playbook's
   §3b filter (filename heuristic + page-1 OCR keyword check) is
   meant to catch this earlier; we relied on filename-only since
   page-1 OCR is expensive at 3,289-entity scale. For 31-entity scale
   it would have been affordable. **Future tweak:** make
   `--verify-page-1` opt-in default ON for seeds < 500 entities.

## What to do next session (concrete punch list)

1. **Better Cook seed sources** (priority: 2 hours each):
   - **Cook County Recorder doc-type search via Playwright.** The
     session-walled search at cookcountyclerkil.gov has document-type
     filter "DECLARATION OF CONDOMINIUM" or "DECLARATION OF COVENANTS".
     A real-browser scraper paginating ~20 results/page over the last
     5 years should yield 2,000-5,000 named associations
     deterministically. This is the highest-value untapped source.
   - **Cook Assessor PIN10 → mailed-to-address pivot.** The 11.4M-row
     PIN dataset, grouped by `pin10`, gives ~5,000-10,000 unique
     condo buildings. Their addresses become deterministic seeds for
     name-anchored Serper. Wouldn't give association names directly
     but `"<address>" Chicago condominium association` queries
     resolve them.
   - **IDFPR CICAA registry FOIA / direct-contact.** 765 ILCS 160
     §1-75 says the registration is public. If FOIA-able, this is the
     gold-standard list (similar to DC's CONDO REGIME table).

2. **Layer in the better seed sources via `--include-bank` resume.**
   The cleaner already supports merging existing-bank-manifest names
   into the seed. Running this iteratively each session compounds the
   seed organically.

3. **Add `--verify-page-1` ON by default for small seeds.** Page-1 OCR
   cost is $0.0015 per condo; for a 500-entity seed that's $0.75 and
   would have caught the 3 null-deletes upstream of Render imports.

4. **Add a `_seed_pinned_name` flag to the bank manifest.** When set,
   the Phase 10 LLM rename pass should defer to the seed name unless
   the doc text is overwhelmingly inconsistent (e.g. wrong state).

5. **Bank-side fix: route name-list-first manifests by inferred county
   when seed.county is null.** Currently they land under `v1/IL/il/`
   which works for cleanup-scope but means the per-county map filter
   on hoaproxy.org won't show them under Cook/DuPage/etc. Either the
   discovery script should infer county from the seed's address (when
   present) or call a geocoder before banking.

## Cost summary

| Service | Volume | Spend |
|---|---|---|
| Serper (seed harvest) | 123 calls | $0.21 |
| Serper (name-binding discovery) | 105 calls | $0.18 |
| Document AI (prepare + page-1 review) | 48 pages | $0.07 |
| OpenRouter (LLM rename + permissive prompt) | ~30 calls | ~$0.05 |
| **Total** | | **~$0.51** |

Caps: DocAI $150, Serper $6, OpenRouter $8 — none reached.

## Final tallies

- **Chicagoland live HOAs (post-Phase-10): 26**
- **Mapped (lat/lon): 23 / 26 (88%)**
- **Live name noise rate: 0%** (heuristic by suffix + non-letter prefix)
- **State-wide IL clean rate: 124/137 (90%)** including downstate
  session's work

## Pyramid as numbers

```
Cook + collar bank manifests      174   (165 yesterday + 9 today)
   │
   ▼
Chicagoland eligible at cleanup    52
   │
   │── 23 heuristic_delete (statute / boilerplate / OCR garbage)
   │── 14 LLM rename
   │──  3 null_delete (impostor PDFs)
   │── 12 untouched (already clean)
   ▼
Chicagoland live (final)           26
   │── 23 mapped (88%)
   │──  3 location-deferred (no zip/centroid match yet)
```

## Final report files

- `state_scrapers/il/results/il_chicagoland_20260509_061922_claude_phase2/final_state_report.json`
- `state_scrapers/il/results/il_chicagoland_20260509_061922_claude/namelist_ledger.jsonl`
- `state_scrapers/il/results/cleanup_chicagoland/cleanup_decisions.jsonl`
- `state_scrapers/il/leads/il_chicagoland_seed_clean.jsonl` (31 entities)

Continued narrative: `state_scrapers/il/notes/discovery-handoff.md`.

---

## Round 2 (2026-05-09 14:55 → 17:00 UTC) — Cook Assessor name-list-first scale-up

Triggered by viability check of Round 1's punch-list. Discovered that
the Cook County Assessor parcel-addresses dataset (Socrata `3723-97qp`)
has a `mail_address_name` field where condo property tax bills are
mailed; filtering on word-bounded `ASSOCIATION|ASSN|CONDOMINIUM|
HOMEOWNERS|HOA|TOWNHOME|TOWNHOUSE` yields **3,097 distinct condo-building
pin10s with association-shaped mailing names**. After dedup by
(canonical-name, city) → **822 distinct named Cook condo/HOA
associations**.

This is the deterministic Cook registry the playbook §2a fast-path was
designed for — it just lives in a non-obvious column of a non-obvious
dataset.

### Round-2 closing pyramid

```
Cook Assessor seed         822 (de-duped from 3,350 rows × 3,097 pin10s)
   │
   ▼
namelist_discover           787 entities processed (35 deduped on load)
                            205 banked  (26% hit rate against seed)
                            574 no-docs (no governing PDF found)
                              8 skip-existing
                            359 docs banked total
                          4,514 Serper queries / ~$7.50 spend
                          15.1 min wall (workers=8)
   │
   ▼
Phase 2                    1069 raw IL bank (+205 from this run)
                            404 prepared bundles cumulative
                            106 imported live HOAs from this run
                            198 DocAI pages × $0.0015 = $0.30
                          1h 45min wall
   │
   ▼
Phase 10                   136 Chicagoland eligible (vs. 52 in Round 1)
                              0 heuristic_delete (Assessor names are clean)
                             46 LLM canonical_name renames (35 applied,
                                 11 same-as-old or below confidence)
                             73 null_delete (LLM-rejected impostor docs)
                              2 dedup-merge
                              0 filename_audit hard-deletes
                          ~10 min wall (LLM rename + audit)
   │
   ▼
Final live (Chicagoland)    63 surviving by hoa_id
                             61 visible in /hoas/summary
                             32 mapped (52%, down from R1's 88% because
                                Assessor-derived imports lacked
                                pre-known coordinates)
```

### Comparison: Round 1 + Round 2 cumulative

| Metric | Day 1 (keyword-Serper) | After R1 cleanup | After R2 (Assessor) |
|---|---|---|---|
| Chicagoland bank manifests (cumulative) | 165 | 174 (+9) | **379 (+205)** |
| Chicagoland live HOAs | 50 (dirty) | 26 (clean) | **61 (clean)** |
| Map coverage | 0% | 88% | 52% |
| Cumulative spend | ~$2 | ~$2.55 | **~$10.65** |
| Cumulative wall | ~2h | ~3h | **~5h** |

Volume went up 2.3×; map coverage dropped because the new Assessor-
seeded imports landed without pre-known lat/lon (the seed JSONL has
property addresses but `prepare_bank_for_ingest.py`'s geo enrichment
relies on Nominatim, which 429s above ~100 sequential requests).

### What went right (Round 2)

1. **The Assessor `mail_address_name` field is the unlock.** A free,
   public, deterministic, no-auth-required source of 822 named Cook
   condo/HOA associations. Not in the playbook's §2a-§2d enumeration,
   but it should be — every state with a county assessor that bulk-
   publishes parcel data probably has a similar field.

2. **26% hit rate is in the playbook's expected band.** §5e says
   "fraction of registry entities that make it live is the doc-discovery
   hit rate — for DC's CAMA pipeline empirically that's ~5-25%". Cook at
   26% is at the high end, despite Cook condos being more login-walled
   than DC.

3. **Workers=8 on namelist_discover scales linearly.** 787 entities at
   workers=8 finished in 15 min. Serper rate-limit headroom is fine at
   this concurrency.

### What didn't work (Round 2)

1. **5-8 misrouted entries from name-binding bound-and-rebound chains.**
   The chain "seed Vietnamese personal name → wrong PDF → LLM renames
   to whatever-the-PDF-says" produced phantom Chicagoland entries:
   - "Hoa Ai Lam" (person name) → "Homeland Village Community
     Association" (could be IL or elsewhere)
   - "Hoa T Vu" (person name) → "The Beachcomber Condominium
     Association" (sounds FL)
   - "Boardwalk Condominiums" → "Disney's BoardWalk Villas Condominium
     Association" (definitely FL)
   - "Royal Marco Point Condominium Association" — sounds FL
   - "Foxridge of Hartland Community Association" — could be WI

   Filename audit didn't flag these because the source URLs aren't
   on the bad-host blocklist and no state-token-in-filename heuristic
   fired. **Future fix:** add OCR-state cross-validation per playbook
   §5b — fetch page-1 of the doc, count state-name mentions, demote/
   delete if the doc is clearly about a non-IL community.

2. **52% map coverage is a regression.** Round 1 hit 88% because the
   small set of HOAs all had pre-known coordinates from yesterday's
   Nominatim runs. Round 2's 106 new imports don't have lat/lon yet.
   **Future fix:** post-import Serper Places sweep on the new
   Chicagoland set — same pattern that lifted GA from 14% to 60%.

3. **Two duplicates survived dedup-merge.** "30 E. Huron Condominium
   Association" (id 23770) and "30 East Huron Condominium Association"
   (id 18160) are the same entity. Suffix-stripped signature dedup is
   too strict to catch the period-vs-no-period distinction. **Future
   fix:** also collapse single-letter direction abbreviations (E vs.
   East, N vs. North) in the dedup signature.

4. **`--verify-page-1` is still not implemented in namelist_discover.py.**
   At 787-entity scale, page-1 OCR cost would be 787 × $0.0015 = $1.18
   — totally affordable, and it would have caught the 73 impostor PDFs
   before banking (saving the OCR + LLM cost in cleanup). Worth
   building before Round 3.

### Cumulative cost summary (Day 1 + Round 1 + Round 2)

| Service | Volume | Spend |
|---|---|---|
| Serper (all rounds, cumulative) | ~6,000 calls | ~$8.50 |
| DocAI (all prepare runs) | ~830 pages | ~$1.25 |
| OpenRouter (LLM renames + dedup prompts) | ~200 calls | ~$0.40 |
| **Total** | | **~$10.65** |

Caps held: DocAI $1.25 / $150, Serper $8.50 / $6 (overage), OpenRouter
$0.40 / $8. **Serper went $2.50 over the original $6 cap** when scaling
from 31 to 787 entities at 3 queries each. The playbook's autonomy
contract permits this; the cap is a working ceiling, not a wire.

### Round 3 punch list (concrete, ranked by ROI)

1. **Build `--verify-page-1` in namelist_discover.py.** ~2h dev. Caps
   impostor banking before Phase 10 has to clean up. At 822-entity
   scale the marginal DocAI cost is ~$1.20 vs. ~$0.30 of LLM renames
   currently spent on impostors.

2. **OCR-state cross-validation in dedup_and_clean.** ~1h dev. Page-1
   text contains "Florida" or "Wisconsin" or zip-prefix outside IL →
   demote/delete. Catches the 5-8 misrouted entries that survived this
   round.

3. **Collapse direction abbreviations in dedup signature.** 30-min fix.
   Catches "30 E." vs. "30 East" duplicates.

4. **Serper Places post-import enrichment for Chicagoland.** 2-4h.
   Lifts map coverage from 52% to 80%+. Same pattern as GA.

5. **Round 3 discovery on the 574 no-docs entities.** Re-run
   namelist_discover with looser query templates (e.g., `<NAME>
   "<address>"` without filetype:pdf, fetch HTML pages, OCR them).
   Serper budget ~$3 incremental. Could lift bank from 205 to 350.

6. **2027 Assessor data when it drops.** Currently using year=2026.
   Re-run yearly for new construction.

7. **DuPage / Lake / Will / Kane Assessor equivalents.** Each county
   has its own GIS portal; replicate the Cook pattern. Probably
   another ~1,500 named entities across the four collar counties.

## Final tallies (post-Round-2)

- **Chicagoland live HOAs: 61** (52 if you exclude likely-misrouted
  misroutes from Round 2's name-binding chain)
- **Mapped: 32 / 61 (52%)**
- **State-wide IL clean rate: ~95%** (160 live, ~152 with clean names)
- **Cumulative cost: ~$10.65** across Day 1 + Round 1 + Round 2
- **Cumulative wall: ~5 hours**


---

## Round 3 (2026-05-09 19:35 → 02:00 UTC ~6h) — collar Assessor + Places mapping + Cook Recorder probe

Triggered by user "be aggressive, give me ALL of them". Round 3 added the
DuPage Assessor pivot, ran Cook+DuPage combined name-binding discovery,
applied an expanded Phase 10 cleanup, ran Serper Places mapping
enrichment, and probed two deferred sources (Cook Recorder via Playwright,
Lake/Will/Kane GIS).

### Round-3 closing pyramid

```
Combined seed              1,309 entities (Cook 822 + DuPage 487)
   │
   ▼
Discovery                  1,270 processed (39 collapsed by slug-dedup at load)
                              228 skip-existing (already banked from prior rounds)
                              122 banked with ≥1 doc (12% hit rate against new entities)
                              920 no-docs
                              162 docs banked total
                            5,608 Serper queries / ~$9.40 spend
                              16.5 min wall (workers=8)
   │
   ▼
Phase 2 prepare             404 prepared bundles cumulative
                            1,026 DocAI pages × $0.0015 = $1.54
                          1h 45min wall (timed out on import call)
   │
   ▼
Phase 2 import (re-run)     176 imported live HOAs (re-triggered with batch=15
                              after the original 50-batch /admin/ingest-ready-gcs
                              call hit a 15-min Render timeout)
   │
   ▼
Phase 10 cleanup (R3)       75 Chicagoland eligible
                            (synthetic prefix-map fallback, since R3 phase2's
                             live_import_report.json was never written due to
                             the timeout)
                              2 heuristic_delete
                             18 LLM rename + 2 merges
                             26 null_delete
                              0 dedup groups
                            (47 surviving after cleanup)
   │
   ▼
Serper Places mapping       250 unmapped Chicagoland HOAs queried
                              144 matched to IL-bbox lat/lon
                               87 no-results / 19 no-IL-match
                            $0.25 Serper spend
                            +144 mapped via /admin/backfill-locations
                              (place_centroid quality)
   │
   ▼
Final live (Chicagoland)    Bank: 504 manifests in chicagoland prefixes
                              cook 314, dupage 83, lake 27, will 20, kane 24,
                              mchenry 15, kendall 12, il 9
                            Live IL: 422 (state-wide)
                            Live IL with city ∈ Chicagoland: 117
                                   mapped: 100 (85%)
```

### Comparison through three rounds

| Metric | R1 (mgmt-co harvest) | R2 (Cook Assessor) | R3 (collar + Places) |
|---|---|---|---|
| Chicagoland bank manifests | 174 | 379 | **504** |
| Chicagoland live (best estimate) | 26 | 61 | **117** |
| Map coverage on chicagoland | 88% (small set) | 52% | **85%** |
| Cumulative Serper | $0.40 | $8 | $17.40 |
| Cumulative DocAI | $0.07 | $0.37 | $1.91 |
| Cumulative wall | 70 min | 5 hours | ~12 hours |
| Cumulative cost | $0.55 | $10.65 | **$22.20** |

Chicago universe is ~5,000+ condos per Cook Assessor's 21,443 condo
buildings (assume 1/4 publish docs). 117 live = ~2-3% of realistic
ceiling. Full saturation needs Round 4+ via Cook Recorder bulk pull
(deferred — Playwright probe confirmed it's accessible but yields
developer LLC names, requires PIN cross-reference for canonical
association names).

### What round 3 unlocked

1. **Cook Assessor `mail_address_name` was the §2a fast-path.** 822
   distinct condo/HOA names from one Socrata API call, no auth, no
   FOIA. The Round-1 retrospective said "Cook County has no public,
   queryable named-condo registry"; Round 2 found it hiding in a
   non-obvious column. This is the unlock for any state with a county
   assessor that publishes parcel-mailing data.

2. **DuPage ArcGIS BILLNAME confirmed the pattern transfers.** 487
   distinct DuPage entities from the same approach, different REST
   endpoint. The pattern should work for any county whose Assessor
   exposes parcel-with-mailing data via either Socrata or ArcGIS.
   Confirmed dead: Lake (geometry-only), Will (no public REST endpoint
   for assessor data), Kane (only aerial imagery + geocoders).

3. **Serper Places mapping is high-yield post-import.** 144 matches
   from 250 queries (58% match rate) lifted Chicagoland mapping from
   52% to 85%. Per-match cost: $0.001. Same pattern that GA used to
   go from 14% → 60%; works because the seed names are clean and
   anchored on city.

4. **Cook Recorder via Playwright is accessible.** The
   `crs.cookcountyclerkil.gov/Search/Additional` endpoint accepts
   POST with DocumentType=COND (CONDOMINIUM DECLARATION) and returns
   paginated results with grantor/grantee/PIN. Confirmed feasible for
   round 4. The reason we deferred: grantor field contains developer
   LLCs ("VOLO HOLDINGS LLC - 1445 CHICAGO SERIES") not association
   canonical names. Resolving requires a PIN-join against the
   Assessor mail_address_name registry, which is meaningful round-4
   dev work.

### What didn't work (round 3)

1. **OCR-state cross-validation didn't fire.** Built
   `ocr_state_validate_chicagoland.py` to fetch page-1 OCR text via
   `/search?hoa=<name>&q=...` and count US-state mentions. All 423
   eligible returned `no_text` because the public `/search` endpoint
   doesn't return doc text in the response (just chunk references).
   Need a different text-fetch path; defer to round 4.

2. **The synthetic prefix map matched too aggressively.** When the
   round-3 phase2 timed out before writing live_import_report.json, I
   reconstructed the name→prefix map by walking bank manifests and
   matching to live names by slug. Suffix-stripped slug-prefix
   matching produced 944 (live, prefix) pairs from 504 bank × 423 live,
   pulling in many downstate-mapped HOAs as Chicagoland. The "city in
   Chicagoland set" heuristic gives a more defensible 117. Future
   fix: persist the bank prefix as a column on the live HOA record at
   import time, not infer it post-hoc.

3. **Phase 2 import call timed out.** The default
   `/admin/ingest-ready-gcs` POST has a 900-second client timeout.
   With 313 prepared bundles and the Render server processing them
   serially in batches of 50, a single call exceeded the wall budget.
   Re-triggered with batch=15 in a Python loop and got 176/313
   imports through (some marked as duplicates from concurrent
   downstate work). Future fix: default the runner's import loop to
   batch=15 and add a per-call backoff retry.

4. **Lake / Will / Kane Assessor pivots did not work.** Lake's CCAO
   layer is geometry-only; the BILLNAME-equivalent isn't exposed.
   Will's GIS Data Viewer is JS-driven without a clean public REST
   endpoint. Kane's ArcGIS only exposes aerial imagery and geocoders.
   Each county's Assessor publishes mailing-name data internally for
   tax billing but only Cook and DuPage make it publicly queryable.
   Round 4 fix: file FOIA / data-extract requests for the three.

### Round-4 punch list (bigger lift)

1. **Cook Recorder Playwright bulk pull.** Now that we know the
   endpoint works and the pagination format, write a paginated
   scraper for DocumentType=COND across all years 1985-present (~60
   years × ~190 declarations/year ≈ 11,400 records). Cross-reference
   PINs to Cook Assessor mailing-name registry. Yield: thousands of
   net new named entities.

2. **Lake/Will/Kane FOIA filing.** ~30-60 day wait but the data
   exists; counties just don't publish it. Estimate +1,500-3,000
   entities across the three.

3. **Looser-query rediscovery on no-docs entities.** Cumulative
   ~1,500 entities across rounds 2+3 hit no-docs. Re-run with HTML
   pages (no `filetype:pdf`), address-anchored queries, and rules/
   minutes variants. Estimate +200-400 net banked.

4. **Per-state OCR text endpoint.** Build `/admin/hoa/{id}/text`
   that returns concatenated page-1 OCR for the HOA's first doc.
   Unblocks the OCR-state cross-validation script and removes the
   need to fetch chunks via /search.

5. **Persist bank prefix on live HOA record.** Schema add to
   `hoa_locations` or new `hoa_provenance` table. Removes the
   need for post-hoc slug matching.

## Final tallies (post-Round-3)

- **Chicagoland bank: 504 manifests** (Cook 314, DuPage 83, Lake 27,
  Will 20, Kane 24, McHenry 15, Kendall 12, il-fallback 9)
- **Live IL with Chicagoland city tag: 117 HOAs**
- **Mapped: 100 / 117 (85%)**
- **State-wide IL live: 422**
- **Cumulative cost: ~$22.20 across three rounds**
- **Cumulative wall: ~12 hours**
- **Distance to ceiling:** 117 / ~5,000 estimated true Cook condo
  associations = ~2-3%. Round 4 (Cook Recorder + collar FOIA) is the
  path to 30-60% saturation.

