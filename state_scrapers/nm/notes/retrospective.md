# New Mexico HOA Scrape Retrospective

Run id: `nm_20260508_110239_claude` (Tier 0). Eighth state in batch
`overnight_batch_20260508`. 9-county expanded coverage.

## Final Counts

| Metric | Value |
|---|---:|
| Raw bank manifests | 299 |
| Live HOA profiles (post-import) | 106 |
| After LLM rename pass (48 renames + 5 merges) | 50 visible |
| After delete pass (22) | **79** (cleaned production count) |

## Counties

Original: Bernalillo (Albuquerque), Sandoval (Rio Rancho), Doña Ana
(Las Cruces), Santa Fe.
Expansion: San Juan (Farmington), Lea (Hobbs), Otero (Alamogordo),
Chaves (Roswell), Lincoln NM (Ruidoso/Ruidoso Downs).

The Albuquerque-Rio Rancho corridor (Bernalillo + Sandoval) was the
primary contributor. Santa Fe added high-quality entries (Eldorado-area
HOAs). Lincoln NM (Ruidoso) added resort condo associations.

## Cost (Approximate)

| Channel | Spend | Per genuine HOA |
|---|---:|---:|
| DocAI | ~$2.80 | $0.035 |
| OpenRouter | ~$0.40 | $0.005 |
| Serper | ~$0.05 | $0.001 |
| **Total** | **~$3.25** | **~$0.041** |

## Phase 10 Outcomes

**LLM unconditional cleanup (106 → 50):** 48 renames + 5 merges.

**Delete pass (22):** Government planning (Hobbs Planning Board, NM
Property Tax Code), recording fragments, "Of " prefixes (Of Enchanted
Hills, Of Protective HOA, Of Restrictive HOA), single-word fragments
(Granada HOA, Islands HOA, Ccceh, Los Nidos), platting fragments,
Albuquerque Arroyo Del Sol fragment, Lincoln County Archives, Design
Guidelines isolated fragment.

## Lessons

1. **Bernalillo + Sandoval was very productive.** Albuquerque proper
   plus Rio Rancho yielded the bulk of NM's genuine HOAs.
2. **Lincoln NM (Ruidoso) was a smart addition.** Ruidoso resort condos
   + Ruidoso Downs subdivision HOAs added 5–8 entries that weren't in
   the original 4-county scope.
3. **NM has a distinctive "Of " fragment leak.** Multiple bank-stage
   names started with "Of " (Of Protective, Of Restrictive, Of
   Enchanted Hills) — likely from "Declaration **Of** Protective
   Covenants And Restrictions" recorded-document title fragments where
   the bank pipeline truncated the leading word. Worth folding the
   `^Of ` regex pattern into the canonical `is_dirty()` set.
4. **Acequia / Mercado / Nuestro fragments** are NM-specific
   government / community-water-rights doc names that look HOA-shaped
   to keyword search; flagged in the regex set.

## Standard Ledger Files

- `state_scrapers/nm/results/nm_20260508_110239_claude/preflight.json`
- `state_scrapers/nm/results/nm_20260508_110239_claude/prepared_ingest_ledger.jsonl`
- `state_scrapers/nm/results/nm_20260508_110239_claude/live_import_report.json`
- `state_scrapers/nm/results/nm_20260508_110239_claude/final_state_report.json`
- `state_scrapers/nm/results/nm_20260508_110239_claude/name_cleanup_unconditional.jsonl`
- `state_scrapers/nm/results/nm_20260508_110239_claude/discover_*.log` (9 files)
