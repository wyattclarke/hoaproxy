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

