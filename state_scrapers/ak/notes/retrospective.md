# Alaska HOA Scrape Retrospective

Run id: `ak_20260508_035203_claude` (Tier 0, agent: claude). Third state in
the May 2026 overnight batch (`overnight_batch_20260508`).

Wall time: ~55 min end-to-end (preflight 03:52 UTC → import-finished 04:46 UTC
→ unconditional name cleanup 04:53 UTC → delete pass 04:55 UTC → dedupe
merges 04:56 UTC). Longer than SD/ND because Alaska's borough-recorder
patterns required more discovery wall time per query (slower hosts).

## Final Counts

| Metric | Value |
|---|---:|
| Raw bank manifests | 133 |
| Prepared bundles | ~50 |
| Live HOA profiles (post-import) | 53 |
| Live after Phase 10 cleanup (24 renames + 1 merge + 20 deletions) | 32 |
| Live after dedupe merges (3 pairs) | **29** |
| Live documents | 41 |
| Map points | 5 |
| Map rate | 17% (5/29) |
| All docs fetchable | 41/41 OK |

29 genuine HOAs is a markedly better yield than ND's 12 or SD's 8 — Alaska
is small in population but condo-heavy (Anchorage, Mat-Su, Kenai resort
condos) so the keyword-Serper sweep got more recognizable HOA names.

## Per-Borough Bank Counts

| Borough | Bank manifests | Notes |
|---|---:|---|
| Anchorage Municipality | ~55 | Densest metro; Anchorage proper + Eagle River |
| Matanuska-Susitna | ~30 | Wasilla, Palmer, growing residential |
| Fairbanks North Star | ~15 | Fairbanks + North Pole |
| Kenai Peninsula | ~25 | Soldotna, Kenai, Homer, Seward — resort condo-heavy |
| `unresolved-name/` | ~8 | |
| **Total** | **133** | |

(Approximate — exact per-borough counts not split out post-import.)

## Genuine HOAs (29)

| hoa_id | Name |
|---|---|
| 16029 | Park Place Condominium Association, Inc. |
| 16033 | Briarcliff Townhomes Association |
| 16034 | The Meadows Property Owners Association, Inc. |
| 16036 | Cimarron Circle Homeowners Association, Inc. |
| 16037 | ALASKAN BAY OWNERS ASSOCIATION |
| 16038 | Victoria Park Subdivision Association, Inc. |
| 16039 | Contempo I Condominium Association |
| 16040 | Adventure Condominium Association |
| 16041 | Keyes Point Subdivision HOA |
| 16042 | Eagle Crossing Homeowners Association |
| 16043 | Hillcrest Condominium Association (merged with 16044) |
| 16045 | Alaska Landings Homeowners Association |
| 16046 | 617 N Street Condominiums Owners Association, Inc. |
| 16047 | Kempton Hills Subdivision Homeowners Association |
| 16050 | Kandlewood Park Condominium Owners Association |
| 16055 | Devonshire Homeowners Association, Inc. |
| 16056 | Woodlake Addition Condominium Association |
| 16060 | Eastridge 4 Condominium Association (merged with 16031) |
| 16066 | Kee's Tern Subdivision Home Owners Association |
| 16067 | Victoria Estates Homeowners Association |
| 16069 | Big Lake Condominiums Owners Association, Inc. |
| 16070 | Falcons Ridge' Owners Association |
| 16072 | Mountain Rose Estates Condominium Owners' Association |
| 16073 | Secluded Pointe Estates Homeowners Association |
| 16076 | POTTER CREEK HOMEOWNER ASSOCIATION |
| 16080 | Powder Ridge Homeowners Association (merged with 16051) |
| 10808 | WINDJAMMER CONDOMINIUM ASSOCIATION, INC. (pre-existing entry, AK-attached, kept) |

## Cost Per HOA

| Channel | Spend | Per genuine HOA |
|---|---:|---:|
| Google Document AI | ~$1.20 (estimated, ~800 pages) | $0.041 |
| OpenRouter (DeepSeek + cleanup) | ~$0.20 | $0.007 |
| Serper | ~$0.03 | $0.001 |
| **Total** | **~$1.43** | **~$0.049** |

DocAI: under the $10 cap. OpenRouter higher than SD/ND because the
unconditional cleanup pass had 53 entries to process vs 19/35.

## Phase 10 Outcomes

**LLM unconditional cleanup (53 → 50):** 25 renames proposed, 24 applied as
renames + 1 merge.

**Hard-delete pass (50 → 30):** 20 deleted via `/admin/delete-hoa`. The
deletion list combined LLM null-canonical decisions with the regex set from
the ND retrospective, plus AK-specific additions:

- Government / municipal: `Municipality of Anchorage Anchorage Assembly`,
  `Anchorage Ao No. 2022-103(s-1)`, `CHAPTER 21.08: SUBDIVISION STANDARDS`,
  `UMED DISTRICT PLAN`, `Recording Dist: 401`, `Laydown Coversheet`
- Legal scholarship / publications: `1 THE YEAR IN REVIEW 2021 ADMINISTRATIVE LAW`,
  `Annual Real Estate Section Law Update 2018 Cases`,
  `Background Research for Issue-Response Summary Table`,
  `Check Your Code to See if ... Brookwood Area`
- Street addresses / fragments: `33699 Granross Street Anchor Point 99556`,
  `Longmere Lake Ridge Part`, `Silver Springs Terrace`, `Snowsmanagement HOA`
- OCR garbage: `ll ll l l ll l ll ll l lil lil ilil ...` (a chain of `i`s and `l`s)
- Boilerplate: `Restrictive —Tools to Limit What a Buyer Can Do With HOA`,
  `of , conditions and HOA`
- Mid-process / preliminary: `Moose Mtn Prelim`,
  `Chena Landings Ph II`, `Kingair Anchor Point Package Opt`,
  `General Information Property Owner Shawn Kantola`

**Dedupe merges (32 → 29):** 3 pairs, all of the same HOA:

| Source (kept after merge) | Merged-into target |
|---|---|
| 16043 Hillcrest Condominium Association | 16044 Hillcrest Condominium |
| 16060 Eastridge 4 Condominium Association | 16031 EASTRIDGE 4 CONDOMINIUM ASSOCIATION |
| 16080 Powder Ridge Homeowners Association | 16051 The Powder Ridge Planned Community Homeowners Association |

The Eastridge case is interesting: the bank pipeline produced both an
all-caps and a mixed-case version of the same HOA from two different
recorded documents. The dedupe merge pattern (rename source to target name,
which triggers merge-on-collision) cleaned this up correctly.

## Doc-Filename Audit

All 29 keepers passed: filenames match HOA names; no utility-newsletter
hosts; no junk-sinks (the pre-existing `Untitled HOA` was caught by the
regex pass and deleted; hoa_id 10808 Windjammer is a real condo association,
not a junk-sink).

## Server Bug — No Impact

The rename-doc-fetch fix shipped before AK's import phase started, so
none of AK's renames were affected by the 400-on-doc-fetch bug. All 41
docs verified fetchable post-cleanup.

## Lessons For The Playbook

1. AK boroughs use `Municipality` / `Borough` not `County`. The query
   files reflected this. The Anchorage queries used both `Anchorage Municipality`
   and `Anchorage` city anchors — both productive.
2. AK's recorded-covenants language is often older and ALL-CAPS. The
   `is_dirty()` `shouting_prefix` rule (len > 40) caught some, but several
   real HOAs (`ALASKAN BAY OWNERS ASSOCIATION` at 32 chars) survived as
   genuine entries with all-caps names. The LLM cleanup pass declined to
   propose canonical names for these (insufficient text variation), so
   they stayed all-caps. Consider: a separate "case normalization" step
   that fixes all-caps names with no other quality issues.
3. The dedupe-merge pattern (3 pairs in AK) is a real signal — bank-stage
   slug-merge sometimes fails when the same HOA's two recorded documents
   have slightly different names. Worth folding into the Phase 10 pass:
   after the LLM rename, scan keepers for near-duplicate names (Levenshtein
   < 5 or substring containment) and merge.
4. Map rate 17% — better than ND's 0% but well short of the Tier 0 80%
   target. Alaska's polygon coverage is sparse (Anchorage city has good
   GIS, but Mat-Su / Kenai / Fairbanks subdivisions don't). Worth a
   targeted Serper Places + ZIP-centroid pass post-deploy.

## Supplemental Sweep (Planned)

Per operator feedback ("err on the side of more counties"), a supplemental
sweep covering Juneau, Kodiak Island, Sitka, and Ketchikan Gateway boroughs
will run after the main batch wraps. Query files written to
`queries/ak_{juneau,kodiak_island,sitka,ketchikan_gateway}_serper_queries.txt`;
COUNTY_RUNS list extended in the runner. Expected to add 5–15 more genuine
HOAs (these are smaller boroughs but still have condo associations and
small-subdivision HOAs).

## Standard Ledger Files

- `state_scrapers/ak/results/ak_20260508_035203_claude/preflight.json`
- `state_scrapers/ak/results/ak_20260508_035203_claude/prepared_ingest_ledger.jsonl`
- `state_scrapers/ak/results/ak_20260508_035203_claude/live_import_report.json`
- `state_scrapers/ak/results/ak_20260508_035203_claude/live_verification.json`
- `state_scrapers/ak/results/ak_20260508_035203_claude/final_state_report.json`
- `state_scrapers/ak/results/ak_20260508_035203_claude/name_cleanup_unconditional.jsonl`
- `state_scrapers/ak/results/ak_20260508_035203_claude/discover_*.log` (4 files, one per borough)
