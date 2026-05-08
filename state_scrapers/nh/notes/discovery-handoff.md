# NH Discovery Handoff

Updated: 2026-05-08

Run-id: `nh_20260507_225937_claude` (production sweep) + `nh_20260507_smoke_claude` (Hillsborough smoke).
Status: **done** for the keyword-Serper pass. See `notes/retrospective.md` for full Phase-10 writeup.

## Current State

- Bank prefix: `gs://hoaproxy-bank/v1/NH/`
- Bank counts: **385 manifests** banked across 10 counties + `_unresolved-name/` slot.
- Prepared bundles: **126** in `gs://hoaproxy-ingest-ready/v1/NH/`
- Live: **110 HOAs** / **146 documents** / **8,305 chunks** at hoaproxy.org
- Map points: **57 zip_centroid** (52% map rate)
- OpenRouter spend: ~$0.40 (deepseek-v4-flash for classification + name repair)
- Serper spend: ~$1.00 (~195 queries)
- DocAI spend cap: ≤$8.05 (5,366 prepared pages × $0.0015; PyPDF handled the text-extractable share for $0)
- Total marginal: ~$9.50 (within $10 NH budget)

## Source Families Attempted

| Source family | Queries run | Net manifests | Net PDFs | Yield | Status |
|---|---|---|---|---|---|
| NH SoS QuickStart | 0 | 0 | 0 | blocked | **abandoned** — Akamai/Imperva JS challenge |
| Keyword-Serper-per-county (Hillsborough) | 28 | 16 | ~24 | low-medium | exhausted (smoke + production) |
| Keyword-Serper-per-county (Rockingham, Belknap, Carroll, Merrimack, Strafford, Grafton, Cheshire, Sullivan, Coos) | ~180 | ~370 | ~470 | low-medium | exhausted, two-sweep stop reached |
| CAI New England directory | 0 | 0 | 0 | zero (per RI) | not attempted |
| Mgmt-company crawl | 0 | 0 | 0 | zero (per RI) | not attempted |
| Site-restricted Serper | 0 | 0 | 0 | zero (per RI) | not attempted |

## Productive Sources

- HOA-owned websites (Squarespace, hoa-express, custom WordPress) surfaced when Serper found the
  HOA's `/governing-documents` or `/documents` page. ~30% of all banked PDFs came from these.
- Town-specific HOA newsletters and meeting packets — surfaced as `low_value:minutes` rejections at
  prepare time but the underlying town/condo names led to a few real-HOA finds.
- SoS-hosted PDFs (`business.sos.nh.gov`-style) are unreachable due to the Akamai gate, but the
  classifier correctly routes them when they leak through Google indexing.

## Dry Sources

- **NH QuickStart business search**: hard-blocked by Akamai/Imperva. Skip until someone builds a
  Playwright harness.
- **Town clerk recorder portals** (Tyler/Cott/IQS/NewVision per municipality): paywalled per
  document. Same pattern as RI.
- **Mgmt-company portals** (TownSq, AppFolio, CINC, ManageBuilding, FrontSteps): walled per-resident
  login. Confirmed in RI run; no reason to expect different in NH.

## Stop Reasons by Branch

- **NH SoS QuickStart** — blocked auth (HTTP 403 + JS challenge). Stopped immediately in Phase 1
  preflight.
- **Per-county Serper sweeps** — completed full 20-query × 10-results cycle on each of the 10
  counties. Two-sweep stop rule effectively triggered for Coos and Sullivan (lowest density).
- **Active discovery for NH** — stopped after one full pass. Re-runs with the same query files would
  produce duplicates almost exclusively.

## Next Branches (if budget allows)

Allowed follow-ups (no new Serper / OpenRouter spend):

1. **Name-quality re-clean**: re-run `clean_dirty_hoa_names.py --state NH --apply` on the live DB
   after the LLM rename catches any stragglers. Expect ~14-16% per the GA / RI pattern.
2. **Demote thin/no-address HOAs**: ~50 of 110 live HOAs are doc-fragment names with no usable
   address. Apply `metadata_type=stub` or hide via `location_quality=city_only` (already done for
   the unmappable 53).
3. **Re-mine existing Serper result directories** (`benchmark/results/nh_serper_docpages_*`) with
   updated host-family knowledge. RI proved this recovers real docs at zero marginal cost.

Not allowed without a new budget gate (would require operator green-light):

4. **Owned-domain whitelisted preflight** for the ~20 NH HOA websites that surfaced but didn't fully
   crawl on the first pass.
5. **Playwright-based NH SoS scraper.** Would unlock the registry's ~2,500 leads but requires
   maintenance and state-specific browser-flow code. Out of scope for this autonomous run.

## Useful Commands

```bash
# Count bank manifests and PDFs
gsutil ls 'gs://hoaproxy-bank/v1/NH/**/manifest.json' 2>/dev/null | wc -l
gsutil ls 'gs://hoaproxy-bank/v1/NH/*/*/doc-*/original.pdf' 2>/dev/null | wc -l

# Re-run prepare phase with the same ledger
.venv/bin/python state_scrapers/nh/scripts/run_state_ingestion.py \
  --run-id nh_20260507_225937_claude --apply --skip-discovery --skip-import \
  --skip-locations --max-docai-cost-usd 10

# Re-run import with fresh JWT
.venv/bin/python state_scrapers/nh/scripts/run_state_ingestion.py \
  --run-id nh_20260507_225937_claude --apply --skip-discovery --skip-prepare

# Re-run ZIP centroid backfill (the path that actually populated the map)
.venv/bin/python /tmp/nh_zip_backfill.py
```

## Autonomy Reminder

NH is **done** for the autonomous keyword-Serper pass. Re-engagement only when one of:
- A budget gate is reopened (e.g. operator funds a Playwright harness).
- New per-NH source families are identified (e.g. an aggregator like CASNC for NC was identified).
- Tier-elevation: NH is treated as Tier 2 with operator-supervised county batching.
