# West Virginia HOA Scrape Retrospective

Run id: `wv_20260508_081725_claude` (Tier 0). Sixth state in batch
`overnight_batch_20260508`. 11-county expanded coverage.

## Final Counts

| Metric | Value |
|---|---:|
| Raw bank manifests | 219 |
| Live HOA profiles (post-import) | 35 |
| After LLM rename pass (19 renames + 1 merge) | 34 |
| After delete pass (10) + dedupe merge + 1 final delete | **22** |

## Counties

Original: Kanawha (Charleston), Berkeley (Martinsburg), Monongalia
(Morgantown), Cabell (Huntington), Marshall, Ohio (Wheeling).
Expansion: Wood (Parkersburg), Harrison (Clarksburg), Mercer
(Bluefield/Princeton), Raleigh (Beckley), Jefferson WV (Charles Town).

Strongest contributors: Berkeley + Jefferson (Eastern Panhandle —
DC-metro commuter HOAs), Greenbrier-area resort HOAs (caught from
Raleigh sweeps).

## Genuine HOAs (22)

- Blackthorn Mountain Estates (resort)
- Brierwood Section 1
- Cloverdale Heights
- Creekside Condo Owners
- Dry Run Commons Subdivision
- Glade Springs Village Property Owners Association, Inc. (resort —
  merged from "Glade Springs Village POA" duplicate)
- Hidden Point Subdivision
- Imperial Woods, Indian Head, Lake View Owners
- Marina Tower Condominium
- Meadow Land Property Owners
- Misty River Resort, Pleasant Hills, Potomac Overlook Estates
- Sheridan, Spring. Mills Subdivision Unit Owners
- Spruce Hill South, The Backwaters, The Woods, Timberwalk
- Windwood Village Owners

## Cost (Approximate)

| Channel | Spend | Per genuine HOA |
|---|---:|---:|
| DocAI | ~$1.80 | $0.082 |
| OpenRouter | ~$0.20 | $0.009 |
| Serper | ~$0.04 | $0.002 |
| **Total** | **~$2.04** | **~$0.093** |

## Phase 10 Outcomes

- LLM rename: 19 renames + 1 merge (35 → 34)
- Regex+LLM-null delete: 10 entries (Berkeley County Subdivision Ordinance,
  Caroline Horton House Landmark Nomination, Clerk of County Commission
  fragment, Condemnation Deed Restrictions, Grassroots Advocacy Overview,
  In Olympian West Condominium Association, Quarterly Journal-OCC,
  Raleigh County Planning Ordinance, Serenity Ridge Subdivision
  fragment, Subdivision HOA)
- Dedupe merge: Glade Springs Village POA → Property Owners Association
- Final delete: 1 fragment ("West Virginia's 55 counties. ... Owners
  Association")

## Lessons

1. **Eastern Panhandle (Jefferson, Berkeley) was the highest-yield**
   region — DC-metro commuter belt. Worth re-running if a future deeper
   pass is launched.
2. **Greenbrier resort area underrepresented.** The Raleigh-county
   sweep caught Glade Springs Village (a ~3,000-home resort) but didn't
   hit the Greenbrier Sporting Club or Snowshoe HOAs that exist in the
   region. Future expansion: Pocahontas County (Snowshoe), Greenbrier
   County (resort).
3. **WV is small and the keyword-Serper pattern works cleanly here.**
   Lower noise rate than AR (10 of 35 deletes vs AR's 58 of 104).

## Standard Ledger Files

- `state_scrapers/wv/results/wv_20260508_081725_claude/preflight.json`
- `state_scrapers/wv/results/wv_20260508_081725_claude/prepared_ingest_ledger.jsonl`
- `state_scrapers/wv/results/wv_20260508_081725_claude/live_import_report.json`
- `state_scrapers/wv/results/wv_20260508_081725_claude/final_state_report.json`
- `state_scrapers/wv/results/wv_20260508_081725_claude/name_cleanup_unconditional.jsonl`
- `state_scrapers/wv/results/wv_20260508_081725_claude/discover_*.log` (11 files)
