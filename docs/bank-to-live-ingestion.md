# Bank to Live Ingestion

This is the production bulk path for moving public HOA governing documents from
the raw discovery bank into the live searchable site.

It is separate from normal website upload. `/upload` and `/upload/anonymous`
remain the user-facing paths for one-off HOA creation and document uploads.

## Buckets

- Raw bank input: `gs://hoaproxy-bank/v1/{STATE}/{county}/{hoa-slug}/...`
- Prepared queue: `gs://hoaproxy-ingest-ready/v1/{STATE}/{county}/{hoa-slug}/{bundle-id}/...`

The raw bank is preserved as input. Do not mutate or drain it directly into the
live site.

## Funnel Counts

Raw bank manifest counts are not the same as live imported HOA/document counts.
A state may have hundreds of banked HOA profiles, but the prepared worker only
writes a live bundle when a manifest contains at least one eligible governing
document after filtering. Rejected records remain auditable in the local ledger
and in each bundle's `rejected_documents`.

Common reasons a banked HOA does not become a prepared live bundle:

- no PDFs found yet
- only low-value documents such as minutes, financials, or insurance records
- PII-risk, junk, or unsupported categories
- duplicate SHA already prepared or already live
- OCR budget cap or extraction error
- wrong-state evidence

Low-value or unsupported category is not itself a final rejection for banked
PDFs. If the file is not a duplicate, junk/PII, wrong-state, or over the
page/cost cap, the worker reviews first-page text before deciding. For scanned
PDFs that means OCRing only page 1, classifying that text, and running
full-document OCR only if the page-1 review identifies a germane governing
document.

## Prepared Bundle Shape

Each prepared bundle contains:

```text
bundle.json
docs/{sha256}.pdf
texts/{sha256}.json
status.json
```

`texts/{sha256}.json` uses the same sidecar shape as `/upload`:

```json
{
  "pages": [
    {"number": 1, "text": "extracted page text"}
  ],
  "docai_pages": 0
}
```

Render imports only bundles with text sidecars. If a sidecar is missing or
invalid, the importer marks the bundle `failed`; it does not run PyPDF or
Document AI as a fallback.

`bundle.json` should also carry the best available live-site metadata:

- `hoa_name`
- `metadata_type`
- `website_url`
- `address.city`, `address.county`, `address.state`, and any street/ZIP found
- `geometry.boundary_geojson` when a credible subdivision/neighborhood polygon exists
- `geometry.latitude` / `geometry.longitude` as the polygon centroid or a high-quality point
- `geometry.location_quality`: `polygon`, `address`, `zip_centroid`, `city_only`, or `unknown`

The map only shows polygon, address, and ZIP-centroid quality rows. Avoid
city-only points because they stack unrelated HOAs in the same downtown.

## Geography Enrichment

Geography belongs in the pre-Render prepared stage. Scrapers should collect raw
clues, then `scripts/prepare_bank_for_ingest.py` resolves missing geography
before writing bundles.

The prepared worker queries Nominatim/OSM for missing boundaries by default,
using a local cache:

```bash
python scripts/prepare_bank_for_ingest.py \
  --state KS \
  --limit 10000 \
  --max-docai-cost-usd 25 \
  --geo-cache data/prepared_ingest_geo_cache.json
```

Use `--skip-geo-enrichment` only for emergency document-only runs. The worker
accepts only credible OSM polygon results for map-quality `polygon` rows. If no
polygon is found, follow with the ZIP cleanup path:

```bash
curl -sS -H "Authorization: Bearer $JWT_SECRET" \
  "https://hoaproxy.org/admin/extract-doc-zips?state=KS"
```

Geocode the best ZIPs locally through a ZCTA gazetteer and post them to
`/admin/backfill-locations` with `location_quality="zip_centroid"`.

## Prepare Locally or in GCP

Run the worker somewhere with enough memory and OCR credentials:

```bash
source .venv/bin/activate
set -a; source settings.env; set +a

python scripts/prepare_bank_for_ingest.py \
  --state KS \
  --limit 25 \
  --max-docai-cost-usd 25 \
  --dry-run
```

When the dry run looks right, omit `--dry-run`.

Useful options:

- `--county Johnson` limits the bank prefix.
- `--include-low-value` allows minutes, financials, and insurance docs.
- `--ledger data/prepared_ingest_ledger.jsonl` records decisions and OCR usage.
- `--geo-cache data/prepared_ingest_geo_cache.json` caches Nominatim responses.
- `--skip-geo-enrichment` disables OSM/Nominatim lookup.
- `--prepared-bucket hoaproxy-ingest-ready` overrides the queue bucket.

The worker filters hard rejects before OCR: PII, obvious junk, page-cap
violations, wrong-state evidence, and exact duplicates. Every remaining
curated-bank candidate gets a page-one review before low-value or unsupported
rejection is final. This protects OCR spend while avoiding blind drops of
relevant scanned governing documents.

## Import on Render

Call the admin endpoint with the JWT secret:

```bash
curl -sS -X POST \
  "https://hoaproxy.org/admin/ingest-ready-gcs?state=KS&limit=5" \
  -H "Authorization: Bearer $JWT_SECRET"
```

For validation without claim/write/ingest:

```bash
curl -sS -X POST \
  "https://hoaproxy.org/admin/ingest-ready-gcs?state=KS&limit=5&dry_run=true" \
  -H "Authorization: Bearer $JWT_SECRET"
```

The importer claims `ready` bundles with a GCS generation precondition, downloads
one bundle at a time, writes PDFs under `HOA_DOCS_ROOT/{hoa_name}/`, upserts
location metadata, and calls `ingest_pdf_paths(..., pre_extracted_pages=...)`.

## Status Values

- `ready`: prepared and available for Render.
- `claimed`: importer has claimed the bundle.
- `imported`: live ingest completed without per-document failures.
- `failed`: bundle validation or ingest failed; inspect `status.json.error`.
- `skipped`: reserved for explicit operator skips.
