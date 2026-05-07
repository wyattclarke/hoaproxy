# Maine Discovery Handoff

Updated: 2026-05-07

Instruction: ME is complete for this pass. Future ME work should start from the
source-family notes below instead of rerunning broad generic sweeps.

## Current State

- Bank prefix: `gs://hoaproxy-bank/v1/ME/`
- Initial bank count: 0 manifests, 0 PDFs.
- Final bank count: 18 manifests, 23 PDFs.
- Run id: `me_20260507_225103_codex`.
- Tier/budget: Tier 1; `--max-docai-cost-usd 10`; OpenRouter cap $5; Serper cap $3.
- Final live import: 17 communities, 22 documents, 950 chunks.
- Map coverage: 17/17 via ZIP centroid backfill; no out-of-state points.
- Estimated DocAI content spend: $0.1665 for 111 pages.
- Final report: `state_scrapers/me/results/me_20260507_225103_codex/final_state_report.json`.

## Source Families Attempted

| Source family | Queries run | Net manifests | Net PDFs | Yield | Status |
|---|---:|---:|---:|---|---|
| Maine ICRS SoS corporate search | 0 | 0 | 0 | blocked | blocked by reCAPTCHA/no-automation notice |
| Generic county-scoped Serper, high-density counties | ~32 | noisy | noisy | poor | stopped and cleaned; too many legal/government/doc-page false positives |
| Curated direct-PDF municipal/registry docs | 0 search calls | 15 | 17 | high | imported after probe/prepare |
| Penobscot county direct-only no-bank sweep | 8 | 0 | 0 | dry | mostly court/statute/manual noise |
| Source-family no-bank sweep | 12 | 3 new + 1 existing | 6 | high | produced Wells/York docs and Beachwood supplementals |

## Productive Sources

- Portland parcel folders (`parcelsfolder.portlandmaine.gov`) were the best
  direct source for Cumberland County condominium declarations, bylaws, rules,
  plats, and amendments.
- York/Wells registry-style municipal PDFs produced the best tail yield:
  Beachwood Bay Estates, Breeze Lane, Fairway View Village, Ocean Mist Villages,
  Misty Harbor/Barefoot Beach, and Regency Woods.
- Highly specific statutory phrasing was better than HOA generic terms:
  `"Maine Condominium Act"`, `"Declaration of Condominium"`, `"Unit Owners"`,
  `"33 M.R.S.A."`, plus city/county hints.
- A curated direct-PDF lead file was much safer than letting the generic runner
  bank automatically from broad Serper results.

## Dry Sources

- Maine SoS ICRS: public page is reachable, but the search form uses reCAPTCHA
  and states that data mining and automated tools are prohibited. Do not scrape
  it in autonomous runs.
- Penobscot county direct-only searches returned no useful HOA governing docs in
  this pass.
- Broad county terms around `homeowners association`, `owners association`, and
  `covenants` produced mostly government packets, court material, and unrelated
  real-estate content.

## False-Positive Patterns To Block

- Real-estate sale packets, MLS disclosures, and offering statements without full
  governing docs. One Homestead Farms offering statement was banked but rejected
  by prepare as `unsupported_category:unknown`.
- Planning-board packets that only reference condo docs.
- DEP/environmental covenants, wetland mitigation, and stormwater-only files.
- Court, foreclosure, bankruptcy, lien, mortgage discharge, and tax-lien packets.
- Recorder fee/fraud-alert pages and generic registry pages.
- Building permits, fire/code files, certificates of occupancy, and zoning PDFs.
- Generic legal explainers, CAI factbooks, law outlines, manuals, and directory hosts.
- Out-of-state collisions including Turnberry/Aberdeen/Charleston and York or
  Cumberland county pages outside Maine.
- `POA` as power of attorney.
- Standalone minutes, budgets, newsletters, rosters, ballots, forms, and private
  portal URLs.

## Final Live Communities

- Beachwood Bay Estates Condominium Association
- Breeze Lane Condominium Association
- Cider Hill Condominium Association
- Cumberland Foreside Condominium Association
- Eastman Block Condominium Association
- Fairway View Village Condominium Association
- Misty Harbor and Barefoot Beach Resort Condominium Association
- Munjoy Heights Condominium Association
- Ocean Mist Villages Condominium Association
- OneJoy Condominium Owners Association
- Park-Danforth Condominium Association
- Pine and Winter Street Condominium Association
- Regency Woods Condominium Association
- Ridgewood Condominium Association
- Sunfield Condominium Association
- Woodbury Shores Cottages Condominium Owners Association
- Yarmouth Bluffs Condominium Association

## Recommended Next Branches

1. Do a second targeted pass over York County registry/municipal PDF naming
   patterns; this was the highest-yield branch late in the run.
2. Mine Portland parcel folders deterministically by known condominium names
   rather than generic county searches.
3. Try Cumberland and York city/town agenda attachment hosts with exact HOA names.
4. Only then spend effort on Hancock/Kennebec/Penobscot. The first Penobscot pass
   was dry.

## Useful Commands

```bash
# Count bank manifests and PDFs
gsutil ls 'gs://hoaproxy-bank/v1/ME/**/manifest.json' 2>/dev/null | wc -l
gsutil ls 'gs://hoaproxy-bank/v1/ME/*/*/doc-*/original.pdf' 2>/dev/null | wc -l

# Prepare/import existing ME bank state
.venv/bin/python state_scrapers/me/scripts/run_state_ingestion.py \
  --run-id me_YYYYMMDD_HHMMSS_codex \
  --discovery-mode keyword-serper \
  --skip-discovery \
  --max-docai-cost-usd 10 \
  --apply

# Reapply conservative ZIP centroid locations
.venv/bin/python state_scrapers/me/scripts/enrich_me_locations.py \
  --base https://hoaproxy.org \
  --zip-cache state_scrapers/me/results/me_YYYYMMDD_HHMMSS_codex/zip_centroid_cache.json \
  --output state_scrapers/me/results/me_YYYYMMDD_HHMMSS_codex/location_enrichment.jsonl \
  --state ME \
  --skip-nominatim \
  --apply
```

## Autonomy Reminder

The turn boundary is not a blocker. For follow-up ME runs, continue from the
targeted branches above and stop only when a real source family is exhausted, a
budget cap is reached, or the user asks for status.
