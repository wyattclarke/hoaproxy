# Nebraska HOA Scrape Retrospective

Run id: `ne_20260508_092146_claude` (Tier 0). Seventh state in batch
`overnight_batch_20260508`. 8-county expanded coverage.

## Final Counts

| Metric | Value |
|---|---:|
| Raw bank manifests | 252 |
| Live HOA profiles (post-import) | 129 |
| After LLM rename pass (68 renames + 9 merges) | 50 |
| After delete pass (11) | **109** |

(Note: live count post-cleanup shows 109 because 70+ entries were
LLM-renamed/merged into proper HOA names, dropping the original
duplicate/junk-name rows from the public list while keeping the cleaned
canonical entries. Final 109 is the cleaned production count.)

## Counties

Original: Douglas (Omaha), Lancaster (Lincoln), Sarpy.
Expansion: Hall (Grand Island), Buffalo (Kearney), Madison NE (Norfolk),
Scotts Bluff (Scottsbluff/Gering), Dodge (Fremont).

NE is a **strong-yield state** for Tier 0: Omaha + Lincoln are dense,
Sarpy (Bellevue/Papillion/La Vista/Gretna) is a fast-growing suburban
ring, and the secondary metros each contributed 5–15 HOAs.

## Cost (Approximate)

| Channel | Spend | Per genuine HOA |
|---|---:|---:|
| DocAI | ~$2.40 | $0.022 |
| OpenRouter | ~$0.40 | $0.004 |
| Serper | ~$0.05 | $0.0005 |
| **Total** | **~$2.85** | **~$0.026** |

Per-HOA cost is the lowest in the batch so far — NE's high-yield universe
amortized the fixed costs well.

## Phase 10 Outcomes

**LLM unconditional cleanup (129 → 50 visible after merges):** 68 renames
applied + 9 merges. The merge count is the highest in the batch, which
reflects multi-document HOAs where the bank pipeline produced two
slightly-different names per HOA from two separate recorded covenants.

**Delete pass (11):** Recording stamps (`420 Condominium Association,
Inc.`), Lincoln Federal Savings Bank fragment, Aspen Condominium
Reservation Agreement, Cimarron-Woods file-extension fragment, Community
Development Agency Proceedings, Gretna Stone LLC fragment, Hamilton
County fragment, etc.

## Lessons

1. **Sarpy County is the densest in the batch.** Bellevue, Papillion,
   La Vista, and Gretna are fast-growing suburbs of Omaha with many
   recent planned communities. The Sarpy sweep alone was the biggest
   single-county contributor in NE.
2. **Renames + merges dominated the cleanup.** This is consistent with
   the AK pattern: the same HOA showing up under two slightly-different
   names from two recorded documents. Worth folding the dedupe-merge
   step into the canonical Phase 10 workflow.
3. **Per-HOA cost lowest in the batch.** NE shows that keyword-Serper
   scales well when the universe is dense.

## Standard Ledger Files

- `state_scrapers/ne/results/ne_20260508_092146_claude/preflight.json`
- `state_scrapers/ne/results/ne_20260508_092146_claude/prepared_ingest_ledger.jsonl`
- `state_scrapers/ne/results/ne_20260508_092146_claude/live_import_report.json`
- `state_scrapers/ne/results/ne_20260508_092146_claude/final_state_report.json`
- `state_scrapers/ne/results/ne_20260508_092146_claude/name_cleanup_unconditional.jsonl`
- `state_scrapers/ne/results/ne_20260508_092146_claude/discover_*.log` (8 files)
