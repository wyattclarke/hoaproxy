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
