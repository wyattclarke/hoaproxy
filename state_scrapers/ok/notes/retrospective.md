# Oklahoma HOA Scrape Retrospective

Run id: `ok_20260508_123834_claude` (Tier 1, $25 DocAI cap). Ninth state in
batch `overnight_batch_20260508`. 11-county expanded coverage.

## Final Counts

| Metric | Value |
|---|---:|
| Raw bank manifests | 299 |
| Live HOA profiles (post-import) | 163 |
| After LLM rename pass (62 renames + 1 merge) | 50 visible |
| After delete pass (28) | **134** (cleaned production count) |

## Counties

Original: Oklahoma County (OKC), Tulsa, Cleveland (Norman), Canadian.
Expansion: Comanche (Lawton), Payne (Stillwater/OSU), Pottawatomie
(Shawnee), Rogers (Owasso/Claremore), Wagoner (Broken Arrow east),
Garfield (Enid), Creek (Sapulpa).

The Oklahoma City metro (Oklahoma County + Cleveland + Canadian) and
Tulsa metro (Tulsa + Rogers + Wagoner + Creek) dominated yield. Norman
(Cleveland) and Edmond/Yukon (Canadian/Oklahoma counties) added planned-
community HOAs. Broken Arrow + Owasso (Rogers/Wagoner) added Tulsa
suburban-ring associations.

## Cost (Approximate)

| Channel | Spend | Per genuine HOA |
|---|---:|---:|
| DocAI | ~$3.20 | $0.024 |
| OpenRouter | ~$0.50 | $0.004 |
| Serper | ~$0.06 | $0.0004 |
| **Total** | **~$3.76** | **~$0.028** |

DocAI well under the $25 Tier 1 cap.

## Phase 10 Outcomes

**LLM unconditional cleanup (163 → 100 visible after merges+renames):**
62 renames + 1 merge.

**Delete pass (28):** OK keyword-Serper picked up a lot of recorded
"DEED OF DEDICATION AND RESTRICTIVE" filing fragments, plat-page
"Curve Table" / "BLOCKS 6-9" extracts, and several Oklahoma City /
Tulsa government entries (Costs for Jail City of Tulsa, City of Broken
Arrow municipal). Plus a "13 Rivendellnow Org" OCR garbage and an
"Environmental Response Trust Agreement" fragment.

## Lessons

1. **OK county recorders publish plat sheets as PDFs** — many of which
   match `"homeowners association" declaration filetype:pdf` because
   the plat sheet itself contains the embedded covenants. The bank
   pipeline correctly captured these but the LLM rename pass had to
   reject the plat-page-fragment names ("Curve Table HOA", "BLOCKS 6-9
   HOA"). Worth folding `Curve Table`, `BLOCKS \d+-\d+`, and
   `Certificate of Dedication` patterns into the bank-stage `is_dirty()`
   set.
2. **The "DEED OF DEDICATION AND RESTRICTIVE" boilerplate is OK-specific.**
   OK uses this phrase prominently in recorded covenants; truncated
   versions appear as bank-stage names that should be rejected.
3. **Tier 1 cost stayed Tier-0-shaped.** $3.76 total is well under the
   $25 cap; the DocAI cost growth scaled sub-linearly with bank size
   (299 manifests vs SD's 79 = 3.8x manifests but only ~12x cost).

## Standard Ledger Files

- `state_scrapers/ok/results/ok_20260508_123834_claude/preflight.json`
- `state_scrapers/ok/results/ok_20260508_123834_claude/prepared_ingest_ledger.jsonl`
- `state_scrapers/ok/results/ok_20260508_123834_claude/live_import_report.json`
- `state_scrapers/ok/results/ok_20260508_123834_claude/final_state_report.json`
- `state_scrapers/ok/results/ok_20260508_123834_claude/name_cleanup_unconditional.jsonl`
- `state_scrapers/ok/results/ok_20260508_123834_claude/discover_*.log` (11 files)
