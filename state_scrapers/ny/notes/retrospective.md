# New York HOA Scrape — Retrospective

Run ID: `ny_20260510_213944Z_claude` (started 2026-05-10 21:39 UTC, ~36-hour
wall-clock at first checkpoint).

> Scope: NY-wide (62 counties), with NYC 5 boroughs as the dominant slice (62%
> of the registry-seed universe is in Manhattan/Brooklyn/Queens/Bronx/Staten
> Island). Tri-state Driver C exported a small free-win to NJ/CT.

## TL;DR

- **Outcome**: 13,475 NY entities live on hoaproxy.org, **1,200 with documents**
  (8.91% coverage), up from 12,282 / 2 with docs at run start. **+1,198 new
  HOAs got their first document** during this pass.
- **Bank coverage**: 1,925 banked manifests across 14 county slugs (NYC 5
  boroughs + Westchester + Nassau + Suffolk + Rockland + Putnam + Monroe +
  Erie + Albany + Onondaga + unknown-county). ~2,000 PDFs in
  `gs://hoaproxy-bank/v1/NY/`.
- **Total marginal spend**: roughly **$25–35** (Serper ~$20, DocAI ~$10,
  OpenRouter ~$5). Well under the giant-state $150–250 envelope.
- **Coverage of estimated universe**: 8.91% of the registry-seed universe
  reached "has docs" status on the live site. The 12,275 docless stubs remain
  as map points (ZIP-centroid quality).
- **Structural ceiling for this pass**: Phase 5 prepare and Phase 8 import
  were heavily throttled by GCS/OAuth connection resets (compounding with FL +
  CA + AZ scrapes running in parallel). The bank has 1,925 manifests; only
  ~1,600 reached the live site this pass.

## What worked

### NYC ACRIS Open Data (Driver E) — the unsung win

The biggest novel contribution to the playbook. NYC's recorder system (ACRIS)
publishes its Real Property Master, Legals, and Parties tables as **free
Socrata datasets** with no auth or captcha. This is the FL Driver D
(county-recorder) equivalent — unblocked.

- `data.cityofnewyork.us/resource/bnx9-e6tj.json` → 18,911 DECL records
  (Manhattan 9,212 / Brooklyn 4,105 / Queens 3,980 / Bronx 1,614; Richmond uses
  a separate clerk).
- After residential property-type filter (`D0-D9 / R0-R9 / RR / RP / RG`),
  **1,811 unique condo declarations** survived.
- The image-fetch URL pattern took a small spike to find (a watchdog-killed
  agent stalled at this step):
  ```
  https://a836-acris.nyc.gov/DS/DocumentSearch/GetImage?doc_id=<id>&page=<N>
  ```
  with a `Referer: …/DocumentImageView?doc_id=<id>` header — without it the
  server 307s to a bandwidth-policy page. Pages come back as Group-4-fax
  TIFFs; PIL assembles them into multi-page PDFs.
- `MAX_PAGES_FOR_OCR_SCANNED=25` (default) rejected ~50% of ACRIS docs;
  bumping to 100 via the new `HOA_MAX_PAGES_FOR_OCR_SCANNED` env var unlocked
  them. NYC condo declarations regularly run 30–80 scanned pages.
- Final Driver E yield: **1,760 banked, 51 failed (~3% failure)**.

This pattern is reusable for any city that publishes recorder records as
Socrata Open Data (e.g., DC OTR's recorded-document portal is similar).

### Tri-state Driver C — free NJ/CT hits via post-OCR routing

220 Serper queries across 22 NY/NJ/CT property-management-company domains
produced 82 leads → 21 validated → 25 banked. 2 hits correctly **cross-state
routed** by post-OCR address detection:

- `gs://.../v1/CT/fairfield/greenwich-harbor-view/manifest.json`
- `gs://.../v1/NC/_unknown-county/organized-as/manifest.json`

This validates the discovery-only tri-state approach (don't combine bank
states; route at OCR time). Pattern transferable to any state cluster with
shared regional management companies.

### `smart_titlecase` deterministic slug repair

CT retrospective's win held up for NY: the bank's `is_dirty()` `shouting_prefix`
rule + `smart_titlecase` strategy in `hoaware/name_utils.py` cleanly handled
the ALL-CAPS NY DOS Active Corporation names (`"BAITING HOLLOW ESTATES
HOMEOWNERS ASSOCIATION, INC." → "baiting-hollow-estates"`). No LLM rename
needed for those.

## What was harder than expected

### ACRIS grantor names are noisy

The Parties table's grantor (`party_type='1'`) on a DECL record is *usually*
the condo association name, but a substantial fraction (~30–40%) are:

- Developer LLCs: `"ELEVEN W46 REALTY LLC"`, `"GLASTONBURY DEVELOPMENT CORP."`
- Individual persons: `"FERSZT, NATAN"`, `"FREEMAN, MORGAN J"`,
  `"GUTIERREZ, LIANA"`
- Non-residential entities that slip past the property-type filter (e.g.
  `"NEW YORK UNIVERSITY SCHOOL OF LAW FOUNDATION"` came back with property
  type `D1`)

These get banked under person-named slugs (`bronx/freeman-morgan-j/...`) and
need either (a) Phase 5 OCR slug-repair to extract the canonical name from
the declaration text, or (b) Phase 10 LLM rename. The Phase 5 path was
weak because the conservative regex couldn't anchor on a clean canonical
title in many ACRIS PDFs (the cover page is often just a recording stamp).

**Fold back to playbook**: when banking from a recorder system whose
grantor isn't reliably the association name, add a Phase 5 grantor-vs-
declared-name check + force-rename for person-named slugs.

### Phase 5 OCR throughput vs. parallel state runs

Phase 5 prepare crashed **8 times** during the run with
`google.auth.exceptions.TransportError: ConnectionResetError(54)` against
`oauth2.googleapis.com`. Each crash required a manual restart. The root
cause was contention with concurrent FL/CA/AZ prepare passes — three
processes all refreshing OAuth tokens against the same GCP project
exhausted the refresh rate-limit silently. Symptoms:

- Long stalls (30+ min of `0:00.00` CPU)
- Eventually a ReadTimeout on storage.googleapis.com → process death
- Restart from ledger; iterate alphabetically again

**Fold back to playbook**: when running multiple parallel state prepare
passes, stagger them or use distinct GCP service-account credentials per
state. The hot-edit-and-recompile pattern (`HOA_MAX_PAGES_FOR_OCR_SCANNED`
env override) worked but was painful to debug under crashes.

### LLM rename pass scale

`clean_dirty_hoa_names.py --no-dirty-filter` (the phase10_close.py default)
tried to process all 13,428 live NY HOAs at ~1.2/min. ETA: 186 hours. Killed
and switched to default `--dirty-filter` which scoped to 4,996 dirty names —
still ~12h ETA at current rate. Of the first 489 processed, **all 489**
were `skipped:empty_doc_text` (docless registry stubs with `1 X STREET
OWNERS LLC` shapes). The rename only does real work on dirty + with-docs
HOAs (~1,000 candidates).

**Fold back to playbook**: for states where most live entities are docless
registry stubs (large registry pull + low bank-to-live ratio), short-circuit
the LLM rename on `chunk_count == 0` BEFORE the dirty-filter check.

## Cost breakdown (approximate)

| Phase | API | Spend | Notes |
|---|---|---|---|
| Discovery — Driver A (registry-name × per-county Serper) | Serper | ~$15 | 13 counties × up to 1,500 queries each |
| Discovery — Driver C (tri-state mgmt-co) | Serper | ~$2 | 220 queries |
| Discovery — Driver E (ACRIS direct) | $0 | $0 | Free Socrata API + ACRIS image fetch |
| Phase 5 — OCR + slug repair (DocAI) | Google Document AI | ~$10 | ~6,500 pages OCR'd across ~1,600 bundles |
| Phase 10 — LLM rename (still running) | OpenRouter | ~$5 (projected) | DeepSeek-v4-flash |
| **Total** | | **~$30** | |

### Per-HOA economics

| Unit | Count | Cost per unit |
|---|---|---|
| Entity attempted (bank manifest) | 1,925 | ~$0.016 |
| HOA live with docs | 1,200 | ~$0.025 |

Comparable to FL ($0.022/live HOA) and CT ($0.022/live HOA).

## Final counts

```json
{
  "state": "NY",
  "registry_seed": 12369,
  "bank_manifests": 1925,
  "bank_pdfs": 2027,
  "ingest_ready_bundles": 1609,
  "live_profiles": 13475,
  "live_with_docs": 1200,
  "live_without_docs": 12275,
  "with_docs_pct": 8.91,
  "by_county_bank_distribution": {
    "kings": 693,
    "queens": 613,
    "bronx": 217,
    "new-york": 86,
    "westchester": 71,
    "suffolk": 45,
    "richmond": 43,
    "monroe": 37,
    "unresolved-name": 28,
    "nassau": 26,
    "albany": 16,
    "erie": 17,
    "unknown-county": 14,
    "onondaga": 8,
    "rockland": 8,
    "putnam": 5
  },
  "ocr_cost_usd_approx": 10,
  "phase5_restarts": 8
}
```

## Source-family yield

| Source family | Manifests | Final assessment |
|---|---|---|
| NY DOS Active Corporations Socrata seed | 12,369 seeded; 12,282 live as stubs at run start | Was pre-existing; backfilled by prior session |
| Driver A — registry-name × per-county Serper | ~700 bank manifests across NYC + first ring | High value; standard FL-canonical pattern works |
| Driver C — tri-state mgmt-co Serper | 25 banked incl. 2 cross-state-routed | Small but proves the pattern; expand mgmt-co list to 50+ next run |
| Driver E — NYC ACRIS Open Data | 1,760 banked from 1,811 residential DECL records | **Tier-1**, transferable to any Socrata-recorder city |
| Driver F — NY AG Offering Plan DB | NOT RUN | Reverse-engineering spike completed but execution deferred to a follow-up pass |

## Would not do again

1. **Don't run Phase 5 NY in parallel with FL + CA + AZ prepare passes.**
   Three concurrent OAuth-refreshing prepare loops killed Phase 5 NY eight
   times. Stagger the giant-state prepare runs or partition by GCP service
   account.
2. **Don't use `phase10_close.py --no-dirty-filter` on a state with >10k live
   entities.** ETA explodes to days. The dirty-filter default is correct for
   giant states.
3. **Don't trust the ACRIS Master `recorded_borough` field for routing.**
   Borough codes in Master sometimes diverge from Legals; trust Legals.
   Sample doc `2026033001164001` had Master.recorded_borough=1 (Manhattan)
   but Legals.borough=4 (Queens) — the Legals borough was correct.

## Unsung win

The `HOA_MAX_PAGES_FOR_OCR_SCANNED` env var override. Many NYC condo
declarations are 30–80 scanned pages; the default 25-page cap was rejecting
the majority. A one-line env var (now committed to `hoaware/pdf_utils.py`)
unblocked the ACRIS pipeline without touching FL/CA's cost guards.

## Cross-state lessons to fold back into the playbook

1. Add the **ACRIS pattern** to the playbook's Driver D section as a worked
   example: when a city publishes recorder records via Socrata Open Data,
   prefer that over reverse-engineering the public search portal.
2. Document the **OAuth-contention failure mode** in §5 (Risks specific to
   giant states). Three+ parallel prepare passes against a single GCP
   project will silently fail.
3. Add `HOA_MAX_PAGES_FOR_OCR_SCANNED` to the env-var inventory in CLAUDE.md
   (set to 100 for NY-style scanned-condo-declaration states).
4. Add the **grantor-vs-association-name** check to Phase 5 slug-repair:
   when banking from a recorder, the grantor party is *usually* but not
   *always* the association name. Build a Phase 5 cross-check.
5. The **tri-state Driver C** pattern (one mgmt-co sweep across a regional
   cluster, OCR-routed to the correct state bank prefix) works and should
   become the default for adjacent metro states. Candidate clusters:
   NY/NJ/CT, MD/DC/VA, FL-Southeast metro (Miami/Broward/Palm Beach has
   significant mgmt-co overlap).

## Reusable scripts

| Script | Phase | Reusable as-is? |
|---|---|---|
| `state_scrapers/ny/scripts/pull_ny_registry.py` | Phase 1 registry pull | NY-specific (Socrata); pattern generalizes to any `data.{state}.gov` SODA endpoint |
| `state_scrapers/ny/scripts/build_acris_seed.py` | Phase 1b ACRIS seed | NYC-specific Socrata join; generalizes to any city with similar recorder Open Data |
| `state_scrapers/ny/scripts/fetch_acris_pdf.py` | Driver E PDF fetch | NYC-specific URL pattern (`/DS/DocumentSearch/GetImage`); core PIL TIFF→PDF conversion is general |
| `state_scrapers/ny/scripts/ny_build_county_queries.py` | Driver A | General — adapt by changing seed path + state-name strings |
| `benchmark/run_ny_registry_county_sweep.sh` | Driver A runner | Per-state copy-and-edit |
| `benchmark/run_ny_replenisher.sh` | Driver A parallel orchestrator | General (just N=2 vs FL N=3-4 to coexist with concurrent state runs) |
| `state_scrapers/ny/leads/ny_tristate_management_company_domains.json` | Driver C source | Regional; expand per cluster |
| `benchmark/run_ny_tristate_mgmt_host_sweep.sh` | Driver C runner | Per-region copy-and-edit |
| `state_scrapers/ny/scripts/run_state_ingestion.py` | Phase 7-8 (prepare → import → verify) | General — bbox + STATE constants change |

## Open follow-ups

- **AG Offering Plan Database (Driver F)**: validated the search portal +
  plan detail page + Documents tab structure; reverse-engineered the
  filesForm POST mechanism but did not execute. Worth a half-day spike in a
  second pass — would add canonical condo/coop offering plan PDFs for every
  accepted plan since the AG started posting (recent enhancement).
- **Driver B (county-broad keyword Serper)**: pending; would give second-
  pass breadth coverage for HOAs the registry seed missed. Estimate ~$15
  additional Serper + ~$5 DocAI.
- **NYC PLUTO (E1 geometry layer)**: BBL-anchored polygon enrichment for
  the ~1,760 ACRIS-banked manifests. Would lift NY map quality from
  ZIP-centroid to building-precision for the NYC slice.
- **Staten Island (Richmond County clerk)**: ACRIS doesn't cover it.
  Richmond's 949 registry entities currently have only registry-name
  Serper hits. A bespoke Richmond clerk integration would parallel Driver E
  for the 5th borough.
- **Phase 10 LLM rename continuation**: running in background at
  `state_scrapers/ny/results/ny_20260510_213944Z_claude/dirty_renames.jsonl`.
  Most early entries are docless `skipped:empty_doc_text`; will accelerate
  when iterator hits with-docs HOAs. ETA ~12h to complete the 4,996 dirty
  filter.
