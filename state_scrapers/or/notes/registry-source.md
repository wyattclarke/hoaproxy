# Oregon registry source

## Pull
```bash
curl -L -o data/or_active_nonprofit_corporations.csv \
    "https://data.oregon.gov/api/views/8kyv-b2kw/rows.csv?accessType=DOWNLOAD"
python3 state_scrapers/or/scripts/pull_or_registry.py
```

## Source
- **Dataset**: Active Nonprofit Corporations (Oregon SoS Corporation Division)
- **Portal**: data.oregon.gov, dataset id `8kyv-b2kw`
- **Auth**: none, no captcha
- **Refresh**: monthly, first working day
- **Format**: CSV; ~180k rows (multiple per entity, one per associated-name role)
- **Distinct entities**: ~9,500 active mutual-benefit nonprofits

## Filter
- Entity Type contains "NONPROFIT"
- Nonprofit Type contains "MUTUAL BENEFIT" (drops religious, public-benefit)
- Business name matches HOA-shaped patterns (`HOA_NAME_RE`)
- Reject patterns drop golf clubs, water districts, fraternal orgs, etc. (`REJECT_RE`)
- Address row priority: MAILING ADDRESS > PRINCIPAL PLACE OF BUSINESS > REGISTERED AGENT > PRESIDENT
- Deduplicate by canonical (uppercased, whitespace-normalized) name

## Output
- 4,299 leads (`or_registry_seed.jsonl`): 2,220 HOA, 2,079 condo, 0 coop
- Universe target was ~4,150 — close match.
