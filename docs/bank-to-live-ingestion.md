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
- `--prepared-bucket hoaproxy-ingest-ready` overrides the queue bucket.

The worker filters before OCR. PII, junk, page-cap violations, exact duplicates,
and unsupported categories are rejected without OCR spend.

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
