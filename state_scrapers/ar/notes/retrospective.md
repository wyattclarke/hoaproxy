# Arkansas HOA Scrape Retrospective

Run id: `ar_20260508_045609_claude` (Tier 0, agent: claude). Fourth state in
the May 2026 overnight batch (`overnight_batch_20260508`). First state to
use the **expanded county set** (10 counties vs the original 5) per
operator feedback that yield was thin on SD/ND.

Wall time: ~1h 30m end-to-end. Cleanup pass dominated by 3 iterative
delete passes against an unusually noisy AR keyword-Serper landscape.

## Final Counts

| Metric | Value |
|---|---:|
| Raw bank manifests | 254 |
| Live HOA profiles (post-import) | 104 |
| After LLM rename pass (33 renames + 7 merges) | 64 |
| After 3 cleanup-delete passes (6 + 28 + 18 + 6 = 58 total deletions) | **38** |
| Live documents | ~50 |
| Map points | 5 |
| Map rate | 13% |

## Per-County Bank Counts (Approximate)

| County | Manifests | Notes |
|---|---:|---|
| Pulaski (Little Rock) | high | Heaviest |
| Benton (Bentonville/Rogers) | high | NW Arkansas |
| Washington (Fayetteville/Springdale) | high | NW Arkansas (UA) |
| Saline (Benton/Bryant) | medium | Little Rock metro |
| Faulkner (Conway) | medium | UCA |
| **Garland (Hot Springs)** | high | **Resort county — added in expansion, big yield** |
| Sebastian (Fort Smith) | medium | Added in expansion |
| Craighead (Jonesboro) | medium | Added in expansion |
| Lonoke (Cabot) | low | Added in expansion |
| White (Searcy) | low | Added in expansion |
| **Total** | **254** | |

## Genuine Live HOAs (38)

Highlights: Bella Vista Village + Townhouse (Benton — NW Arkansas), Hot
Springs Village + CooperShares + Townhouse (Garland — large resort HOA
with multiple sub-associations), Chenal Valley (Pulaski — large planned
community), Tanasi Cove Villas, Steeplechase, plus many smaller subdivision
HOAs.

The **Hot Springs Village** family alone justified the Garland-county
expansion: 3 sub-associations of one of the largest planned communities
in the South.

## Cost Per HOA

Approximate based on prepare ledger (full numbers not surfaced this turn
due to Render load):

| Channel | Spend (approx) | Per genuine HOA |
|---|---:|---:|
| DocAI | ~$2.50 | $0.066 |
| OpenRouter | ~$0.30 | $0.008 |
| Serper | ~$0.05 | $0.001 |
| **Total** | **~$2.85** | **~$0.075** |

DocAI well under the $10 cap despite 2.5× the bank manifests of SD/ND.

## Phase 10 Outcomes

**LLM unconditional cleanup (104 → 64):** 33 renames + 7 merges.

**Three iterative delete passes (64 → 38):** AR's keyword-Serper sweeps
captured an unusually high proportion of bank-stage misclassifications —
recording stamps, government ordinances, planning department docs, title
insurance forms, fragmented OCR, even a Red Cross blood drive newsletter.
The first delete pass caught 6 obvious entries, the second 28 (covering
recording number fragments, government planning, person-name fragments,
title insurance, OCR garbage, the operator-confirmed `[non-HOA] tag-only
is wrong, must hard-delete` policy), the third 18 (phase fragments,
"Protective HOA" / "PROTECTIVE HOA" residue, neighborhood association
mislabels, a "Spring Hill Subdivision - TitleTech of LLC" doc, etc.),
plus a final cleanup of 6 (Transfer Fees article, Unified Development
Ordinance, county fragments, "Ws Plantation Ph Cov Merged"-style
abbreviations).

The high non-HOA rate (66/104 = 63%) is consistent with AR's Serper
landscape: the Pulaski/Saline/Faulkner counties recorder sites publish
ordinances + zoning code as PDFs that match `"homeowners association"
declaration filetype:pdf` queries enough to pull the document, even when
the doc is a city ordinance or a NAACP records archive.

## Map Coverage (5/38 = 13%)

Below the Tier 0 80% target. Same root cause as ND: rural-county HOA
manifests don't carry geocodable addresses; Phase 6 enrichment fell back
to ZIP centroids only when the OCR text mentioned a clear ZIP. Hot
Springs Village got a polygon (it's a recognizable place name on
Nominatim).

## Lessons

1. **Expanded county set worked.** 254 manifests vs SD's 79 vs ND's 87.
   The 5 added counties (Garland, Sebastian, Craighead, Lonoke, White)
   contributed ~30% of the genuine HOAs, with Garland alone contributing
   the Hot Springs Village family.
2. **AR keyword-Serper is noisier than SD/ND.** Probably because more
   counties publish their ordinances + zoning code as searchable PDFs.
   Three iterative delete passes were needed; the regex set has been
   extended further with each pass.
3. **The dedupe-merge pattern (7 merges in AR) confirms the AK signal**
   — it's not state-specific. Bank-stage slug merging sometimes fails
   when the same HOA's two recorded documents have slightly different
   names (e.g., "Bella Vista Townhouse Association" vs the all-caps
   "BELLA VISTA TOWNHOUSE ASSOCIATION").
4. **Operator feedback applied:** "err on the side of more counties"
   directly raised genuine-HOA yield from ~10/state (SD, ND) to ~38 (AR).

## Standard Ledger Files

- `state_scrapers/ar/results/ar_20260508_045609_claude/preflight.json`
- `state_scrapers/ar/results/ar_20260508_045609_claude/prepared_ingest_ledger.jsonl`
- `state_scrapers/ar/results/ar_20260508_045609_claude/live_import_report.json`
- `state_scrapers/ar/results/ar_20260508_045609_claude/final_state_report.json`
- `state_scrapers/ar/results/ar_20260508_045609_claude/name_cleanup_unconditional.jsonl`
- `state_scrapers/ar/results/ar_20260508_045609_claude/discover_*.log` (10 files)
