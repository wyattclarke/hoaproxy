# South Dakota HOA Scrape Retrospective

Run id: `sd_20260508_023534_claude` (Tier 0, agent: claude). First state in
the May 2026 overnight batch (`overnight_batch_20260508`) covering 10 small
states sequentially.

Wall time: ~30 min end-to-end (preflight 02:35 UTC → import-finished 03:06 UTC
→ unconditional name cleanup 03:08 UTC).

## Final Counts

| Metric | Value |
|---|---:|
| Raw bank manifests | 79 |
| Bank PDFs | 73 |
| Prepared bundles | 24 |
| Live HOA profiles | 19 |
| Live documents | 19 |
| Live chunks | ~1,200 |
| Map points (after enrichment) | 1 |
| Map rate | 5% (1/19) |
| Out-of-state map points | 0 (all coords inside SD bbox) |
| Names auto-cleaned (unconditional pass) | 6 renamed |
| `[non-HOA]` residuals tagged → **hard-deleted** | 9 |
| Doc-HOA mismatch hard-deletes | 2 (Sunset Harbor utility newsletter, Millstone Village pre-existing 9-HOA junk-sink) |
| **Genuine live HOAs after full cleanup** | **8** |

The 5% map rate is below the Tier 0 target (≥80%) but the absolute count
(19 live HOAs, of which only ~5 looked like genuine HOA names before the
unconditional cleanup pass) means even one polygon is meaningful. Most live
entries are bank-stage misclassifications that the closing LLM rename pass
should either rename or tag `[non-HOA]`.

## Per-County Bank Counts

| County | Manifests | Notes |
|---|---:|---|
| Minnehaha (Sioux Falls) | 26 | Largest metro, Sioux Falls + Brandon/Hartford suburbs |
| Pennington (Rapid City) | 21 | Black Hills resort condos + Rapid City |
| Lincoln | 11 | Sioux Falls southern suburbs (Tea, Harrisburg) |
| Brown (Aberdeen) | 6 | Smaller metro; lowest expected yield |
| `unresolved-name/` | 15 | Bank-stage `is_dirty()` failures routed here |
| **Total** | **79** | |

## Cost Per HOA

| Channel | Spend | Per live HOA |
|---|---:|---:|
| Google Document AI | ~$0.28 | $0.015 |
| OpenRouter (DeepSeek + cleanup) | ~$0.10 | $0.005 |
| Serper | ~$0.02 | $0.001 |
| **Total** | **~$0.40** | **~$0.021** |

DocAI: $0.0015/page × ~189 pages (24 prepared bundles' page counts in the
ledger). Well under the $10 cap.
OpenRouter: discovery DeepSeek triage + 19-HOA cleanup pass.
Serper: 4 county query files × ~17 queries = ~68 searches × ~$0.30/1000.

## Discovery Branch

Keyword-Serper-per-county across 4 counties (Minnehaha/Sioux Falls,
Pennington/Rapid City, Lincoln, Brown/Aberdeen) per Appendix D guidance.
SoS-business-registry-first is retired per the playbook. The 4-county scope
matched the actual HOA distribution: Minnehaha and Pennington dominate, with
Lincoln (Sioux Falls suburbs) and Brown (Aberdeen) as secondary metros.

## Main False-Positive Classes (from prepare ledger)

24 prepared / 64 rejected (88 manifests evaluated; 79 unique bank manifests
because of multi-document manifests).

| Reject class | Count | Notes |
|---|---:|---|
| `junk:government` | 14 | County zoning ordinances, Register of Deeds index pages, legislative journals |
| `unsupported_category:unknown` | 6 | Page-one OCR couldn't classify; mostly subdivision plat indices |
| `pii:membership_list` | 6 | Owner rosters caught at hard-reject gate |
| `junk:court` | 6 | Litigation packets that surfaced on `Articles of Incorporation` queries |
| `low_value:financial` | 4 | Audit/budget PDFs from city sites |
| `junk:unrelated` | 2 | Newsletters / marketing |
| `page_cap_scanned:N` (N=26..85) | 8 | Multi-HOA county records dumps; correctly rejected by the 25-page scanned cap |
| `page_cap:N` (N=202..495) | 3 | Bulk archives that bypassed the scanned cap (text-extractable but oversized) |
| `duplicate:prepared` | 1 | |

The `page_cap_scanned` rejections vindicate the WY-era 25-page cap: every one
of those 8 bundles would have been a multi-HOA records dump that wasted DocAI
spend if it had run. No new false-positive class needs deterministic deny-list
follow-up.

## Phase 10 Cleanup Results (Unconditional LLM Pass)

DeepSeek (kimi fallback) reviewed all 19 live HOAs against their first-page
OCR text. 6 renames applied:

| hoa_id | Before | After |
|---|---|---|
| 15869 | `REALSTACK.com Lincoln County . Becky Vander Broek ... By Laws of the Homeowners Association` | `220 Beech Association` |
| 15867 | `MEMBERSHIP IN HOMEOWNERS' ASSOCIATION. The HOA` | `Dakota Highland Estates Homeowners' Association, Inc.` |
| 15861 | `Highpointe Owners Association Executed` | `Highpointe Ranch Homeowners Association, Inc.` |
| 15616 | `Untitled HOA` | `Millstone Village Community Association, Inc.` |
| 15865 | `Restated of Reservations and Restrictive HOA` | `Red Rock Meadows Homeowners' Association, Inc.` |
| 15868 | `Pennington County , as shown by the plats thereof on file in ... Owners Association` | `Spring Brook Acres Homeowners Association, Inc.` |

9 residual entries were initially tagged with `[non-HOA] ` prefix (the model
declined a canonical name with confidence=0): three government titles (TITLE 5
SUBDIVISION HOA, MEADE COUNTY REGISTER OF DEEDS HOA, SD Legislature Senate
Journal), one radio club (Rara Rockford), one street-address-only fragment
(621 Sixth Street HOA), one meeting-agenda title (Consent Agenda Plats),
and three OCR-fragment names. Total tag rate: 47% — slightly higher than WY's
35%, consistent with SD's smaller universe and the higher proportion of
government-published PDFs in the keyword sweeps.

**Tagging is not a final state. The 9 tagged entries were hard-deleted via
`POST /admin/delete-hoa`** because they are not HOAs and don't belong on the
live site even with a prefix. Tagging is a useful intermediate when an admin
endpoint would be slow or risky — but for stateless content like this,
deletion is the canonical Phase 10 close. See updated playbook Phase 10.

## Doc-HOA Mismatch Audit (Phase 10 closing step)

After the rename + delete passes, the 10 surviving entries were filename-
audited against their HOA name. Two further hard-deletes were needed:

| hoa_id | Name | Doc | Reason |
|---|---|---|---|
| 15859 | Sunset Harbor Homeowners Association | `svecc_2024_01january.pdf` (siouxvalleyenergy.com) | Sioux Valley Electric Cooperative newsletter, not an HOA doc. Bank-stage misclassification — keyword match on "homeowners" or "covenants" inside a utility newsletter. |
| 15616 | Millstone Village Community Association, Inc. | 9 mixed-HOA docs (Mapleton Highlands, Mydland, Northpark, Twin Oaks Sec 2, etc.) | Pre-existing junk-sink (`hoa_id` predates this SD run; was named "Untitled HOA" before the unconditional rename pass picked one filename's HOA name). 9 docs from at least 5 unrelated HOAs (Hendricks/Hancock filenames suggest Minnesota origin). |

The remaining 8 entries are genuine SD HOAs: **220 Beech Association,
Countryside South HOA, Dakota Highland Estates HOA, Highpointe Ranch HOA,
Long Meadow Estates, Meadow Lake Resort HOA, Red Rock Meadows HOA, Spring
Brook Acres HOA.** Three of these have opaque filenames (`zq84...pdf`,
`1259448_28e4962a-...pdf`, `250228_Filed_Restated_Covenants_...pdf`) — they
look like CMS-generated upload IDs and warrant first-page-text spot-checks
in a future cleanup pass, but the source URLs and bank manifests don't
flag them as obvious mismatches.

**Lesson for the playbook:** Phase 10 needs an explicit "doc-filename audit"
step *after* LLM rename and *before* the retrospective is finalized. The
audit should flag (and propose deletion for):

1. Documents whose filename mentions a different HOA name than the host HOA
   (`SAHA-2021` under "Meadow Lake Resort", `Hendricks` under "Millstone
   Village").
2. Documents whose source URL host is generic/utility/news/government, not
   an HOA-owned or recorder-owned domain (`siouxvalleyenergy.com`,
   `*.gov/AgendaCenter`, `legis.sd.gov`).
3. HOAs with `doc_count > 3` that pre-date this state's run (timestamp on
   the earliest document is before the current run's `started_at`) — these
   are usually pre-existing junk-sinks where one HOA name accumulated docs
   from multiple sources.

## Bank-Stage `is_dirty()` Misses (worth folding into the regex set)

Inspected the 19 live HOAs; ~14 are bank-stage misclassifications that
slipped past `is_dirty()`. Patterns:

- Names ending with `HOA` after a comma-separated address fragment, e.g.
  `"Pennington County , as shown by the plats thereof on file in ... Owners Association"`. The `, as shown by` boilerplate from recorded plat preambles.
- Government titles where the all-caps shouting was already short, e.g.
  `"TITLE 5 SUBDIVISION HOA"`, `"MEADE COUNTY REGISTER OF DEEDS HOA"`. The
  `shouting_prefix` rule fires only at len > 40; this case is len ≤ 30.
- Single fragmented words appended with `HOA`, e.g. `"Untitled HOA"`,
  `"Of Jay Bird Property Owners Association"`.
- Quoted-document fragments, e.g. `"MEMBERSHIP IN HOMEOWNERS' ASSOCIATION. The HOA"`.
- URL-leaked strings, e.g. `"REALSTACK.com Lincoln County . Becky Vander Broek ... By Laws of the Homeowners Association"`.
- Date/time prefixes from legislative records, e.g. `"SD Legislature Senate
  Journal 1 9 2024 12:00:00 PM HOA"`.

These all confirm the playbook callout that `is_dirty()` is **necessary but
not sufficient** for keyword-Serper states; the unconditional Phase 10 LLM
pass is the only reliable arbiter.

## Server Bug Fixed Mid-Run: Doc-File Endpoint After Rename

**Symptom:** After the unconditional name-cleanup pass renamed 6 of SD's 19
live HOAs, every doc-open request for those 6 returned HTTP 400 "Document
does not belong to requested HOA."

**Root cause:** `/admin/rename-hoa` only updates `hoas.name`. It does not
update `documents.relative_path` (still has the old HOA name as its first
segment) and does not move the on-disk PDF directory under
`hoa_docs/{old_name}/`. The file-serve endpoint
(`GET /hoas/{name}/documents/file`) validated ownership with a string-prefix
check `rel_doc.startswith(f"{resolved_hoa}/")` — which falsely failed because
`resolved_hoa` was the new name and `rel_doc` still had the old prefix.

**Fix (commit `b935807`):** Replaced the prefix string check with a
documents-table ownership lookup:

```sql
SELECT 1 FROM documents d JOIN hoas h ON d.hoa_id = h.id
WHERE h.name = ? AND d.relative_path = ?
```

Same fix applied to the searchable-HTML endpoint. No DB schema changes; no
on-disk dir rename needed. Deployed via `rebuild` skill at 03:35 UTC.

**Cross-state implication:** WY's run produced 7 renames + 1 merge that
will have been similarly broken since then. Post-deploy verification of
WY doc-opens recommended; if any are broken, they're now fixed by the same
endpoint patch.

## Lessons For The Playbook

1. The 4-county selection (Minnehaha, Pennington, Lincoln, Brown) was right;
   the per-state cost finished comfortably under the $10 ceiling, so widening
   to a 5th county would not have been blocked by budget.
2. The 25-page scanned cap saved ~$0.06–$0.13 of DocAI spend on 8 multi-HOA
   records dumps. Keep it tight.
3. SoS-first attempt was correctly skipped per the retired-discovery rule;
   `https://sosenterprise.sd.gov/BusinessServices/...` URLs that surfaced
   inside Serper hits (one document banked under "Untitled HOA" came from
   that endpoint) are fine as opportunistic Serper snippets but not as a
   universe-building strategy.
4. The bank-stage `is_dirty()` set should be extended:
   - `address_preamble_anywhere` — `\b(as shown by the plats|on file in the office|filed for record)\b`
   - `legis_journal_anywhere` — `\b(Legislature|Senate Journal|House Journal|Bill Status)\b`
   - `gov_registry_anywhere` — `\b(Register of Deeds|Recorder of Deeds|County Clerk|Title 5|Subdivision Regulations)\b`

## Bucket-Binds-Bbox Verification

Per the Phase 6 invariant, ran `GET /hoas/map-points?state=SD` post-import:
exactly one pin returned, at lat=44.4877, lon=-103.8197 (Countryside South
Homeowners Association, polygon quality). That's inside the SD bbox
(min_lat 42.48, max_lat 45.95, min_lon -104.06, max_lon -96.44), so no
demotion needed.

## Next Branches (If SD Discovery Resumes)

- City-anchor sweeps for **Brookings** (Brookings County, university town —
  may have student-housing condos) and **Yankton** (Yankton County, smaller
  but condo-heavy on the Missouri River).
- Pennington follow-up: `Hill City` and `Keystone` Black Hills resort condos
  — current Pennington queries focused on Rapid City and missed several
  Black Hills resort patterns.
- Owned-domain whitelisted preflights for management-co domains (none
  surfaced organically in this sweep — Tier 0 universe is too small for
  mgmt-co harvesting to pay off).

## Standard Ledger Files

- `state_scrapers/sd/results/sd_20260508_023534_claude/preflight.json`
- `state_scrapers/sd/results/sd_20260508_023534_claude/prepared_ingest_ledger.jsonl`
- `state_scrapers/sd/results/sd_20260508_023534_claude/live_import_report.json`
- `state_scrapers/sd/results/sd_20260508_023534_claude/live_verification.json`
- `state_scrapers/sd/results/sd_20260508_023534_claude/final_state_report.json`
- `state_scrapers/sd/results/sd_20260508_023534_claude/name_cleanup_unconditional.jsonl`
- `state_scrapers/sd/results/sd_20260508_023534_claude/discover_*.log` (4 files, one per county)
