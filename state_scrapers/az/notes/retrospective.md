# Arizona HOA Ingestion — Retrospective

**Run:** 2026-05-10 (in progress)
**Reference playbook:** [`docs/giant-state-ingestion-playbook.md`](../../../docs/giant-state-ingestion-playbook.md)
**Operator:** wyatt + Claude Opus 4.7 (autonomous mode)

This is the operator-facing summary of the AZ giant-state run. It will be filled in as the pipeline completes; values marked `TBD` are placeholders.

---

## Outcome (top-line)

| Metric | Value |
|---|---|
| `bank_entity_count` | TBD |
| `bank_with_docs_count` | TBD |
| `live_entity_count` (post-import) | TBD |
| Geometry: subdpoly polygons | TBD |
| Geometry: HERE address | TBD |
| Geometry: OSM polygons / centroids | TBD |
| Geometry: ZIP centroid only | TBD |
| Total Serper spend | TBD |
| Total DocAI spend | TBD |
| Total OpenRouter spend | TBD |
| Total wall time | TBD |

---

## Run shape — pivots from FL canonical

The AZ playbook differed materially from the FL canonical:

1. **No free state corp registry.** Arizona Corporation Commission's bulk corp data is a $75 mailed CSV (M027 form, 2-4 week lead time), not the free SFTP dump FL has via Sunbiz. Decision (recorded in `docs/giant-state-ingestion-playbook.md` §0): **skip ACC entirely**; use Maricopa+Pima ArcGIS subdivision-polygon layers as the seed instead.
2. **Driver A' (subdpoly-name Serper)** replaced Driver A (registry-name Serper). Maricopa published 31,471 named subdivisions; Pima 6,447. After noise filtering and dedup → 35,481 unique seed names with geometry pre-attached. This effectively merged Driver A and Geometry E1 into one pull.
3. **Driver E (master-planned community pass)** is new for AZ. ~70 iconic communities (Sun City variants, Anthem, DC Ranch, Verrado, Estrella, Eastmark, Power Ranch, Encanterra, Trilogy, Saddlebrooke, Rancho Sahuarita, etc.) get per-portal Serper sweeps anchored on each community's name + alt_names + suspected portal domains.
4. **Maricopa sub-divided into 4 city clusters** for Driver B (phoenix-core, east-valley, west-valley, southeast); Pima sub-divided into 2 (tucson-core, suburban). Each cluster is a separate sweep with its own queries file and result dir, but banks under the canonical `maricopa` / `pima` slug. Other 13 counties → one row each.
5. **Re-ranked driver order** to match AZ's market structure:
   - 1 (highest yield): Driver A' (subdpoly-name Serper) + Driver E (master-planned)
   - 2: Driver B (sub-county broad)
   - 3: Driver C (mgmt-host) — runs early because cheap, but lower yield in AZ where mgmt cos gate documents behind HomeWiseDocs/CondoCerts/Nabr/Buurt portals.

## Driver yields

TBD — fill in from `state_scrapers/az/results/` after each driver completes.

| Driver | Sweeps | Manifests banked | Notes |
|---|---|---|---|
| A' (subdpoly-name) | TBD | TBD | TBD |
| B (sub-county broad) | TBD | TBD | TBD |
| C (mgmt-host) | 1 | 22 | First-banked; validated end-to-end pipeline |
| E (master-planned) | 1 | TBD | TBD |

## Geometry stack outcome

| Tier | Source | Manifests stamped |
|---|---|---|
| E4 | ZIP centroid (Census 2023 ZCTA) | TBD |
| E3 | OSM Nominatim place lookup | TBD |
| E2 | HERE address geocoder | TBD |
| E1 | Maricopa+Pima ArcGIS subdivision polygon | TBD |

## State-mismatch reroute

TBD — will fill in count of OCR-flagged misroutes promoted to other states' bank prefixes after the reroute pass completes.

## Slug pollution

TBD — count of slug repairs applied during OCR pass.

## What didn't I try

- **Driver D (county recorder)** — shelved per playbook, same as FL. Maricopa Recorder, Pima Recorder both have public CCR search but the per-search captcha + per-result paywall makes bulk extraction prohibitive without a license.
- **ACC eCorp scraping** — eCorp was decommissioned 2026-01-02 and replaced with Arizona Business Center, which has a captcha-protected per-record lookup but no bulk export below the $75 paid M027 form.
- **Cross-state HOA management chains** — FirstService Residential, CCMC, Associa all manage AZ communities; their portals were swept (Driver C) but most documents are behind HomeWiseDocs/CondoCerts/Nabr Network/Buurt paywalls. A future pass could try to enumerate community subdomains per management company.

## Companion artifacts

- Recon notes: see `state_scrapers/az/notes/recon.md` (TBD if produced)
- Scripts: `state_scrapers/az/scripts/`
- Sweep results: `benchmark/results/az_*/`
- Subdpoly seed: `state_scrapers/az/data/az_subdpoly.jsonl` (gitignored — 112 MB)
- Master-planned list: `state_scrapers/az/leads/az_master_planned.json`
- Mgmt-co domains: `state_scrapers/az/leads/az_management_company_domains.json`

## Cross-references

- [Giant-state playbook](../../../docs/giant-state-ingestion-playbook.md)
- [Multi-state playbook](../../../docs/multi-state-ingestion-playbook.md)
- FL canonical retrospective: `state_scrapers/fl/notes/retrospective.md`
