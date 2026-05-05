# Florida HOA Discovery Handoff

State: **FL**
Started: 2026-05-05

## Why Florida is special

FL is the first state where we depart from pure Serper-driven county sweeps. Two FL-specific facts shape the approach:

1. **Sunbiz bulk corporation download.** The Florida Division of Corporations publishes the entire corporation registry as quarterly bulk files via public SFTP (`sftp.floridados.gov`, user `Public` / pwd `PubAccess1845!`). Files: `doc/quarterly/cor/cordata.zip` split into 10 shards (records ending in 0..9). Fixed-width 1440-char records; field map at https://dos.sunbiz.org/data-definitions/cor.html. We filter to `Status=A` nonprofits whose name matches HOA/condo/POA/master patterns and use that as a seed list to drive targeted Serper queries.

2. **HB 1203 (effective Jan 1, 2025).** Every FL HOA with 100+ parcels must publish governing docs to a website (or app). The mandated portal is private/password-gated, so HB 1203 doesn't directly create public URLs — but the rollout pushed thousands of HOAs onto common platforms (HOA-Express, HOA Sites, eNeighbors, FrontSteps, CINC Systems, gogladly, fsresidential, hmsft-doc) that often leak public pages. Skip walled-portal hosts (CINC, FrontSteps, AppFolio).

DBPR is **not** a useful registry — Chapter 720 explicitly leaves HOAs out of DBPR's regulatory scope (DBPR only handles condos/coops/timeshares/mobile homes).

## Scope reminder

"HOA" includes condos in this project — see CLAUDE.md and the playbook. FL has both Chapter 720 HOAs and Chapter 718 condominium associations; both are in scope and bank under the same `gs://hoaproxy-bank/v1/FL/{county}/{slug}/` layout.

## Bank coverage

| Snapshot | Manifests | Notes |
|---|---|---|
| 2026-05-05 start | 22 | All under GA county names (`bryan, carroll, chatham, dekalb, fulton, glynn, hall, henry, houston, paulding, walton`) — mis-routed cross-state from the GA pass. Cleanup pending. |

## Drivers

- **A — Sunbiz-name-driven Serper.** For each top-30 FL county, take that county's Sunbiz HOAs and run targeted `"<Name>" Florida documents bylaws filetype:pdf` queries.
- **B — Standard per-county sweeps.** Mirror of `run_ga_county_sweep_v2.sh` with FL counties + cities, architectural-anchored-on-Declaration queries.
- **C — Registered-agent host expansion.** Identify top 30 FL property management companies via Sunbiz registered-agent column, run `site:<domain>` Serper sweeps.

## Productive source families (carried forward from GA)

`img1.wsimg.com`, `static1.squarespace.com`, `nebula.wsimg.com`, `rackcdn.com`, `s3.amazonaws.com`, `fsresidential.com`, `eneighbors.com/p/`, `gogladly.com/connect/document/`, `inurl:hmsft-doc`, `inurl:/wp-content/uploads/`. Skip: `cincsystems.net`, `frontsteps.com` (walled).

## FL-specific blocked hosts (added to `_STATE_BLOCKED_HOSTS`)

`leg.state.fl.us`, `flsenate.gov`, `dos.fl.gov`, `myfloridalicense.com`, `flrules.org`, `flcourts.org`, `flgov.com`.

## OpenRouter spend

Starting budget: ~$8 of $20 cap remaining as of GA pass. User cleared additional Serper budget for FL.

## Log

- 2026-05-05: Investigated FL state-level data sources. Found Sunbiz bulk SFTP. Confirmed DBPR has no HOA registry. Decided on Drivers A/B/C plan. Updated CLAUDE.md and playbook to clarify "HOA" is condo-inclusive.
