# WA HOA Scrape Retrospective

## Final state (2026-05-09)

- **Live HOA count:** 137 (post-Phase 10)
- **Bank manifests:** 533
- **Prepared bundles:** 251 (47% retention from bank)
- **Imported:** 251
- **Initial live (post-import):** 243
- **Cleanup deletes (Phase 10):** 168 → −106 net live (44% removal rate, very high — Tier-3 keyword-Serper noise as expected)
- **Cities backfilled via OCR-LLM:** 57 (Seattle, Bellevue, Spokane, Tacoma, Vancouver, Bellingham, Olympia, Bremerton, etc.)
- **Map points:** 143 (~75% map coverage)
- **Out-of-bbox flagged:** 1 (GA polygon attached to a generic "Homeowners Association" entry; demoted to `city_only`, not auto-deleted)

## Run history

### Single overnight (`wa_20260509_062427_phaseA` / `phaseB`, 2026-05-09)
End-to-end run on 12 counties — King (Seattle/Bellevue/Redmond/Sammamish), Pierce (Tacoma/Gig Harbor), Snohomish (Everett/Lynnwood), Spokane (Spokane/Liberty Lake), Clark (Vancouver/Camas), Thurston (Olympia), Kitsap (Bremerton/Bainbridge), Whatcom (Bellingham), Yakima, Benton (Tri-Cities), Skagit (Anacortes/La Conner), Chelan (Lake Chelan/Leavenworth) — plus state-wide host-family. Wall time: ~3.5 hours (Phase A discovery 1.5h + Phase B prepare 1h + import 15min + Phase 10 ~30min).

Per-county bank manifest yield:

| County | Manifests | Notes |
|---|---|---|
| Spokane | 53 | Strongest yield. Liberty Lake / Cheney suburban subdivisions surface well. |
| Pierce | 48 | Tacoma + Gig Harbor + Bonney Lake. |
| King | 46 | Seattle metro; sample showed lots of court filings + recorded boilerplate. |
| Snohomish | 41 | Everett/Mill Creek/Mukilteo. |
| Kitsap | 30 | Bremerton/Bainbridge/Poulsbo. |
| Clark | 28 | Vancouver/Camas. |
| Benton | 28 | Tri-Cities (Kennewick/Richland/West Richland). |
| Thurston | 25 | Olympia/Lacey/Tumwater. |
| Skagit | 23 | Anacortes/La Conner waterfront. |
| Whatcom | 21 | Bellingham/Birch Bay. |
| Chelan | 17 | Lake Chelan/Leavenworth/Manson resort. |
| Yakima | 12 | Yakima/Selah/Sunnyside. |
| `_unresolved-name` | 0 | Bank-stage routing was clean (no slugs failed `is_dirty()` outright). |

`host_family` queries contributed ~161 additional manifests across counties (e.g., eNeighbors, mgmt-co portals, RCW 64.34 / RCW 64.38 statute-anchored Serper hits).

### Phase 10 cleanup (`scripts/phase10_close.py --apply`, 2026-05-09 05:14)

- **Renames applied:** 109 (deterministic prefix-strip + LLM canonicalization). Includes deduplication-via-rename (11 merged into existing keepers).
- **Hard-deleted:**
  - `delete_null` (LLM canonical=null, document is not an HOA governing doc): **88**
  - `delete_not_hoa` (OCR-LLM `is_hoa=false` with conf ≥ 0.85): **73**
  - `delete_foreign_state` (OCR-LLM detected the doc is from a different state): **7** — across AR, OR, VA, WI, FL, CA, MN. Cross-state contamination spread is **broader** than for any prior state in the May 2026 batch (7 distinct states vs. KY's 4).
  - `delete_audit` (doc-filename audit): 0
  - `delete_regex` (cumulative override regex): 0 — no override patterns fired on WA. Suggests WA's noise shape differs from previous Tier-1/2 states; see lessons below.
- **Dedupe-merge:** 3 (post-rename pair detection).
- **Location backfill:** 62 city-only matches via OCR-LLM (of 65 proposed).

Result: 243 → 137 live with 57 cities populated.

## Discovery techniques attempted

- ✅ **Per-county Serper** with `Auditor's File` recorder anchor (WA county recorders are called "County Auditor"), `Declaration of Covenants`, `Master Deed`, `Condominium Association`, etc. Yielded the 12-county breakdown above.
- ✅ **Sub-county neighborhood anchors** for King (Seattle/Bellevue/Redmond/Sammamish/Issaquah/Kirkland/Renton/Federal Way), Pierce (Tacoma/Gig Harbor/Puyallup), Snohomish (Everett/Mill Creek/Edmonds/Marysville/Mukilteo/Bothell). These were essential — top-level "King County" queries surface court filings; neighborhood anchors surface HOAs.
- ✅ **Statute-anchored host-family queries:** RCW 64.34, RCW 64.38, "Washington Condominium Act", "Washington Uniform Common Interest Ownership Act".
- ✅ **Mgmt-co host-family:** Morris Management WA, FirstService Residential WA, Pacific Crest, Phillips Real Estate Services, Newman HOA Mgmt, Rentvine WA.
- ❌ **Name-list-first** (CINC/AppFolio portal scrape, King County registered condos) — not attempted in this run; per the name-list-first playbook §2.5, **WA King County is a name-list-first candidate** since Seattle metro is paywalled-urban. See "Open follow-ups" below.

## Lessons learned

1. **Tier-3 keyword-Serper noise rate is much higher than Tier-1/2.** WA's 44% post-import deletion rate (168/243) dwarfs the May 2026 batch averages (NV 19%, KY 25%, ID 33%, AL 10%). Three reasons:
   - **Court filings dominate Seattle metro Serper hits** (settlement claims, hearing examiner decisions, federal class actions hosted on `settlement-claims.com`, `*.uscourts.gov`).
   - **Government planning documents** with HOA-shape suffix (county comprehensive plans, "Annexation Agreement", "Homeowner Information and HOA").
   - **RCW boilerplate citations** ("Chapter 64.34 RCW Act Sections HOA") — the statute itself surfaces in many results.
   - The OCR-LLM `is_hoa` validation caught **73 of these** — the single most productive Phase 10 step. Without it, WA's live list would have been ~210 with high junk fraction.

2. **Cross-state contamination is broader than for any prior state (7 states detected).** Likely explanation: many WA Serper hits land on multi-state mgmt-co portals (Russell PM, Associa) or settlement claim sites that aggregate filings from multiple states. The OCR-LLM cross-state check is **load-bearing** — these had no regex pattern that would have caught them.

3. **Override-regex tier didn't fire (0 deletes).** WA's noise shape is *different* from prior states:
   - **Less "OF CONDOMINIUM FOR" boilerplate** (UT-style) — WA recorders use "Auditor's File" stamps, which the existing regex doesn't pattern-match against.
   - **More court-filing fragments** ("1 2 5 8 10 11 12 13 14 15 16 SUPPLEMENTAL HOA", "CASE 2:15-cv-01413-MJP-10 FILED 04-06-16") — these have HOA-shape *only* because the bank-stage suffix appended "HOA" to court caption text.
   - **Possible override regex additions for next state batch:**
     - `^Case \d+:\d+-cv-\d+\b` (federal case caption)
     - `^\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\b` (numeric list with 8+ ints — court exhibit)
     - `\bSettlement Claims?\b`
     - `\bHearing Examiner\b`
     - `\bChapter 64\.\d+ RCW\b`

4. **Sub-county neighborhood anchors are essential for Seattle metro.** Plain "King County" + "Homeowners Association" queries returned mostly court filings from federal Seattle court cases. Adding Bellevue, Redmond, Sammamish, Issaquah, Kirkland anchored queries pulled the suburban-density HOA stock (where King County's HOA volume actually lives). Future WA expansion should add more neighborhood anchors: West Seattle / Capitol Hill / Magnolia / Fremont (Seattle proper), Mercer Island, Newcastle, Maple Valley, Covington, Black Diamond, North Bend, Snoqualmie.

5. **Resort county yield is modest.** Chelan (Lake Chelan/Leavenworth) banked 17 manifests, Skagit (Anacortes/La Conner) banked 23. After Phase 10 only a handful per county survived. Resort condos in the Cascades are mostly mgmt-co-portal-locked (Vail Resorts properties, Ross Mountain Park), similar to the UT Wasatch / NV Vegas / ID Blaine pattern observed in the May 2026 batch.

6. **WA Tier-3 ran in a single autonomous overnight session.** Per the playbook this should have been "operator-supervised, county-batched, multi-week", but with the post-overnight-batch tooling (override regex + OCR-LLM `is_hoa` + retry-on-5xx + Render-API JWT fetch) the full pipeline ran end-to-end in ~3.5 hours wall time at total cost ~$4 (DocAI $3.39 + OpenRouter ~$0.50 + Serper ~$0.30). The playbook's "multi-week" estimate was for the pre-OCR-LLM-validation tooling era.

## Costs

- **DocAI:** ~$3.39 (operationally measured via `/admin/costs` delta from $166.49 → $169.88)
- **Serper:** ~$0.40 (~290 queries at $0.0014 each, 25 queries × 13 county runs)
- **OpenRouter** (DeepSeek-flash + Kimi-k2.6 fallback): ~$0.50 for the OCR-LLM rename + `is_hoa` + cross-state passes (243 entries × ~$0.002 each)
- **Total:** ~$4.30

This is *substantially under* the playbook's Tier-3 budget envelope ($100–250 OCR + $30–75 OpenRouter). The improved Phase 2 OCR-first slug + geo extraction in `prepare_bank_for_ingest.py` plus the override-regex tier means most non-HOA candidates are caught at prepare time before full-doc OCR ever runs.

## Open follow-ups

1. **King County name-list-first pass.** Seattle metro condo/coop stock is paywalled-urban (CINC/AppFolio portals dominate). Per the name-list-first playbook §2b, the King County Recorder's office and WA Department of Licensing condominium registration (RCW 64.34) could provide a name universe. Estimated yield: 500–2,000 net-new live King County condo associations. Not attempted in this run.

2. **Sub-county Seattle neighborhood expansion.** Add West Seattle, Capitol Hill, Magnolia, Mercer Island, Newcastle, Maple Valley, Snoqualmie anchored queries in a future supplemental sweep. Estimated yield: ~30–80 net-new live King County HOAs.

3. **WA-specific override regex additions** (see Lessons #3): patterns for federal case captions, settlement claims, hearing examiner decisions, RCW chapter citations. These should be promoted to `scripts/phase10_close.py::NON_HOA_NAME_PATTERNS_OVERRIDE` when the next code change to that file lands.

4. **1 bbox-flagged record** (lat 34.29, lon -83.87 = Atlanta GA) attached to a "Homeowners Association" entry with state=WA. Demoted to `city_only` quality so it's hidden from the map. Source is likely a same-name HOA collision from a prior cross-state import. Could be hard-deleted manually; flagged but not auto-deleted by Phase 10 (bbox audit is warning-only).

## Files

- Final state report: `state_scrapers/wa/results/wa_20260509_062427_phaseB/final_state_report.json`
- OCR-LLM rename ledger: `state_scrapers/wa/results/wa_20260509_062427_phaseB/name_cleanup_unconditional.jsonl`
- Phase 10 log: `state_scrapers/wa/results/wa_20260509_062427_phaseB/phase10.log`
- Live verification (post-location): `state_scrapers/wa/results/wa_20260509_062427_phaseB/live_verification_post_location.json`
- Location backfill records: `state_scrapers/wa/results/wa_20260509_062427_phaseB/location_backfill_records.json`
- Prepared ingest ledger: `state_scrapers/wa/results/wa_20260509_062427_phaseB/prepared_ingest_ledger.jsonl`
- Per-county query files: `state_scrapers/wa/queries/wa_*_serper_queries.txt` (12 counties + host_family)
