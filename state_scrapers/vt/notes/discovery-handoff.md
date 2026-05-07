# Vermont Discovery Handoff

Updated: 2026-05-07

Instruction: continue autonomously for VT. Do not stop at checkpoints. Commit
or hand off as needed, then immediately keep scraping. Only send a final
response if blocked, out of budget, or asked for status.

## Current State

- Bank prefix: `gs://hoaproxy-bank/v1/VT/`
- Current clean bank count: `17` manifests, `21` PDFs.
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
| County Serper fallback | 88 Serper calls | 17 | 21 | low but usable | stopped under low-yield rule |
| SNHA source-family sweep | 4 Serper calls | 0 | 1 enrichment | low | exhausted |

## Productive Sources

- Direct-only curated leads in `leads/vt_curated_repair_leads.jsonl`,
  `leads/vt_curated_direct_reprobe_leads.jsonl`,
  `leads/vt_curated_second_wave_leads.jsonl`, and
  `leads/vt_curated_snha_enrichment_leads.jsonl`.
- Productive communities came from public HOA/condo sites, resort-community
  document hosts, and municipal development-review attachments where the PDF
  text clearly named the association.
- Strongest final sources: Smugglers' Notch/SNHA condominium documents,
  Eastridge Acres/Sunrise PDFs, Chimney Hill/Haystack Highlands CDN PDFs,
  Quechee Lakes auction packet PDFs, and direct municipal attachments for
  Hillview Heights / Old Morgan Orchard.

## Dry Sources

- Direct Vermont SoS API calls are blocked headlessly.
- Broad county searches were very noisy: municipal plans, zoning bylaws,
  regional plans, hazard mitigation plans, permit packets, real-estate due
  diligence packets, court filings, legislative PDFs, and out-of-state county
  overlap dominated the raw results.
- Automatic probing was too permissive on the first Chittenden/Rutland pass and
  banked municipal packets. Those prefixes were deleted from GCS and replaced
  by direct-only curated leads.

## Stop Reasons by Branch

- SoS-first: stopped at preflight because public search requires CAPTCHA and
  direct API calls return Imperva 403.
- County Serper fallback: stopped after all 14 counties plus one SNHA
  source-family sweep. Net was 17 clean manifests / 21 PDFs, and the remaining
  collect-only candidates were >80% government, real-estate, court, or generic
  planning documents.

## Next Branches

1. Run `prepare_bank_for_ingest.py` through the VT runner with
   `--max-docai-cost-usd 5`.
2. Import prepared VT bundles into live through `/admin/ingest-ready-gcs`.
3. Verify live counts/map coverage and write the mandatory retrospective.

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
