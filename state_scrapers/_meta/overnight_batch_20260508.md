# Overnight Batch 2026-05-08 — Meta Retrospective

10-state sequential run executed by a single Claude Code session as
`overnight_batch_20260508`. All 10 states completed with retrospectives
committed and pushed.

## Headline

**633 genuine HOAs banked, ingested, cleaned, and deployed across 10
small/medium states for ~$18 of DocAI** — well under the $200 batch
ceiling and roughly $0.028 per genuine HOA. The 10-county-per-state
expansion (operator-directed mid-batch after thin SD/ND yields) was the
single biggest contributor to the final yield: AR–MT averaged ~80 live
HOAs each vs SD/ND's combined 19.

## Per-State Final Counts

| # | State | Tier | Bank manifests | Live (genuine) | Map points | Cost ($) |
|---|---|---:|---:|---:|---:|---:|
| 1 | SD | 0 | 79 | 7 | 1 | ~0.40 |
| 2 | ND | 0 | 87 | 12 | 0 | ~0.63 |
| 3 | AK | 0 | 213 (incl. supp) | 33 | 5 | ~1.43 |
| 4 | AR | 0 | 254 | 38 | 5 | ~2.85 |
| 5 | MS | 0 | 262 | 53 | 4 | ~2.55 |
| 6 | WV | 0 | 219 | 21 | 1 | ~2.04 |
| 7 | NE | 0 | 252 | 107 | 13 | ~2.85 |
| 8 | NM | 0 | 299 | 77 | 0 | ~3.25 |
| 9 | OK | 1 | 299 | 132 | 9 | ~3.76 |
| 10 | MT | 1 | 394 | 153 | 5 | ~4.11 |
| | **Total** | | **2,558** | **633** | **43** | **~$23.87** |

(Rounded; cumulative DocAI billing on the GCP project moved from $146.88
to $164.91, a delta of $18.03. The remaining ~$6 is OpenRouter +
Serper + the rename-fix deploy roundtrips.)

## Cross-State Patterns

### Discovery / yield

1. **Per-state county count matters.** SD and ND were specced at 4
   counties each and produced 7 and 12 genuine HOAs. After operator
   feedback ("err on the side of more counties"), AR onward used 8–12
   counties each and yields jumped to 38–153. **Do not undersize county
   coverage; the 10-county minimum should be canonical for Tier 0/1
   keyword-Serper runs.**
2. **The bank-pipeline noise rate is 50–70%.** Even on dense states,
   half or more of bank manifests are not genuine HOAs (recorded
   documents that match `"homeowners association" declaration filetype:pdf`
   include city ordinances, planning packets, plat-page extracts, title
   insurance forms, utility newsletters, legal-scholarship papers, and
   bar-association seminar PDFs). The Phase 10 LLM rename + delete
   passes are now load-bearing, not optional.
3. **Resort/ranch counties are high-yield.** AR's Garland (Hot Springs
   Village family, ~3 sub-associations of one major resort), MT's
   Madison MT (Big Sky), MT's Ravalli (Bitterroot ranch HOAs), and
   WV's Raleigh (Glade Springs Village) each contributed disproportionately.
   For future runs, ALWAYS include the state's primary resort county
   in the county set.

### Bank-stage `is_dirty()` patterns surfaced (worth folding into `hoaware/name_utils.py`)

- **Recording-stamp prefixes:** `^\d+r-?\d`, `^Doc[#]`, `^\d+\s+[A-Z]`
- **"Of " fragment prefixes** (NM-prevalent): `^Of\s+`, `^of [A-Z]`
- **Plat-page extracts** (OK-prevalent): `Curve Table`, `BLOCKS \d+-\d+`,
  `Certificate of Dedication`
- **Government boilerplate:** `\bCity of\b`, `Code of Ordinances`,
  `Planning (Department|Commission)`, `Subdivision Regulations?`,
  `Comprehensive Plan`, `Unified Development Ordinance`
- **OK-specific:** `DEED OF DEDICATION AND RESTRICTIVE`
- **MT-specific:** `Wilderness Area`, `Forest Service`, `Conservation Easement`,
  `Irrigation District`, `Grazing`, `Fishing Access`
- **OCR garbage:** `\bilil\b`, `\billil\b`, `\bH OA$`, `\.pdf HOA$`
- **Single-word fragments:** `^[A-Z][a-z]+$`, `^\w+\s+HOA$`

These should be promoted from per-state cleanup scripts into the canonical
`is_dirty()` regex set; the playbook callout #14 covers the broader
hard-delete policy.

### Phase 10 cost shape

DocAI dominates, but the per-state cap is generous compared to actual
spend:
- Tier 0 cap: $10. Actual spend: $0.40–$3.25 (well under).
- Tier 1 cap: $25. Actual spend: $3.76–$4.11 (well under).

The batch's real cost ceiling is OpenRouter (cumulative ~$3 across all 10
states' rename passes) and Serper (~$0.50 across all per-county sweeps),
which are both negligible relative to DocAI.

### Server-side bug uncovered

Mid-batch (state SD), the `/admin/rename-hoa` endpoint was found to break
doc-fetch for renamed HOAs (HTTP 400 "Document does not belong to
requested HOA"). Root cause: the prefix string check on
`documents.relative_path` doesn't survive a rename because rename only
updates `hoas.name`, not the relative_path or the on-disk dir.

Fix shipped in commit `b935807`: replaced the prefix check with a
documents-table ownership lookup. Deployed via `rebuild` skill. All 633
final HOAs verified doc-fetchable post-deploy.

This invalidated the earlier `[non-HOA] ` rename-tag closing step; the
playbook now mandates `/admin/delete-hoa` over tagging (Phase 10 +
cross-state lesson #14).

## Recommendations For The Next Batch

The user requested the next 10-state queue earlier in this session. The
recommended set (Tier 0/1, smallest first):

1. **DC** (Tier 0, <1,500 CAI) — open-portal (DC Recorder of Deeds)
2. **HI** (Tier 1, 1,600 CAI) — condo-registry (HI Bureau of Conveyances)
3. **IA** (Tier 1, <3,000 CAI) — county-recorder
4. **ID** (Tier 1, <3,000 CAI) — county-recorder
5. **KY** (Tier 1, 2,500 CAI) — Southern county-recorder
6. **AL** (Tier 1, >3,000 CAI) — Southern county-recorder
7. **LA** (Tier 1, 2,200 CAI) — parish-based (adapt slugs)
8. **NV** (Tier 1, 3,800 CAI) — Clark/Washoe-heavy
9. **UT** (Tier 1, 3,700 CAI) — county-recorder + LDS-region
10. **ME** (Tier 1, <2,000 CAI) — only if prior ME session is confirmed
    fully stopped; otherwise replace

**For the next batch, lock in these defaults:**
- Min 10 counties per state (the SD/ND undersizing was the biggest
  yield miss in this batch).
- Add state's primary resort county explicitly (HI Maui, NV Lake Tahoe
  side, UT Park City/Wasatch, KY Lake Cumberland).
- Pre-load the regex set above into the cleanup script as the new
  baseline; the per-state iteration in this batch should not need to
  recur for those classes.
- Use `/admin/delete-hoa` directly for residuals; don't tag-then-delete.

## Stop Conditions That Did NOT Trigger

Per the kickoff, the following would have stopped the queue:
- GCP `hoaware` shutoff: did not happen (cost ~$165 of $600 cap)
- JWT_SECRET rotation: did not happen
- Same operational failure 3+ consecutive states: only one repeat
  pattern (the rename-doc-fetch bug, fixed mid-batch)
- Cumulative DocAI > $200: did not happen ($18.03 batch delta)

Clean batch execution.

## Artifacts

All 10 retrospectives committed to master:
- `state_scrapers/sd/notes/retrospective.md`
- `state_scrapers/nd/notes/retrospective.md`
- `state_scrapers/ak/notes/retrospective.md`
- `state_scrapers/ar/notes/retrospective.md`
- `state_scrapers/ms/notes/retrospective.md`
- `state_scrapers/wv/notes/retrospective.md`
- `state_scrapers/ne/notes/retrospective.md`
- `state_scrapers/nm/notes/retrospective.md`
- `state_scrapers/ok/notes/retrospective.md`
- `state_scrapers/mt/notes/retrospective.md`

Per-state run-id directories (e.g.
`state_scrapers/sd/results/sd_20260508_023534_claude/`) contain the
preflight, prepare ledger, import report, name-cleanup JSONL, final
state report, and per-county discover logs. Those directories are
gitignored (per `state_scrapers/**/results/`); the retrospectives
summarize the contents.

Queue state file: `state_scrapers/_meta/overnight_batch_20260508_state.json`.
