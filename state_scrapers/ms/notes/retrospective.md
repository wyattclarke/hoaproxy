# Mississippi HOA Scrape Retrospective

Run id: `ms_20260508_063804_claude` (Tier 0, agent: claude). Fifth state in
the May 2026 overnight batch. 10-county expanded coverage.

## Final Counts

| Metric | Value |
|---|---:|
| Raw bank manifests | 262 |
| Live HOA profiles (post-import) | 103 |
| After LLM rename pass (34 renames + 3 merges) | 66 |
| After 2 cleanup-delete passes (22 + 21 = 43 deletions) + 1 merge | **56** |
| Map points | TBD |

## Counties

Hinds (Jackson), Madison, Rankin, DeSoto (Memphis suburbs), Harrison (Gulf
Coast), plus expansion: Lee (Tupelo), Lauderdale (Meridian), Lamar +
Forrest (Hattiesburg), Jackson MS (Pascagoula/Ocean Springs).

The Gulf Coast (Harrison) and the Jackson metro (Hinds/Madison/Rankin)
contributed the bulk of genuine HOAs. DeSoto added Memphis-suburb
condo associations.

## Genuine HOAs (56)

Highlights: Bella Vista–style scale (Hot Springs Village equivalent doesn't
exist in MS), but solid coverage of Jackson-metro planned communities
(Annandale Estates, Beaumont Estates, Bienville Place, Belle Meade,
Bridgefield, Brookleigh, Cannon Ridge, Cypress Point River Club, Dickens
Place, Fairhaven Estates, Henry's Plantation, La Bonne Terre, Lake
Serene, McCormick Woods, Montclair, Northbay, Northshore Landing, Pecan
Ridge, Pembroke Cove, Pine Ridge, Wellington, etc.) plus Gulf Coast
condos, Hattiesburg subdivisions, and Memphis-suburb DeSoto entries.

## Cost (Approximate)

| Channel | Spend | Per genuine HOA |
|---|---:|---:|
| DocAI | ~$2.20 | $0.039 |
| OpenRouter | ~$0.30 | $0.005 |
| Serper | ~$0.05 | $0.001 |
| **Total** | **~$2.55** | **~$0.046** |

## Phase 10 Outcomes

**LLM unconditional cleanup (103 → 66):** 34 renames + 3 merges.

**Two delete passes (66 → 56):** 22 in pass 1 (recording fragments,
ordinances, single-word fragments, OCR garbage), 21 in pass 2 (water
utility, Mississippi Bar association, Madison Zoning, "Of Wellington"
fragment, "Ordinance of Gated Community", "Mississippi Valley Title
Insurance 2014 Agent Seminar"). Plus 1 dedupe merge (Lake Serene
Property Owners Association — two near-duplicate entries).

## Lessons

1. **MS Gulf Coast is condo-heavy.** Harrison county added a substantial
   pool of beachfront condo associations; the expansion to include it
   was clearly correct.
2. **Jackson metro (Hinds/Madison/Rankin) is dense.** Three contiguous
   counties produced ~30 genuine HOAs together — the largest metro
   contribution in this batch outside AR's Hot Springs Village family.
3. **Two passes still left work.** MS keyword-Serper picked up a lot of
   government-published PDFs (zoning code, ordinance archives, Bar
   Association seminar PDFs) that the LLM rename pass renamed but didn't
   flag as null-canonical. Phase 10 is now confirmed as a 2–3 pass
   process, not a single pass.

## Standard Ledger Files

- `state_scrapers/ms/results/ms_20260508_063804_claude/preflight.json`
- `state_scrapers/ms/results/ms_20260508_063804_claude/prepared_ingest_ledger.jsonl`
- `state_scrapers/ms/results/ms_20260508_063804_claude/live_import_report.json`
- `state_scrapers/ms/results/ms_20260508_063804_claude/final_state_report.json`
- `state_scrapers/ms/results/ms_20260508_063804_claude/name_cleanup_unconditional.jsonl`
- `state_scrapers/ms/results/ms_20260508_063804_claude/discover_*.log` (10 files)
