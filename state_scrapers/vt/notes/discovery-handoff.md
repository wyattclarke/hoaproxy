# Vermont Discovery Handoff

Updated: 2026-05-07

Instruction: continue autonomously for VT. Do not stop at checkpoints. Commit
or hand off as needed, then immediately keep scraping. Only send a final
response if blocked, out of budget, or asked for status.

## Current State

- Bank prefix: `gs://hoaproxy-bank/v1/VT/`
- Initial bank count: `0` manifests, `0` PDFs.
- Run ID: `vt_20260507_224836_codex`
- Tier/budget: Tier 0; `--max-docai-cost-usd 5`.
- Discovery branch: SoS-first preflight failed headlessly; fallback is
  county-scoped deterministic Serper over Vermont's 14 counties.
- Secrets policy: keys are loaded from `settings.env`; do not echo them.

## SoS Preflight

Vermont's public registry is an Angular app at
`https://bizfilings.vermont.gov/business/businesssearch`, backed by
`https://api.bizfilings.vermont.gov/api`. The frontend exposes
`POST /api/businesssearch/searchbusiness`, but the UI requires CAPTCHA and
direct API POSTs from this environment return Imperva 403 pages. Treat the SoS
branch as blocked for this run unless a later browser/manual path becomes
available without bypassing CAPTCHA.

## Source Families Attempted

| Source family | Queries run | Net manifests | Net PDFs | Yield | Status |
|---|---:|---:|---:|---|---|
| Vermont SoS API | 1 API payload probe | 0 | 0 | zero | blocked by CAPTCHA/Imperva |
| County Serper fallback | pending | pending | pending | pending | active |

## Productive Sources

None yet. Prioritize Chittenden, Rutland, Washington, Bennington, and Lamoille
first because density is likely highest around Burlington, resort condos, and
Montpelier/Stowe.

## Dry Sources

- Direct Vermont SoS API calls are blocked headlessly.

## Stop Reasons by Branch

- SoS-first: stopped at preflight because public search requires CAPTCHA and
  direct API calls return Imperva 403.

## Next Branches

1. Run county Serper fallback with conservative per-county lead caps and
   `--require-state-hint`.
2. Probe only public governing-document candidates; skip portals, resident
   pages, newsletters, minutes, forms, and real-estate packets.
3. After banking, run `prepare_bank_for_ingest.py` with `--max-docai-cost-usd 5`.
4. Write `final_state_report.json` and the mandatory retrospective before exit.

## Useful Commands

```bash
gsutil ls 'gs://hoaproxy-bank/v1/VT/**/manifest.json' 2>/dev/null | wc -l
gsutil ls 'gs://hoaproxy-bank/v1/VT/*/*/doc-*/original.pdf' 2>/dev/null | wc -l

set -a; source settings.env; set +a
curl -s https://openrouter.ai/api/v1/credits \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" | python3 -m json.tool
```

## Autonomy Reminder

The turn boundary is not a blocker. If no real blocker exists, keep launching
the next concrete scrape/probe/validation step. Do not send a final answer just
to summarize progress.
