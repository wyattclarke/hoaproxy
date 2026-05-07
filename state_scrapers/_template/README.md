# State Scraping Runner Template

Copy this directory to `state_scrapers/{state}/` (lowercase two-letter code) and
replace every placeholder identifier before running.

## Placeholders

| Placeholder | Replace with | Example |
|---|---|---|
| `__STATE__` | Two-letter state code | `MT` |
| `__STATE_NAME__` | Full state name | `Montana` |
| `__STATE_BBOX__` | Bounding-box dict for OOB coordinate check | `{"min_lat": 44.35, "max_lat": 49.00, "min_lon": -116.05, "max_lon": -104.04}` |
| `__TIER__` | Playbook tier (0–4) | `1` |
| `__MAX_DOCAI_USD__` | Default OCR budget in USD — see Appendix D cost defaults | `10` |
| `__DISCOVERY_SOURCE__` | Short label matching Appendix D recommendation | `sos-first` |

Run a global find-and-replace in `scripts/run_state_ingestion.py` before your first commit.

## Quick checklist

1. Copy directory: `cp -r state_scrapers/_template state_scrapers/{state}`.
2. Replace all six placeholders in `scripts/run_state_ingestion.py`.
3. Choose `--discovery-mode` (see Appendix D for this state):
   - `keyword-serper` — populate `COUNTY_RUNS` with per-county query files in `queries/`.
   - `sos-first` — place SoS-extracted leads at `leads/{state}_sos_leads.jsonl`.
   - `manual-leads` — place curated leads at `leads/{state}_curated_leads.jsonl`.
4. Run Phase 1 preflight (10-minute spike): verify GCS, DocAI, Serper keys, and admin token.
5. Tag `--run-id` with both `{state}_{YYYYMMDD_HHMMSS}` and the agent name
   (`claude` or `codex`) for cross-batch attribution, e.g. `mt_20260601_120000_claude`.
6. Set `--max-docai-cost-usd` to the Appendix D Tier default before the first live run.
7. Run with `--apply` once dry-run validates cleanly.
8. Write the Phase 10 retrospective at `notes/retrospective.md` before the session exits.

## Directory layout after setup

```
state_scrapers/{state}/
  scripts/
    run_state_ingestion.py   # edited from this template
  queries/                   # per-county query .txt files (keyword-serper mode)
  leads/                     # pre-collected SoS or curated lead .jsonl files
  notes/
    discovery-handoff.md     # running handoff updated during the run
    retrospective.md         # mandatory Phase 10 retrospective
  results/
    {run_id}/                # per-run outputs (logs, ledger, final_state_report.json)
```

## Harness agnosticism

The runner template uses only:
- Python stdlib
- `requests` (HTTP calls to the live API and Render env-var endpoint)
- `google.cloud.storage` (GCS counts)
- `subprocess.run` against existing repo scripts

No Claude or Codex SDK imports. Both agents use the same runner unchanged.

## Reference

- `docs/multi-state-ingestion-playbook.md` — canonical playbook (all phases)
- `docs/multi-state-ingestion-playbook.md#appendix-d` — per-state discovery table
- `state_scrapers/in/scripts/run_state_ingestion.py` — original IN runner this was generalized from
- `state_scrapers/ri/scripts/` — SoS-first canonical scripts (probe_enriched_leads, enrich_ri_locations)
- `state_scrapers/ks/notes/discovery-handoff.md` — most detailed handoff exemplar
- `state_scrapers/ri/notes/retrospective.md` — Tier 1 SoS-first retrospective exemplar
