# New York HOA Discovery Handoff

State: **NY**
Started: 2026-05-10
Run ID: `ny_20260510_213944Z_claude`

## Why New York needs a bespoke plan

NY is a giant state (>10k entities, >30 counties) but unlike FL/CA/TX it has a
dominant urban core that drives the shape of every phase:

- **NYC 5 boroughs hold 62%** of the 12,369-entity DOS registry seed
  (`state_scrapers/ny/leads/ny_registry_seed.jsonl`, pulled from
  `data.ny.gov/api/views/n9v6-gdp6`).
- **Coops outnumber condos and HOAs** (4,959 / 2,901 / 4,509). NY is the only
  state where the dominant governance form is the cooperative apartment
  corporation, not Chapter-720-style HOAs.
- **NYC ACRIS** is a free, public, Socrata-API document recorder system. **No
  reCAPTCHA, no auth, no Developer API license.** 18,911 DECL records across
  4 boroughs (Manhattan 9,212 / Brooklyn 4,105 / Queens 3,980 / Bronx 1,614).
  This is the **FL Driver D** equivalent, except unblocked.
- **NY AG Real Estate Finance Bureau Offering Plan Database** (recent
  enhancement: posting plan PDFs themselves, not just metadata) at
  `offeringplandatasearch.ag.ny.gov/REF/`. Potentially Tier-0 for accepted
  condo/coop/HOA plans, needs reverse-engineering spike on the filesForm POST.
- **Staten Island gap**: Richmond County uses a separate clerk, not in ACRIS.
  Served by Driver A only.

## Goals (user-set, 2026-05-10)

1. **Max entities** (coops + condos + HOAs)
2. **Prioritize getting docs** — bank density > bare names
3. **Make sure it's all clean** — no junk slugs, no junk docs, no
   cross-state pollution, no plats/subdivisions masquerading as HOAs

The playbook is a starting point, not a constraint.

## Strategy: discovery-only (Track 1 already done by prior session)

**Track 1 — Registry stub backfill: ALREADY DONE.** `GET /admin/state-doc-coverage`
on 2026-05-10 21:48Z shows NY at **12,282 live, only 2 with docs, 0.02% coverage**.
The 12,369-entity DOS seed was bulk-stubbed (under source
`"ny-dos-active-corporations"`) in an earlier session. Source string is
**already burned** per the never-reuse rule — any future NY-DOS supplementation
must use a different source string (e.g. `"ny-dos-active-corporations-v2-2026-05"`).

This means **the entire run is Track 2** — document discovery feeding existing
stubs by name match through prepare → /upload.

**Track 2 — Document discovery (fills the existing 12,280 docless stubs).**
Five drivers running in parallel:

| Driver | Source | Scope | Status |
|---|---|---|---|
| A | Registry-name × per-county Serper | All 62 NY counties (10 top first) | FL-canonical pattern adapted for NY |
| B | County-broad keyword Serper | Top 10 counties (5 boroughs + Westchester/Nassau/Suffolk/Rockland/Erie/Monroe) | FL-canonical |
| C | Mgmt-co host expansion | **Tri-state NY+NJ+CT joint** | New variant; post-OCR routes to correct state |
| E | ACRIS Open Data | NYC 4 boroughs (DECL via Socrata) | New — replaces shelved Driver D |
| F | AG Offering Plan DB | Statewide condo/coop offering plans | Spike first; lower priority |

**Concurrency cap**: N=2 parallel county sweeps (vs FL's N=4) because CA and
AZ scrapes are running in parallel and need Serper rate-limit headroom.

## Phase shape (giant-state playbook with NY adaptations)

| Phase | Action |
|---|---|
| 0 | Cleanup mis-routed bank manifests (done) + DB snapshot |
| 1a | Bulk stub backfill 12k DOS entities → live |
| 1b | Build ACRIS seed via Socrata join (Master + Legals + Parties → name+BBL+address) |
| 1c | Adapt FL canonical scripts to NY |
| 2 | Drivers A/B/C/E in parallel; Driver F spike if time |
| 3 | Pre-OCR metadata repair (slug variant merge, mis-routed cleanup) |
| 4 | Doc filter (hard-reject categories: court/government/tax/membership_list) |
| 5 | OCR + slug repair + geo extract via `prepare_bank_for_ingest.py` with `--max-docai-cost-usd 150` |
| 5b | State-mismatch reroute (aggressive: NY name patterns are high collision risk; expect 15-20% per CT retro) |
| 6 | Geometry stack: E4 ZIP centroid → E3 OSM Nominatim → E2 HERE → E1 **NYC PLUTO** (BBL→polygon, new layer replacing FL Subd_poly) |
| 7 | Prepare bundles |
| 8 | Import to live via `/upload` (75s gap between calls per memory) |
| 10 | Phase 10 close: `phase10_close.py` + LLM rename + hard-delete junk (no tagging per memory) |

## Hard rules carried from CLAUDE.md / audit-pipeline.md

- `/admin/clear-hoa-docs` not `/admin/delete-hoa` for "real HOA, bad content"
  cleanup (preserves geometry).
- `on_collision: "disambiguate"` on all bulk stub backfills.
- **Plats/subdivisions are NOT HOAs.** No `*_subdivisions` GIS layers without
  `grade_entity_names.py` first.
- `source` strings never reused. NY DOS uses `"ny-dos-active-corporations"`.
- Snapshot via `POST /admin/backup-full` before any bulk operation.
- Pacing: 75s gap between `/upload` calls (Render OOM mitigation).
- OCR-first slug + geo at prepare time, not from search snippets.

## Budget envelope

| Component | Estimate |
|---|---|
| Serper | $60–90 (12k seed × 2 queries × top 10 counties) |
| DocAI | $80–150 (NYC offering plans are pre-2010 scanned, image-heavy) |
| OpenRouter | $5–10 |
| ACRIS / AG portal | $0 (free public APIs) |
| **Total** | **$150–250** |

## Pre-launch bank state

Cleaned of 14 mis-routed dirs (FL/CA/AZ/GA county slugs under `v1/NY/`) on
2026-05-10. Bank now empty.

## Pre-launch live state

`/admin/state-doc-coverage` snapshot at 2026-05-10 21:48Z:

| State | Live | With docs | Coverage |
|---|---|---|---|
| FL | 36,237 | 20 | 0.06% |
| CA | 25,670 | 431 | 1.68% |
| TX | 16,290 | 773 | 4.74% |
| **NY** | **12,282** | **2** | **0.02%** |
| WA | 8,988 | 134 | 1.49% |
| CO | 8,462 | 436 | 5.15% |
| NJ | 28 | 28 | 100.00% |
| CT | 3,503 | 99 | 2.83% |

NY has the **highest absolute count of docless stubs** of any state (12,280),
making it the highest-value scrape target for doc-density work right now.

DB snapshot taken at `gs://hoaproxy-backups/db/hoa_index-20260511-014454.db`
via `POST /admin/backup-full` before discovery launch.

## Log

- 2026-05-10 21:11Z: Registry seed already exists (12,369 entities, pulled
  earlier — see `state_scrapers/ny/scripts/pull_ny_registry.py`).
- 2026-05-10 21:39Z: Validated 3 NY-specific sources via probes:
  - NYC ACRIS Socrata: 18,911 DECL records, Master+Legals join works clean.
  - NY AG Offering Plan DB: search.action + planFormServlet reachable; document
    download needs reverse-engineering spike.
  - NYC PLUTO (via BBL from ACRIS Legals) for E1 polygon enrichment.
- 2026-05-10 21:46Z: Cleaned 14 mis-routed NY bank dirs (`gsutil rm` sequential
  after `-m` got stuck on parallel FL gsutil contention).
- 2026-05-10 21:50Z: Spot-checked 30 random seed names — all look like real
  residential associations. Proceeding to backfill without `grade_entity_names`
  (which needs live rows to sample from, and NY has none yet).
- 2026-05-10 21:53Z: Driver A scripts shipped (`benchmark/run_ny_registry_county_sweep.sh`,
  `benchmark/run_ny_replenisher.sh`). Driver C tri-state mgmt-co scripts
  shipped (`benchmark/run_ny_tristate_mgmt_host_sweep.sh` + 22-company JSON).
  ACRIS seed builder shipped (`state_scrapers/ny/scripts/build_acris_seed.py`).
- 2026-05-10 21:55Z: Putnam Driver A smoke test launched.
- 2026-05-10 21:56Z: ACRIS full pull launched in background.
- 2026-05-10 21:57Z: Putnam smoke RESULT — 190 queries → 9 validated → 7 clean
  → 7 PDFs banked. 0 errors. Bank now has `v1/NY/putnam/{bluffs-offering-plan,
  hill-dale,kentwood-lake,mahopac-point,roaring-brook-lake}/` + 1
  unresolved-name. **Pipeline works end-to-end for NY.**
- 2026-05-10 21:58Z: ACRIS pull DONE — 1,811 unique residential leads from
  18,911 master DECL records (21% residential rate, 116 dups collapsed).
- 2026-05-10 21:59Z: **Driver A replenisher launched** at N=2 parallel across
  13 counties: `Kings, New York, Queens, Richmond, Westchester, Nassau,
  Suffolk, Bronx, Rockland, Monroe, Albany, Erie, Onondaga`. Currently
  running Kings + New York (Manhattan).
- 2026-05-10 21:59Z: **Driver C launched** — tri-state mgmt-co sweep, 220
  queries across 22 NY/NJ/CT property mgmt domains. Post-OCR state routing
  will distribute hits to correct NY/NJ/CT bank prefixes.
- 2026-05-10 21:59Z: ACRIS PDF fetcher subagent still running (Driver E
  blocked on this).
