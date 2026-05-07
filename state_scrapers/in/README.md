# Indiana HOA Ingestion

Per-county Serper discovery (KS/TN-style) for Indiana. Indiana's INBiz public
business search is reCAPTCHA-gated for non-whitelisted IPs and the bulk
download is $9,500, so we don't have a free SoS-registry universe like CT/RI.
Indiana is medium-sized (6.8M pop) with reasonably distinctive city names
(Indianapolis, Carmel, Fishers, Noblesville, Evansville), so per-county
keyword Serper is a workable primary.

## Layout

```
state_scrapers/in/
  README.md                                    # this file
  queries/                                     # one Serper queries file per
                                               #   priority county; one
                                               #   "other_metros" sweep for
                                               #   smaller counties
  scripts/
    in_geo.py                                  # IN bounding box, CITY_COUNTY
                                               #   map, CITY_CENTROIDS
    enrich_in_locations.py                     # ZIP-centroid backfill via
                                               #   zippopotam.us; Nominatim is
                                               #   opportunistic only
    run_state_ingestion.py                     # end-to-end pipeline runner
  leads/                                       # (unused; benchmark/scrape
                                               #   _state_serper_docpages.py
                                               #   writes to benchmark/results)
  results/{run_id}/
    preflight.json
    discover_NN_*.log                          # per-county Serper run logs
    20_prepare.log
    prepared_ingest_ledger.jsonl
    prepared_ingest_geo_cache.json
    live_import_report.json
    live_verification.json
    30_location_enrichment.log
    location_enrichment.jsonl
    zip_centroid_cache.json
    final_state_report.json
```

## Run end-to-end

```bash
.venv/bin/python state_scrapers/in/scripts/run_state_ingestion.py \
  --apply \
  --max-docai-cost-usd 50
```

## Resume / partial reruns

```bash
# Re-run only Hamilton + Marion counties:
.venv/bin/python state_scrapers/in/scripts/run_state_ingestion.py \
  --apply --skip-prepare --skip-import --skip-locations \
  --counties-only "Hamilton,Marion"

# Re-prepare and import after additional discovery:
.venv/bin/python state_scrapers/in/scripts/run_state_ingestion.py \
  --apply --skip-discovery --skip-locations
```

## Pipeline

1. **Per-county Serper discovery** — `benchmark/scrape_state_serper_docpages.py`
   with `--probe`, called once per priority county with the matching
   queries file and `--default-county`. Banks PDFs into
   `gs://hoaproxy-bank/v1/IN/{county}/{slug}/`.

2. **Prepared bundles** — `scripts/prepare_bank_for_ingest.py --state IN`
   filters page-one OCR-style, runs full DocAI on scanned PDFs (capped by
   `--max-docai-cost-usd`), enriches geography, writes prepared bundles
   to `gs://hoaproxy-ingest-ready/v1/IN/`.

3. **Live import** — `POST /admin/ingest-ready-gcs?state=IN&limit=50` in a
   loop until empty. Endpoint caps at 50 per call.

4. **Map backfill** — `enrich_in_locations.py --apply` reads
   `/hoas/summary?state=IN`, fills missing coords via ZIP centroid
   (zippopotam.us) → city centroid (`CITY_CENTROIDS`) → city_only.
   Out-of-state coords are demoted to city_only.

5. **Final report** — `final_state_report.json` aggregates raw manifest
   count, prepared count, import results, live counts, map quality
   distribution.

## Why per-county Serper instead of SoS

INBiz `/PublicIpAddress/GetIpAddress` returns `false` for non-whitelisted
external IPs, which means reCAPTCHA gates every search. The bulk-download
business entity file is $9,500 + $500/mo, gated behind an INBiz account.
Per-county Serper produces noisier leads (governing-doc PDFs surface from
private HOA websites, county recorder uploads, and management-company
portals) but doesn't require state cooperation. Quality is enforced
downstream: `prepare_bank_for_ingest.py` rejects junk via DocAI page-one
text classification, and `enrich_in_locations.py` rejects out-of-state
coordinates and demotes city-only records.
