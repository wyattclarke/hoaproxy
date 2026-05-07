# Vermont State Scrape

Tier 0 run for Vermont (`VT`) under the canonical multi-state ingestion
playbook.

## Discovery Plan

Primary recommendation was SoS-first, but the current Vermont registry requires
CAPTCHA in the public UI and direct calls to
`https://api.bizfilings.vermont.gov/api/businesssearch/searchbusiness` return
Imperva 403s from this environment. This run therefore uses the playbook
fallback: county-scoped deterministic Serper queries over all 14 counties.

## Runner

```bash
.venv/bin/python state_scrapers/vt/scripts/run_state_ingestion.py \
  --run-id vt_YYYYMMDD_HHMMSS_codex \
  --discovery-mode keyword-serper \
  --max-docai-cost-usd 5 \
  --apply
```

The runner writes artifacts under `state_scrapers/vt/results/{run_id}/` and
uses the existing GCS bank bucket:

```text
gs://hoaproxy-bank/v1/VT/
```
