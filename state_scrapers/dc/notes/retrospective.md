# DC HOA Scrape Retrospective

Two-pass run history. Pass 1 (overnight 9-state orchestrator, 2026-05-08)
ran neighborhood-anchored Serper sweeps and produced 1 live HOA after
Phase 10 deletion of mostly .gov/court noise. Pass 2 (DC CAMA + name-list
namelist_discover, 2026-05-08 → 2026-05-09) migrated DC to the
[name-list-first playbook](../../../docs/name-list-first-ingestion-playbook.md)
using the DC GIS CONDO REGIME table.

## TL;DR

- **Final state:** 21 live HOAs, 16 mapped (76%), 0 out-of-bbox.
- **Coverage of estimated universe:** ~0.6% of the 3,289-condo registry.
  Most DC condos do not publish governing docs to the public web — they're
  paywalled at DC Recorder of Deeds or login-walled at CINC/AppFolio.
- **Discovery pattern adopted:** name-list-first per
  `docs/name-list-first-ingestion-playbook.md` (registry → name-anchored
  Serper → bank with `pinned_name=True`).

## Pipeline numbers

| Stage | Count |
|---|---|
| CAMA CONDO REGIME entities pulled | 3,289 |
| Entities with at least one PDF banked | 565 (17.5%) |
| Total docs banked | 740 |
| Bundles prepared (post DocAI) | 408 |
| Bundles imported | 249 |
| Live HOAs after import | 299 |
| LLM rename `is_hoa: false` rejections | 251 |
| Live HOAs after Phase 10 hard-delete | 21 |
| Mapped (zip_centroid) | 16 (76%) |
| Out-of-bbox map points | 0 |

## Why such a high LLM rejection rate (251 of 280)

The CAMA name-anchored Serper queries surfaced PDFs that mentioned the
condo name but were NOT governing documents:

- SEC filings (10ELEVEN Condominium → financial corporation filing)
- Supreme Court petitions (1130 Columbia Rd Nw Condo)
- Federal Register issues (1441 Fernwood Condominium)
- Academic articles about public housing stigma (1430 K Street Condominium)
- Government rulemaking notices (14 & K Lofts Condominium)
- Single-unit deeds (The New York Condominium)
- Bankruptcy case-law reviews (The Newport Condominium)

The condos themselves are real DC entities. The Phase 10 LLM correctly
identified the *documents* as non-governing and deleted the entity rather
than have it appear with junk docs. This is the right behavior for the
current pipeline (1 entity = 1+ doc, doc validates entity).

## Source families attempted

- Per-neighborhood Serper (8 neighborhoods × ~17 queries): pass 1, weak
  yield.
- Mgmt-co + statute-anchored Serper: pass 1.
- DC CAMA name-list-first per `dc_cama_pipeline.py` + `namelist_discover.py`:
  pass 2 — primary yield path.

## Lessons learned

1. **Name-list-first is necessary for DC.** Keyword-Serper-per-neighborhood
   produced too much .gov/court noise. The CAMA registry gives an
   authoritative entity universe.
2. **CAMA names are tax-record-style** ("3025 Porter Street Condo") and
   need looser query variants to find the legal name on bylaws ("3025
   Porter Street Condominium Association"). The
   `namelist_discover.queries_for_seed()` strip-and-expand pattern works.
3. **`pinned_name=True` in `bank_hoa()` is required** for registry-derived
   names — many DC condos have street-numbered names that trip
   `is_dirty()` and would otherwise be routed to `_unresolved-name/`.
4. **Phase 10 LLM rejection rate is structurally high (~89%)** for DC's
   CAMA-anchored Serper because most DC condos genuinely don't have
   public governing docs. The 21 live entities are the floor, not a
   failure — they represent the real condos with real governing docs
   that Google has indexed.
5. **Future improvement to push DC higher:** soft-deletion path that
   keeps registry-derived entities visible even when their banked docs
   are wrong (so users can manually upload). Out of scope for this run.

## Files

- Bank: `gs://hoaproxy-bank/v1/DC/dc/{slug}/manifest.json` (796 manifests)
- Seed: `state_scrapers/dc/leads/dc_cama_condo_seed.jsonl`
- Discovery ledger: `state_scrapers/dc/results/dc_namelist_v2_*/namelist_ledger.jsonl`
- Prepare ledger: `state_scrapers/dc/results/*_finalize/prepared_ingest_ledger.jsonl`
- Phase 10 ledger: `state_scrapers/dc/results/*_finalize/name_cleanup_unconditional.jsonl`
- Final state report: `state_scrapers/dc/results/*_finalize/final_state_report.json`
