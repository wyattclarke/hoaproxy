# North Dakota HOA Scrape Retrospective

Run id: `nd_20260508_031314_claude` (Tier 0, agent: claude). Second state in
the May 2026 overnight batch (`overnight_batch_20260508`).

Wall time: ~30 min end-to-end (preflight 03:13 UTC → import-finished 03:44 UTC
→ unconditional name cleanup 03:48 UTC → delete pass 03:50 UTC).

## Final Counts

| Metric | Value |
|---|---:|
| Raw bank manifests | 87 |
| Bank PDFs | 77 |
| Prepared bundles | 38 |
| Live HOA profiles (post-import) | 35 |
| Live after Phase 10 cleanup (10 renames + 2 merges) | 33 |
| Live after non-HOA delete pass | 12 |
| **Genuine live HOAs** | **12** |
| Live documents | 13 |
| Map points | 0 |
| Map rate | 0% (no usable centroids in this run) |
| Out-of-state map points | 0 |
| Doc-fetch verification (post-deploy of rename-fix) | 13/13 OK |

## Per-County Bank Counts

| County | Manifests | Notes |
|---|---:|---|
| Cass (Fargo) | 31 | Largest metro; Fargo + West Fargo |
| Burleigh (Bismarck) | 25 | Capital + Lincoln suburb |
| Grand Forks | 15 | University town |
| Stark (Dickinson) | 7 | Smallest metro |
| `unresolved-name/` | 9 | Bank-stage `is_dirty()` failures |
| **Total** | **87** | |

## Cost Per HOA

| Channel | Spend | Per genuine HOA |
|---|---:|---:|
| Google Document AI | $0.51 (338 pages × $0.0015) | $0.042 |
| OpenRouter (DeepSeek + cleanup) | ~$0.10 | $0.008 |
| Serper | ~$0.02 | $0.002 |
| **Total** | **~$0.63** | **~$0.052** |

ND was cheaper per-bank than SD (per-page cost similar) but the genuine-HOA
yield was lower (12 of 87 manifests = 14%), so per-genuine-HOA cost is
roughly 2.5× SD's. This is consistent with the playbook expectation that
sparser-population states have lower-density signal in keyword-Serper sweeps.

## Discovery Branch

Keyword-Serper-per-county across 4 counties: Cass (Fargo), Burleigh
(Bismarck), Grand Forks, Stark (Dickinson). Per Appendix D guidance.

## Genuine HOAs (12)

After two-stage Phase 10 cleanup (LLM rename + non-HOA delete + doc-filename
audit):

| hoa_id | Name | County (approx) |
|---|---|---|
| 13756 | Oxbow Homeowners Association | Cass (Oxbow, ND) — pre-existing entry, doc validated |
| 15988 | Heritage Park Association | Cass / Burleigh |
| 15992 | The Ranch Second Subdivision HOA | Cass |
| 15993 | Misty Waters Owners' Association | Cass |
| 15994 | Southbay Homeowners Association | Cass |
| 15998 | Ashmoor Glen Fourth HOA | Cass |
| 16000 | Crofton Coves Homeowners Association, Inc. | Cass |
| 16003 | Highland Park Community Services Association, Inc. | Burleigh |
| 16005 | Martens Way Homeowners Association | Cass |
| 16008 | Lakeview Homeowners' Association | Burleigh |
| 16009 | River's Bend at the Preserve Homeowner's Association | Burleigh |
| 16011 | Autumn Woods Estates Association, Inc. | Cass |

The Cass-heavy distribution matches the underlying universe: Fargo has the
largest HOA count in ND.

## Phase 10 Outcomes

**LLM unconditional cleanup (35 live → 33):**
- 10 renames proposed; 8 applied as renames + 2 applied as merges.
- 14 entries got a `canonical_name`; 21 were null (LLM rejected).

**Hard-delete pass (33 → 12):** 21 entries deleted. The deletion list
combined the LLM's null-canonical decisions (where confidence ≥ 0.5) with a
deterministic regex over the live names that catches:

| Pattern class | Examples |
|---|---|
| Recording stamps / pagination | `1 of 16 7 27 2021 12:02 PM AMRST $65.00 HOA`, `1 of 9 HOA` |
| Title companies | `Bismarck Title Company HOA` |
| Government bodies | `Grand Forks Code of Ordinances HOA`, `The City of Fargo HOA`, `Planning & Zoning Commission Proceedings HOA` |
| Utilities / REITs | `XCEL ENERGY INC HOA`, `Presidio Property Trust, Inc HOA` (SEC 10-K) |
| Law firms | `Tschider & Smith Law HOA` |
| Developer / brokers | `Heritage Development HOA`, `Crary Real Estate HOA` |
| OCR fragments | `Of , Obligations HOA`, `of Additional , Conditions, , and HOA`, `Of Additional HOA`, `PROPERTY HOA`, `CC&Rs HOA`, `Crary S Seventh`, `Lostriverdev Th Addition`, `Nated Representative` |
| Boilerplate phrases | `Corps or the Health Department. The cost to be the responsibility of the Homeowners Association` |
| Generic suffix garbage | `Woodhaven 4th Addtn .pdf HOA` |
| Junk-sink | `Untitled HOA` (hoa_id 15915, 3 docs) |

**Doc-filename audit:** None of the 12 keepers had filename mismatches or
suspect source-URL hosts (per the new playbook step). One pre-existing
junk-sink (`Untitled HOA` / hoa_id 15915) was caught by the regex pass and
deleted.

## Main False-Positive Classes (from prepare ledger)

86 ledger rows: 38 prepared + 48 rejected.

| Class | Count | Notes |
|---|---:|---|
| `junk:unrelated` | 9 | Generic legal docs, real-estate listings |
| `junk:government` | 7 | City/county packets, zoning docs |
| `low_value:financial` | 3 | Audits / budgets |
| `unsupported_category:unknown` | 2 | Page-one OCR couldn't classify |
| `junk:court` | 1 | Court packet |
| `page_cap:227..360` (text-extractable bulk) | 6 | Long city ordinance dumps |
| `page_cap_scanned:27..46` | 4 | Scanned multi-HOA filings |

The 4 scanned `page_cap_scanned:` rejects saved ~$0.21 of DocAI spend on
multi-HOA records dumps — same pattern as SD.

## Server Bug Fixed Mid-Run

The `/admin/rename-hoa` → 400-on-doc-fetch bug (full root-cause in SD
retrospective + cross-state lesson #14) shipped before ND's import phase, so
ND's renamed entries were never broken. All 13 ND docs verified fetchable
post-cleanup.

## Lessons For The Playbook

1. The 4-county scope was right. Stark (Dickinson) returned only 7
   manifests; rural ND counties below Stark would have been pure noise.
2. Per-state non-HOA-deletion regex is reusable across keyword-Serper
   states. Worth extracting into a sibling of `clean_dirty_hoa_names.py`
   that wraps the LLM null-canonical decisions plus a regex pass over
   common name fragments. Pattern set used here:
   - recording stamps (`^\d+ of \d+\b`)
   - title companies (`\bTitle Company\b`)
   - utilities (`\bENERGY INC\b`)
   - REITs (`\bProperty Trust, Inc\b`)
   - law firms (`\bLaw\s+HOA$`)
   - government (`\bCity of\b`, `\bCode of Ordinances\b`,
     `\bPlanning\b.*Commission\b`)
   - file-extension leakage (`\.pdf HOA$`)
   - "Of " / "of " fragment prefixes
   - "Untitled" prefix (junk-sinks)
3. Map rate is 0% — none of the 12 genuine HOAs surfaced a usable
   city/ZIP/polygon. ND-specific follow-up: Phase 6 enrichment could pull
   ZIP centroids from the recorded plat references in the OCR text. Out
   of scope for this overnight batch; flagged for next pass.

## Standard Ledger Files

- `state_scrapers/nd/results/nd_20260508_031314_claude/preflight.json`
- `state_scrapers/nd/results/nd_20260508_031314_claude/prepared_ingest_ledger.jsonl`
- `state_scrapers/nd/results/nd_20260508_031314_claude/live_import_report.json`
- `state_scrapers/nd/results/nd_20260508_031314_claude/live_verification.json`
- `state_scrapers/nd/results/nd_20260508_031314_claude/final_state_report.json`
- `state_scrapers/nd/results/nd_20260508_031314_claude/name_cleanup_unconditional.jsonl`
- `state_scrapers/nd/results/nd_20260508_031314_claude/discover_*.log` (4 files, one per county)
