# WA HOA registry seed — blocker notes

**Status:** blocked. No `wa_registry_seed.jsonl` produced.

## What was tried (2026-05-09)

1. **WA SoS Corporations Data Extract** (`data.wa.gov/.../prdz-hjxr`):
   formerly a free nightly XML/text dump of the entire corporations database.
   **Retired September 30, 2024.** The successor dataset id `f9jk-mm39` is a
   metadata stub; programmatic CSV pulls return
   `no row or column access to non-tabular tables` (HTTP 403).

2. **Corporations & Charities Filing System** (`ccfs.sos.wa.gov`):
   Angular SPA fronted by Cloudflare Turnstile (JS challenge). Manual
   advanced search supports CSV export of result sets, but it is not
   programmatically accessible without a headless browser harness.

3. **King County Assessor `EXTR_CondoComplex` / `EXTR_AptComplex`**:
   the assessor publishes per-complex CSVs, but the public-facing
   `info.kingcounty.gov/assessor/datadownload/default.aspx` requires
   accepting a RCW 42.56.070(9) commercial-use disclaimer (form POST). The
   underlying `aqua.kingcounty.gov/extranet/assessor/` directory listing is
   403-Forbidden. The github.com/lukeschlather/load-king-county-assessor-data
   repo confirms users have to manually click through to download.

## What is already covered

Nothing in `state_scrapers/wa/leads/` yet — directory is empty.
`state_scrapers/wa/scripts/run_state_ingestion.py` exists from the per-state
playbook but has not been run.

## Next steps if this gets revisited

- Drive `info.kingcounty.gov/assessor/datadownload/default.aspx` once with a
  headless browser to capture `EXTR_CondoComplex.zip` + `EXTR_AptComplex.zip`,
  which together cover ~5–6k associations in King County alone.
- Pull Pierce, Snohomish, Spokane, Clark county assessor extracts (they
  publish similar CSVs without disclaimer interstitials in some cases).
- Hire the WA SoS for a paid bulk extract (the retired feed was free but
  may now be a paid PRA request).
- Run a Serper sweep on `seattlecondoreview.com`, `wahoa.org`, and the WA SoS
  charities database (separate from corps) for the subset of HOAs that
  registered as 501(c)(4) charities.
