# Maine State Scrape

Maine is a Tier 1 small-state run under `docs/multi-state-ingestion-playbook.md`.
Appendix D recommends SoS-first, but the Maine ICRS corporate search currently
uses reCAPTCHA and displays an explicit automated-use prohibition, so autonomous
discovery falls back to county-scoped deterministic Serper.

## Runner

```bash
.venv/bin/python state_scrapers/me/scripts/run_state_ingestion.py \
  --run-id me_YYYYMMDD_HHMMSS_codex \
  --discovery-mode keyword-serper \
  --max-docai-cost-usd 10 \
  --apply
```

Use `--counties-only Cumberland,York,Hancock,Kennebec,Penobscot` for the first
density-prioritized sweep. The runner writes run artifacts under
`state_scrapers/me/results/{run_id}/`.

## Notes

- Bank prefix: `gs://hoaproxy-bank/v1/ME/`
- Prepared prefix: `gs://hoaproxy-ingest-ready/v1/ME/`
- Primary fallback source families: county registry wording, Portland parcel
  folders, CivicPlus town agenda attachments, SearchIQS/Acclaim/GovOS public
  recorder indexes where documents are public.
- Avoid SoS automation unless a curated public export or non-automated lead file
  is supplied.
