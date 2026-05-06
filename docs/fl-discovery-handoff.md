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
- 2026-05-05: Sunbiz Non-Profit quarterly download complete (43 MB → 10 × 30 MB fixed-width). Parser yielded 36,644 active FL HOAs/condos (`data/fl_sunbiz_hoas.jsonl`). 99% DOMNP filing type. Top registered agents: Sentry Mgmt 434, Specialty Mgmt 164, Vesta 126, Leland 106, Resort Mgmt 105, Associa Gulf Coast 86, Home Encounter 85.
- 2026-05-05: ZIP→county map built from Census 2020 ZCTA crosswalk + 14 synthetic prefix ranges → 1,987 FL ZIPs. 36,185 of 36,644 Sunbiz rows tagged (98.7%); the 459 nulls are out-of-state mailing/registered-agent addresses. Top counties by HOA count: miami-dade 4,708 / palm-beach 3,252 / broward 3,201 / hillsborough 2,169 / orange 2,091 / pinellas 2,074 / collier 2,001 / lee 1,865 / sarasota 1,230 / brevard 1,181.
- 2026-05-05: Mis-routed manifest cleanup complete. 11 of 22 recovered to correct FL counties (broward, st-johns, collier, palm-beach, alachua, clay, duval, seminole, walton-FL); 11 unrecoverable junk (garbled OCR names, regulatory boilerplate, attorney CVs) moved to `_unknown-county/`. `scripts/fl_repair_misrouted_manifests.py` is idempotent.
- 2026-05-05: Driver B launched in background (`run_all_fl_counties_v2.sh`). Started Miami-Dade with 34 cities, --max-queries 800, --max-leads 800. Expected ~17 min Serper + validate + clean + probe per county.
- 2026-05-05: Drivers A and C scaffolded but not yet run. A targets top 20 counties × Sunbiz HOA names (~30k queries, ~$30 Serper). C targets 9 verified management-company domains × 7 patterns = 63 queries, ~$1.50 Serper. Holding both until Driver B finishes its first county to avoid concurrent Serper hammering.
