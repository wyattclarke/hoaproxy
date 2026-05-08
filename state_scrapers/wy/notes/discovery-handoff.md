# Wyoming Discovery Handoff

Updated: 2026-05-07

Instruction: continue autonomously for WY. Do not stop at checkpoints. Commit
or hand off as needed, then immediately keep scraping. Only send a final
response if blocked, out of budget, or asked for status.

## Current State (RUN COMPLETE)

- Bank prefix: `gs://hoaproxy-bank/v1/WY/`
- Final bank count: 314 manifests across 9 swept counties + 44 in `_unresolved-name/`.
  Plus 2 stale legacy entries under `WY/broward/` and `WY/pike/` (cross-state
  contamination from a prior run; left untouched).
- Live profiles: 133, documents: 137, chunks: 7,019.
- Map coverage: 48 / 133 = 36% (zip_centroid only); 0 out-of-state.
- Spend: DocAI $2.20, OpenRouter ~$0.45, Serper ~$0.05; all under cap.
- Run ID: `wy_20260507_225444_claude`
- Tier/budget: Tier 0; `--max-docai-cost-usd 5`; OpenRouter cap $5; Serper cap $3.
- Discovery branch: SoS-first preflight failed (wyobiz.wyo.gov is F5/Akamai
  TSPD bot-protected — same blocker as Vermont). Pivoted to county-scoped
  keyword Serper anchored on the 9 HOA-bearing counties.
- Secrets policy: keys are loaded from `settings.env`; do not echo them.

## SoS Preflight

Wyoming SoS Filing Search at `https://wyobiz.wyo.gov/Business/FilingSearch.aspx`
returns an F5 BIG-IP / TSPD CAPTCHA challenge page (not the search form) for
all unauthenticated requests, including direct API POSTs from headless contexts.
The state's bulk-download / SODA-equivalent does not exist for the business
registry (verified against `https://sos.wyo.gov/Business/Default.aspx` and
state open-data portal). Treat the SoS branch as blocked for this run.

## County Scope

Wyoming has 23 counties but the HOA universe is concentrated in ~9. Rural
ranchland counties are skipped to keep precision/cost ratio reasonable.

| County | City anchors | HOA shape |
|---|---|---|
| Teton | Jackson, Wilson, Teton Village, Moose, Kelly, Alta | Resort condos (likely 50-70% of statewide) |
| Laramie | Cheyenne, Pine Bluffs, Burns | Front-range planned communities |
| Natrona | Casper, Mills, Bar Nunn, Evansville | Front-range planned communities |
| Park | Cody, Powell, Meeteetse | Cody/Yellowstone gateway condos + ranches |
| Sublette | Pinedale, Big Piney, Marbleton, Boulder, Daniel | Resort condos + ranches |
| Sheridan | Sheridan, Story, Big Horn, Ranchester, Dayton | Foothill ranches + planned communities |
| Albany | Laramie (city), Centennial, Tie Siding | University-adjacent townhomes |
| Lincoln | Star Valley Ranch, Alpine, Afton, Thayne, Etna | Jackson-adjacent resort condos |
| Fremont | Lander, Riverton, Dubois | Wind River resort + ranches |

Skipped (effectively zero HOAs): Niobrara, Crook, Weston, Converse, Carbon,
Goshen, Hot Springs, Johnson, Platte, Sweetwater, Uinta, Washakie, Big Horn,
Campbell. (Sweetwater/Campbell may surface in a follow-up sweep if the primary
nine prove thin.)

## Source Families Attempted

| Source family | Queries run | Net manifests | Net PDFs | Yield | Status |
|---|---:|---:|---:|---|---|
| Wyoming SoS (wyobiz) | 1 probe | 0 | 0 | zero | blocked by F5 TSPD CAPTCHA |
| County Serper (9 counties) | pending | pending | pending | pending | active |

## Productive Sources

None yet.

## Dry Sources

- `wyobiz.wyo.gov` — bot-protected, no API export.

## Stop Reasons by Branch

- SoS-first: stopped at preflight; F5 BIG-IP / TSPD CAPTCHA blocks headless access.

## Next Branches

1. Run the 9-county Serper sweep via `state_scrapers/wy/scripts/run_state_ingestion.py`.
2. After banking, run `prepare_bank_for_ingest.py` with `--max-docai-cost-usd 5`.
3. Import via `/admin/ingest-ready-gcs?state=WY` in 50-cap loops.
4. Apply ZIP-centroid location enrichment.
5. If primary 9 counties are thin, sweep Sweetwater (Rock Springs / Green
   River) and Campbell (Gillette) before stopping.
6. Write `final_state_report.json` and the mandatory retrospective before exit.

## Useful Commands

```bash
gsutil ls 'gs://hoaproxy-bank/v1/WY/**/manifest.json' 2>/dev/null | wc -l
gsutil ls 'gs://hoaproxy-bank/v1/WY/*/*/doc-*/original.pdf' 2>/dev/null | wc -l

set -a; source settings.env; set +a
curl -s https://openrouter.ai/api/v1/credits \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" | python3 -m json.tool
```

## Autonomy Reminder

The turn boundary is not a blocker. If no real blocker exists, keep launching
the next concrete scrape/probe/validation step. Do not send a final answer
just to summarize progress.
