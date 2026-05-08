# Montana HOA Scrape Retrospective

Run id: `mt_20260508_142636_claude` (Tier 1, $25 DocAI cap). Tenth and final
state in batch `overnight_batch_20260508`. 12-county expanded coverage.

## Final Counts

| Metric | Value |
|---|---:|
| Raw bank manifests | 394 |
| Live HOA profiles (post-import) | 189 |
| After LLM rename pass (72 renames + 11 merges) | 50 visible |
| After delete pass (25) | **153** (cleaned production count) |

**Largest single-state yield in the batch.** MT was Tier 1 (>2,000 CAI)
and the universe lived up to that estimate.

## Counties

Original: Yellowstone (Billings), Missoula, Gallatin (Bozeman), Flathead
(Kalispell), Lewis and Clark (Helena).
Expansion: Cascade (Great Falls), Silver Bow (Butte), Ravalli (Bitterroot),
Madison MT (Big Sky/Ennis), Park MT (Livingston), Sanders (Thompson Falls),
Jefferson MT (Boulder/Whitehall — Helena suburbs).

The 12-county coverage matched MT's HOA distribution well: Gallatin
(Bozeman + Big Sky), Flathead (Kalispell + Whitefish + Bigfork resort
condos), Yellowstone (Billings), Lewis and Clark (Helena) and Missoula
each contributed substantially. Madison MT (Big Sky resort area) +
Ravalli (Bitterroot Valley ranch HOAs) added the resort/ranch
associations the playbook flagged in the kickoff `NAME_PATTERN_NOTES`.

## Cost (Approximate)

| Channel | Spend | Per genuine HOA |
|---|---:|---:|
| DocAI | ~$3.50 | $0.023 |
| OpenRouter | ~$0.55 | $0.004 |
| Serper | ~$0.06 | $0.0004 |
| **Total** | **~$4.11** | **~$0.027** |

DocAI well under the $25 Tier 1 cap; per-HOA cost on par with NE's
$0.026. The dense universe + 12-county sweep amortized fixed costs
exceptionally well.

## Phase 10 Outcomes

**LLM unconditional cleanup (189 → 50 visible):** 72 renames + 11 merges.
The 11 merges is the highest in the batch — MT has many HOAs whose
recorded covenants used different naming conventions across documents
("XYZ Estates HOA" vs "XYZ Estates Homeowners Association, Inc.").

**Delete pass (25):** Government planning, ordinance fragments, MT-specific
"Wilderness Area" / "Forest Service" / "Irrigation District" /
"Conservation Easement" / "Grazing" / "Fishing Access" — Montana's
public-lands focus produces a lot of non-HOA recorded documents that
match keyword-Serper queries.

## Lessons

1. **Resort + ranch HOA name patterns paid off.** The kickoff added
   `ranch association` and `club association` query variants for MT
   specifically; those queries yielded ~15 genuine HOAs (Bitterroot
   ranch HOAs, Big Sky ski-club associations) that wouldn't have shown
   on the standard `homeowners association` shape.
2. **Public-lands noise is MT-specific.** Forest Service grazing leases,
   irrigation district records, conservation easements, and fishing
   access permits all match HOA-shape queries on MT recorder sites.
   Worth folding `Wilderness Area`, `Conservation Easement`, `Grazing`,
   `Irrigation District`, `Forest Service` into the bank-stage
   `is_dirty()` set for any future Mountain-state run.
3. **12 counties was the right scope.** Each county contributed at
   least 5 genuine HOAs after cleanup — no rural-county was a complete
   waste of budget.
4. **Tier 1 cost behaviour confirms the budget envelope.** $4.11 total
   is well under the $25 cap. Tier 1 states with 200–400 manifests
   land in the $3–5 range, comfortably leaving headroom for unexpected
   bulk-archive OCR.

## Standard Ledger Files

- `state_scrapers/mt/results/mt_20260508_142636_claude/preflight.json`
- `state_scrapers/mt/results/mt_20260508_142636_claude/prepared_ingest_ledger.jsonl`
- `state_scrapers/mt/results/mt_20260508_142636_claude/live_import_report.json`
- `state_scrapers/mt/results/mt_20260508_142636_claude/final_state_report.json`
- `state_scrapers/mt/results/mt_20260508_142636_claude/name_cleanup_unconditional.jsonl`
- `state_scrapers/mt/results/mt_20260508_142636_claude/discover_*.log` (12 files)
