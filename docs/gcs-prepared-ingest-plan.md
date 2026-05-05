# GCS Prepared Ingest Plan

Goal: bulk move scraped HOA governing documents from the GCS document bank into the live site without making Render do expensive OCR or handle large multipart upload batches.

Normal website users must still be able to create an HOA and upload documents through the existing `/upload` and `/upload/anonymous` paths. This plan adds a separate admin/bulk path for banked public documents; it does not replace user upload.

## Current Baseline

Already exists:

- Raw discovery bank: `gs://hoaproxy-bank/v1/{STATE}/{county}/{hoa-slug}/manifest.json` plus `doc-{sha}/original.pdf` and `precheck.json`.
- `/upload` accepts `extracted_texts` sidecars and skips server-side extraction when `pre_extracted_pages` is supplied.
- `hoaware.ingest.ingest_pdf_paths()` can ingest PDFs from disk using supplied page text.
- `hoaware.pdf_utils.extract_pages()` has the right OCR routing:
  - `text_extractable=True`: PyPDF only.
  - `text_extractable=False`: DocAI only.
  - `text_extractable=None`: PyPDF first, DocAI for blank pages only.
- Tests prove sidecars bypass Render extraction and log local DocAI page counts.

Missing:

- A durable prepared-ingest queue in GCS.
- A local/GCP prep worker that filters banked docs, runs OCR only when needed, and writes upload-ready bundles.
- A Render admin importer that reads prepared bundles from GCS and ingests them without OCR.
- A runbook/schema so this can be used repeatedly for KS, TN, GA, and future states.

## Architecture

Use three stages:

1. **Discovery bank**
   - Existing raw sink.
   - Scrapers write public findings with `bank_hoa()`.
   - No live-site ingestion happens here.

2. **Prepared ingest queue**
   - New GCS prefix, for example `gs://hoaproxy-ingest-ready/v1/{STATE}/{county}/{hoa-slug}/{bundle-id}/`.
   - Produced by a local or GCP worker, not Render.
   - Contains only filtered, approved, OCR-ready records.

3. **Render importer**
   - Admin-only endpoint/worker reads prepared bundles from GCS.
   - Downloads one small bundle at a time.
   - Saves PDFs under `HOA_DOCS_ROOT`.
   - Calls `ingest_pdf_paths()` with `pre_extracted_pages`.
   - Does not call DocAI.

## Prepared Bundle Schema

Each bundle directory should contain:

```text
bundle.json
docs/{sha}.pdf
texts/{sha}.json
status.json
```

`bundle.json`:

```json
{
  "schema_version": 1,
  "bundle_id": "sha-or-uuid",
  "source_manifest_uri": "gs://hoaproxy-bank/v1/KS/johnson/example/manifest.json",
  "state": "KS",
  "county": "Johnson",
  "hoa_name": "Example Homes Association",
  "metadata_type": "hoa",
  "website_url": "https://example.org",
  "address": {
    "city": "Overland Park",
    "state": "KS",
    "county": "Johnson"
  },
  "geometry": {
    "boundary_geojson": null,
    "latitude": null,
    "longitude": null
  },
  "documents": [
    {
      "sha256": "...",
      "filename": "declaration.pdf",
      "pdf_gcs_path": "gs://hoaproxy-ingest-ready/v1/KS/johnson/example/bundle/docs/sha.pdf",
      "text_gcs_path": "gs://hoaproxy-ingest-ready/v1/KS/johnson/example/bundle/texts/sha.json",
      "source_url": "https://source.example/declaration.pdf",
      "category": "ccr",
      "text_extractable": false,
      "page_count": 28,
      "docai_pages": 28,
      "filter_reason": "valid_governing_doc"
    }
  ],
  "rejected_documents": [
    {
      "sha256": "...",
      "source_url": "...",
      "reason": "junk:minutes"
    }
  ],
  "created_at": "2026-05-05T00:00:00Z"
}
```

`texts/{sha}.json` uses the existing `/upload` sidecar shape:

```json
{
  "pages": [
    {"number": 1, "text": "..."}
  ],
  "docai_pages": 28
}
```

`status.json` tracks queue state:

```json
{
  "status": "ready",
  "claimed_by": null,
  "claimed_at": null,
  "imported_at": null,
  "error": null
}
```

Allowed statuses: `ready`, `claimed`, `imported`, `failed`, `skipped`.

## Filtering Rules

The prep worker must filter before OCR.

Keep only:

- `ccr`
- `bylaws`
- `articles`
- `rules`
- `amendment`
- `resolution`

Reject before OCR:

- PII categories: `membership_list`, `ballot`, `violation`.
- Junk categories: `court`, `tax`, `government`, `real_estate`, `unrelated`.
- Low-value optional categories for bulk import: `minutes`, `financial`, `insurance`, unless explicitly enabled.
- Files over the upload/page cap.
- Bad state/county evidence when the bundle target is known.
- Exact duplicate SHA already prepared or already live.

Use bank `precheck.json` as a hint, not an authority. If missing or weak, re-run local precheck from the PDF bytes before deciding.

## OCR Policy

Run OCR in the prep worker, not on Render.

Recommended routing:

- If PyPDF extracts meaningful text from enough pages: use PyPDF pages, `docai_pages=0`, `text_extractable=true`.
- If first-page and sample text are blank/scanned: run DocAI for the whole document, `docai_pages=page_count`, `text_extractable=false`.
- If mixed: run PyPDF first and DocAI only blank pages, `text_extractable=null` or recorded as mixed in bundle metadata.

The prep worker should maintain a local ledger with:

- Manifest URI.
- Document SHA.
- Category decision.
- Text extractability decision.
- Page count.
- DocAI pages.
- Cost estimate.
- Prepared GCS paths.
- Rejection reason.

## Render Importer

Add a new admin-only path, separate from `/upload`:

```text
POST /admin/ingest-ready-gcs?state=KS&limit=10&dry_run=false
```

Behavior:

1. List `ready` bundles for the requested state.
2. Claim one bundle using a GCS generation precondition on `status.json`.
3. Download `bundle.json`, PDFs, and text sidecars.
4. Save PDFs under `HOA_DOCS_ROOT/{hoa_name}/`.
5. Build `metadata_by_path` with:
   - `category`
   - `text_extractable`
   - `source_url`
   - `pre_extracted_pages`
6. Insert/update location metadata using bundle address/geometry.
7. Call `ingest_pdf_paths()`.
8. Mark bundle `imported` or `failed`.

Render must not call DocAI in this path. If a bundle lacks text sidecars, importer should fail the bundle rather than falling back to OCR.

## User Upload Requirement

Do not weaken or remove the existing user-facing upload paths:

- Authenticated `/upload` remains available for website users.
- `/upload/anonymous` remains available for public contributors.
- These paths can still use existing server-side extraction behavior for small one-off uploads.
- The prepared-ingest queue is admin-only and intended for scraped public bank material.

## Implementation Steps

1. Add `hoaware/prepared_ingest.py`
   - Bundle dataclasses/schema validation.
   - GCS path helpers.
   - Status load/claim/update helpers.
   - Sidecar parse helpers converting JSON to `PageContent`.

2. Add `scripts/prepare_bank_for_ingest.py`
   - Input: `--state`, optional `--county`, `--limit`, `--dry-run`, `--max-docai-cost-usd`.
   - Reads bank manifests.
   - Downloads PDFs.
   - Filters before OCR.
   - Runs local PyPDF/DocAI extraction.
   - Writes prepared bundles to GCS.
   - Writes local JSONL audit ledger.

3. Add admin importer in `api/main.py`
   - `POST /admin/ingest-ready-gcs`.
   - Admin auth via `JWT_SECRET`, same as existing admin routes.
   - Limit defaults small, e.g. `limit=1` or `limit=5`.
   - Does no OCR.

4. Tests
   - Bundle schema validation.
   - Claim status generation-precondition behavior with a fake/local adapter where practical.
   - Importer uses `pre_extracted_pages` and does not call `extract_pages`.
   - Existing `/upload` tests continue to pass unchanged.

5. Docs
   - Add `docs/bank-to-live-ingestion.md`.
   - Update `docs/agent-ingestion.md`:
     - `/upload` is still the user/one-off path.
     - GCS prepared ingest is the bulk bank-drain path.
     - Server-side bulk import remains discouraged; GCS prepared bundles are the approved durable queue.

## Operational Flow

For a completed state:

```bash
source .venv/bin/activate
set -a; source settings.env; set +a

python scripts/prepare_bank_for_ingest.py \
  --state KS \
  --bucket hoaproxy-bank \
  --ready-bucket hoaproxy-ingest-ready \
  --max-docai-cost-usd 25 \
  --dry-run

python scripts/prepare_bank_for_ingest.py \
  --state KS \
  --bank-bucket hoaproxy-bank \
  --prepared-bucket hoaproxy-ingest-ready \
  --max-docai-cost-usd 25
```

Then import in small batches:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $JWT_SECRET" \
  "https://hoaproxy.org/admin/ingest-ready-gcs?state=KS&limit=5"
```

Verify:

```bash
curl -sS -H "Authorization: Bearer $JWT_SECRET" \
  "https://hoaproxy.org/admin/zero-chunk-docs"
```

## Non-Goals

- Do not create a new public upload flow.
- Do not make Render run bulk DocAI OCR.
- Do not reintroduce `/admin/bulk-import`.
- Do not remove `/upload` or `/upload/anonymous`.
- Do not import raw bank manifests directly into SQLite without the ingestion pipeline.
