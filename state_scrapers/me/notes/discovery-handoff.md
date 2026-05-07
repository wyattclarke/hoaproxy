# Maine Discovery Handoff

Updated: 2026-05-07

Instruction: continue autonomously for ME. Do not stop at checkpoints. Commit
or hand off as needed, then immediately keep scraping. Only send a final
response if blocked, out of budget, or asked for status.

## Current State

- Bank prefix: `gs://hoaproxy-bank/v1/ME/`
- Initial bank count: 0 manifests, 0 PDFs.
- Run id: `me_20260507_225103_codex`.
- Tier/budget: Tier 1; `--max-docai-cost-usd 10`; OpenRouter cap $5; Serper cap $3.
- Active work: SoS preflight blocked by reCAPTCHA and explicit automated-use
  prohibition; running county-scoped deterministic Serper fallback.
- Parallel runs active: VT, NH, WY. Keep request delays conservative and avoid
  shared-file edits outside `state_scrapers/me/`.

## Source Families Attempted

| Source family | Queries run | Net manifests | Net PDFs | Yield | Status |
|---|---:|---:|---:|---|---|
| Maine ICRS SoS corporate search | 0 | 0 | 0 | blocked | blocked by reCAPTCHA/no-automation notice |
| County-scoped Serper, high-density counties | pending | pending | pending | pending | active |

## Productive Sources

Pending live sweeps. Candidate high-signal patterns from preflight:

- Portland parcel folders: `site:parcelsfolder.portlandmaine.gov` with
  `Declaration of Condominium`, `Bylaws`, `Maine Condominium Act`, `Unit Owners`.
- CivicPlus town attachments: `AgendaCenter/ViewFile` with condo declarations or
  unit-owner associations.
- Registry statutory phrasing: `"Maine Condominium Act"`, `"Maine Unit Ownership
  Act"`, `"33 M.R.S.A. §§1601-101"`, `"Declaration of Condominium"`.
- Non-condo phrasing: `"Declaration of Covenants and Restrictions"` plus
  `"Property Owners Association"` or `"Owners Association"`.

## Dry Sources

- Maine SoS ICRS: public page is accessible, but the search form uses reCAPTCHA
  and states that data mining and automated tools are prohibited. Do not scrape
  it in this autonomous run.

## False-Positive Patterns To Block

- Real-estate sale packets, MLS disclosures, offering statements without full
  governing docs.
- Planning-board packets that only reference condo docs.
- DEP/environmental covenants, wetland mitigation, stormwater-only agreements.
- Court, foreclosure, bankruptcy, lien, mortgage discharge, tax-lien packets.
- Recorder fee/fraud-alert pages and generic registry pages.
- Building permits, fire/code files, certificates of occupancy.
- Generic legal explainers and directory hosts.
- Out-of-state county collisions, especially Cumberland County NC/PA and York
  County PA/SC.
- `POA` as power of attorney.
- Standalone minutes, budgets, newsletters, rosters, ballots, forms, and private
  portal URLs.

## Next Branches

1. Run the first sweep on Cumberland, York, Hancock, Kennebec, and Penobscot.
2. Inspect benchmark result audits for host families that produce actual PDFs.
3. Promote any productive host family to deterministic direct-PDF mining.
4. Continue with Androscoggin, Knox, Lincoln, Sagadahoc, and Waldo only if the
   first sweep has useful yield; leave sparse northern counties for the tail.

## Useful Commands

```bash
# Count bank manifests and PDFs
gsutil ls 'gs://hoaproxy-bank/v1/ME/**/manifest.json' 2>/dev/null | wc -l
gsutil ls 'gs://hoaproxy-bank/v1/ME/*/*/doc-*/original.pdf' 2>/dev/null | wc -l

# Dry preflight
.venv/bin/python state_scrapers/me/scripts/run_state_ingestion.py \
  --run-id me_YYYYMMDD_HHMMSS_codex \
  --discovery-mode keyword-serper \
  --skip-discovery --skip-prepare --skip-import

# First live discovery sweep
HOA_DISCOVERY_RESPECT_ROBOTS=1 HOA_DISCOVERY_REQUEST_DELAY_SECONDS=1.5 \
.venv/bin/python state_scrapers/me/scripts/run_state_ingestion.py \
  --run-id me_YYYYMMDD_HHMMSS_codex \
  --discovery-mode keyword-serper \
  --counties-only Cumberland,York,Hancock,Kennebec,Penobscot \
  --max-queries-per-county 8 \
  --results-per-query 10 \
  --max-leads-per-county 30 \
  --skip-prepare --skip-import \
  --max-docai-cost-usd 10 \
  --apply
```

## Autonomy Reminder

The turn boundary is not a blocker. If no real blocker exists, keep launching
the next concrete scrape/probe/validation step. Do not send a final answer just
to summarize progress.
