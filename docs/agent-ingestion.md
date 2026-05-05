# Agent-driven HOA ingestion

This is the canonical reference for one-off HOA additions and normal website uploads. An LLM agent (Claude Code or equivalent) discovers an HOA, fetches a polygon, classifies its public documents locally, and POSTs them to `/upload` with structured metadata. The server is a trusted-but-verifying executor.

Bulk scraped-bank ingestion uses a separate prepared GCS queue: the raw bank stays at `gs://hoaproxy-bank/v1/{STATE}/...`, a local/GCP worker writes OCR-ready bundles to `gs://hoaproxy-ingest-ready/v1/{STATE}/...`, and Render imports them through `POST /admin/ingest-ready-gcs`. See [`bank-to-live-ingestion.md`](bank-to-live-ingestion.md). Do not use `/upload` as a Render-side bulk OCR worker.

If you're a fresh session figuring out how to add a single HOA or handle a public contributor upload, read this doc top to bottom and you'll have the full contract. For the one-time cleanup of legacy data still left in the prod DB, see [`ops-cleanup.md`](ops-cleanup.md).

## Mental model

**Old model (deleted):** server is intelligent (classifier inside the pipeline, OCR cascade with fallbacks). Agent is dumb — it just fires `curl`. Per-corpus uploader scripts (`upload_alexandria_to_site.py`, `upload_trec_to_site.py`, etc.) shoveled bulk data through.

**One-off model:** agent is intelligent (it already classifies "this is meeting minutes, not a CC&R" the way a human would). Server is a routing layer that trusts the agent's verdicts and runs cheap defense-in-depth checks. One HOA per request.

**Bulk bank-drain model:** discovery agents preserve raw findings in GCS first. A prep worker filters and OCRs outside Render, writes prepared bundles with text sidecars, and the admin importer ingests only those prepared bundles. Missing sidecars fail; Render does not fall back to Document AI for this bulk path.

## The six-step loop

This is what every HOA addition looks like.

**1. Discover the HOA**
- Agent searches for the HOA name, its website, its management company.
- Polygon: query Nominatim for `place=neighbourhood`, then `osm_ids=W{id}` lookup for the GeoJSON. Cross-check the area against any documented home-count/acreage for sanity.

**2. Find candidate PDFs**
- HOA's own website (often `parkvillage.org` or `<name>hoa.org`).
- Management company portal (CASNC, Grandchester Meadows, Omega, etc.).
- County Register of Deeds (for the recorded Declaration if the HOA doesn't post it).
- Use `site:<domain> filetype:pdf` searches to find direct links.

**3. Precheck each PDF (free, no commitment)**

```bash
python scripts/hoa_precheck.py path/to/declaration.pdf --hoa "Park Village"
# or for a remote URL:
python scripts/hoa_precheck.py --url https://example.com/decl.pdf --hoa "Park Village"
```

Returns JSON with:
```json
{
  "ok": true,
  "page_count": 28,
  "text_extractable": false,
  "suggested_category": "ccr",
  "classification_method": "filename",
  "classification_confidence": 0.7,
  "is_valid_governing_doc": true,
  "is_pii_risk": false,
  "is_junk": false,
  "est_docai_pages": 28,
  "est_docai_cost_usd": 0.042,
  "recommendation": "upload"
}
```

Exit codes: `0` = upload, `1` = reject (PII or junk), `2` = review (uncertain), `3` = error.

The server-side equivalent is `POST /agent/precheck` (request body: `{url?, sha256?, filename?, hoa?}`); use it when the agent doesn't have local file access.

**4. POST `/upload` with parallel arrays**

```bash
curl -sS -X POST "https://hoaproxy.org/upload" \
  -H "Authorization: Bearer $JWT" \
  -F "hoa=Park Village" \
  -F "boundary_geojson=$(cat boundary.geojson)" \
  -F "website_url=https://parkvillage.org/" \
  -F "city=Cary" -F "state=NC" -F "postal_code=27519" \
  -F "files=@declaration.pdf" -F "files=@bylaws.pdf" -F "files=@pool_rules.pdf" \
  -F "categories=ccr"   -F "categories=bylaws" -F "categories=rules" \
  -F "text_extractable=false" -F "text_extractable=true" -F "text_extractable=true" \
  -F "source_urls=https://parkvillage.org/declaration.pdf" \
  -F "source_urls=https://parkvillage.org/bylaws.pdf" \
  -F "source_urls=https://parkvillage.org/pool_rules.pdf"
```

The arrays must be the same length as `files`. Validation rejects:
- length mismatch (400)
- unknown category (400)
- PII category (`membership_list`, `ballot`, `violation`) (400)
- daily DocAI budget would be exceeded (429) — see [Cost guards](#cost-guards)

The server returns 200 with `{hoa, saved_files, queued: true}` immediately and runs ingestion in a background task.

**5. Server-side ingest (asynchronous, single-threaded)**

For each PDF, the server reads the agent's `text_extractable` hint and routes:

| Hint | What runs | Cost |
|---|---|---|
| `true` | PyPDF only — never call OCR | $0 |
| `false` | DocAI on the whole document — skip PyPDF | ~$0.0015/page |
| omitted | PyPDF first, DocAI for blank pages only | $0 + DocAI for missing pages |

A `Semaphore(1)` serializes ingestions across the whole app. Then chunks → optional proxy-rules detection (one `gpt-4o-mini` call per doc that mentions "proxy") → OpenAI `text-embedding-3-small` → SQLite + sqlite-vec.

**6. Verify**

```bash
curl -sS "https://hoaproxy.org/hoas/Park%20Village/location" | python3 -m json.tool
curl -sS "https://hoaproxy.org/hoas/Park%20Village/documents" | python3 -m json.tool
```

`chunk_count > 0` per doc means it's indexed and searchable.

## API contract reference

### `POST /upload` (authenticated)

Multipart form. Auth: `Authorization: Bearer <user JWT>` (register a temp account via `POST /auth/register` if needed).

Form fields:

| Field | Type | Required | Notes |
|---|---|---|---|
| `hoa` | str | yes | HOA name (used as identity key) |
| `files` | UploadFile[] | yes (≥1) | Each ≤ 25 MB |
| `categories` | str[] | optional | One per file; must be in [VALID_CATEGORIES](#categories) or `unknown` |
| `text_extractable` | str[] | optional | One per file; `"true"`/`"false"` (or `"yes"`/`"no"`/`"1"`/`"0"`) |
| `source_urls` | str[] | optional | One per file; provenance, stored on the document row |
| `extracted_texts` | str[] | optional | One per file; JSON sidecar of pre-extracted page text (see [Offloading OCR](#offloading-ocr-to-the-agent)). Empty string `""` for files where the server should extract. |
| `boundary_geojson` | str (JSON) | optional | GeoJSON Polygon or MultiPolygon |
| `website_url` | str | optional | HOA's public site |
| `street`, `city`, `state`, `postal_code`, `country` | str | optional | Address — geocoded to lat/lon if missing and possible |
| `latitude`, `longitude` | float | optional | Override geocoding |
| `metadata_type` | str | optional | "hoa" / "condo" / etc. |

Response 200:
```json
{
  "hoa": "Park Village",
  "saved_files": ["declaration.pdf", "bylaws.pdf", "pool_rules.pdf"],
  "indexed": 0,
  "skipped": 0,
  "failed": 0,
  "queued": true,
  "location_saved": true
}
```

(`indexed`/`skipped`/`failed` are always 0 because ingestion is asynchronous; poll `/hoas/{name}/documents` for progress.)

### `POST /upload/anonymous`

Same form, plus required `email` field, no auth header. Rate-limited to **3 requests / hour / IP**. Sets `hoa_locations.source = "public_contributor"`.

### `POST /agent/precheck`

JSON body:
```json
{ "url": "https://...pdf", "sha256": null, "filename": null, "hoa": "Park Village" }
```
or
```json
{ "filename": "covenants.pdf", "hoa": "Park Village" }
```

Returns the same shape as the CLI (without `recommendation` and exit codes — caller infers from `is_valid_governing_doc` / `is_pii_risk`).

When `url` is provided the server downloads it (max 25 MB) and inspects it server-side (PyPDF + classifier). When only `filename` is provided, classification falls back to the filename regex.

`duplicate_of` is set when a document with the same SHA256 is already in the corpus, formatted as `"<HOA>/<relative_path>"`.

### `POST /admin/ingest-ready-gcs`

Admin-only bulk importer for prepared GCS bundles. Auth:
`Authorization: Bearer <JWT_SECRET>`.

```bash
curl -sS -X POST \
  "https://hoaproxy.org/admin/ingest-ready-gcs?state=KS&limit=5" \
  -H "Authorization: Bearer $JWT_SECRET"
```

This endpoint reads `ready` bundles from
`gs://hoaproxy-ingest-ready/v1/{STATE}/...`, claims them, downloads PDFs and
text sidecars, and calls `ingest_pdf_paths()` with `pre_extracted_pages`.
It never runs Render-side OCR. If `texts/{sha}.json` is missing or invalid, the
bundle is marked `failed`.

## Categories

Defined in `hoaware/doc_classifier.py`.

**VALID** (the agent should upload these):

| category | description |
|---|---|
| `ccr` | CC&Rs / Declaration of Covenants, Conditions & Restrictions |
| `bylaws` | Bylaws of the association |
| `articles` | Articles of Incorporation |
| `rules` | Rules & Regulations, Architectural Guidelines, Design Standards |
| `amendment` | Amendments or Supplements to any governing document |
| `resolution` | Board Resolutions, formal policy changes |
| `minutes` | Meeting Minutes, Newsletters, Annual Meeting summaries |
| `financial` | Budgets, Financial Statements, Reserve Studies |
| `insurance` | Certificates of Insurance, liability policies |

**REJECT — junk** (server accepts the upload but the agent should drop these client-side):

| category | description |
|---|---|
| `court` | Court filings, lawsuits, judgments, liens |
| `tax` | IRS Form 990, tax returns |
| `government` | City council agendas, planning docs, environmental reports |
| `real_estate` | MLS listings, market reports |
| `unrelated` | Anything else not HOA-related |

**REJECT — PII** (server refuses with HTTP 400; the agent must never upload these):

| category | description |
|---|---|
| `membership_list` | Member directories with names/addresses/phones |
| `ballot` | Filled ballots or proxy forms with personal info |
| `violation` | Violation notices naming specific owners |

## OCR provider

**Document AI is the sole OCR provider.** Tesseract has been removed from the runtime path.

Configure on the server:
- `HOA_ENABLE_DOCAI=1` (default in production)
- `HOA_DOCAI_PROJECT_ID`, `HOA_DOCAI_LOCATION` (default `us`), `HOA_DOCAI_PROCESSOR_ID`
- `GOOGLE_APPLICATION_CREDENTIALS` pointing at a GCP service-account JSON

If DocAI is not configured and the agent says `text_extractable=false`, those pages come back blank in the database. There is no fallback.

The DocAI request batches pages in chunks of `HOA_DOCAI_CHUNK_PAGES` (default 10) to fit Document AI's per-call page limit. When `text_extractable=null` (legacy), DocAI is invoked only on the specific page numbers PyPDF couldn't read — not the whole document. That's a key change from pre-PR-1 behavior.

There's a hard guard: documents with > 200 pages skip OCR entirely (`MAX_PAGES_FOR_OCR` in `pdf_utils.py`). Real governing docs don't exceed this; if a 1000-page archive sneaks through, it stays unembedded rather than burning $1.50.

### Offloading OCR to the agent

Server-side DocAI rendering is the dominant memory load on the API host (Render's 512 MB instance OOMs cycle as 502s during background ingestion). To skip it, run extraction locally and pass the result via `extracted_texts` — a parallel form-array, one entry per file:

- `""` — server extracts as usual (via `extract_pages` routing on `text_extractable`).
- A JSON object — server skips `extract_pages` for that file and uses the supplied pages directly:

```json
{
  "pages": [
    {"number": 1, "text": "..."},
    {"number": 2, "text": "..."}
  ],
  "docai_pages": 12
}
```

`docai_pages` is the page count the agent ran through DocAI locally. The server logs it to `api_usage_log` *before* the per-upload budget check, so the rolling 24h DocAI cap covers local + server spend uniformly. Set it to `0` (or omit) when extraction was PyPDF-only.

The agent can use `hoaware.pdf_utils.extract_pages` directly — same routing logic, same DocAI client, same `text_extractable` semantics — just from the local machine. Cap is 10 MB per sidecar JSON.

## Cost guards

Three layers:

**1. Per-upload preflight (synchronous, returns 429)**

`api/main.py:_check_daily_docai_budget` runs at the top of `/upload`. It sums the page count of files the agent flagged `text_extractable=false`, multiplies by `$0.0015/page`, adds the rolling-24h DocAI spend from `api_usage_log`, and refuses if the projection exceeds `DAILY_DOCAI_BUDGET_USD` (env, default `$20`).

Error body:
```json
{"detail": "Daily DocAI budget would be exceeded: $X spent in last 24h + $Y projected > $20.00 cap. Either set text_extractable=true for digital PDFs, raise DAILY_DOCAI_BUDGET_USD, or wait for the rolling window."}
```

**2. Daily cost-alert endpoint (for cron)**

```bash
GET /admin/costs/docai-alert?threshold_usd=10&hours=24&notify=true
Header: Authorization: Bearer $JWT_SECRET
```

Returns:
```json
{
  "service": "docai",
  "hours": 24,
  "spend_usd": 0.42,
  "threshold_usd": 10.0,
  "over_threshold": false,
  "daily_upload_cap_usd": 20.0,
  "notified": false
}
```

When `over_threshold=true` and `notify=true`, sends email to `COST_REPORT_EMAIL` via the existing email service. Wire this to cron-job.org (or any cron) for a daily check.

**3. Existing cost dashboard**

`/admin/costs` (Bearer `JWT_SECRET`) returns the all-time / per-month metered spend across all logged services. `hoaware/cost_tracker.py:log_docai_usage` is called once per `ProcessDocument` call.

## What hidden documents look like

`documents.hidden_reason` is set when a doc is junk/PII (e.g. `"pii:membership_list"`, `"junk:court"`). Both the search/QA path (`db.vector_search`) and the document listing (`list_documents_for_hoa`) filter `WHERE d.hidden_reason IS NULL` by default. Pass `include_hidden=True` to see them.

A document never becomes hidden as a side effect of `/upload` — the validation refuses PII categories outright (HTTP 400). Hidden docs only exist from PR-2's backfill of the historical audit report (with `--apply-hidden-reason` / `?apply_hidden_reason=true`).

## Worked example: Park Village in Cary, NC

**1. Discover.** Search "Park Village HOA Cary NC" → land on `parkvillage.org`. Hit Nominatim:

```bash
curl -sS "https://nominatim.openstreetmap.org/search.php?q=Park+Village+Cary+NC&format=jsonv2&polygon_geojson=1" \
  -H "User-Agent: hoaproxy-agent/1.0 (you@example.com)"
```

Pull the `geojson` field from the top result (way 204873392, 52-point polygon, ~176 acres → matches the documented "~200 acres / 605 homes").

**2. Find PDFs.** `site:parkvillage.org filetype:pdf` returns the Declaration, ACC application (architectural standards), Pool Rules, plus a "mystery" PDF that turns out to be 2014 board meeting minutes (skip).

**3. Precheck.** Download each PDF, run:
```bash
python scripts/hoa_precheck.py declaration.pdf --hoa "Park Village" --human
#   file:        declaration.pdf
#   text:        scanned (needs OCR)
#   category:    ccr (via filename, conf=0.7)
#   est OCR:     28 pages → $0.0420
#   recommend:   UPLOAD
```

**4. Upload.** Authenticated POST to `/upload` with all three keepers, parallel arrays giving each a category and `text_extractable` (Declaration is image-only → `false`; ACC and Pool Rules are digital → `true`). Server returns 200 with `queued: true`.

**5. Wait + verify.**
```bash
curl -sS https://hoaproxy.org/hoas/Park%20Village/documents
# After ~30 seconds: chunk_count populated for all three
```

**6. Done.** The HOA is queryable at `/search` and via QA endpoints. Polygon is on the map at `/`.

## Anti-patterns (don't do these)

- **Don't write a batch-import script.** The per-corpus uploaders (`upload_alexandria_to_site.py`, `upload_trec_to_site.py`, the queue runner `scripts/ingest.py`) were deleted on purpose. New HOAs go in one at a time via `/upload`. If you find yourself wanting a queue, you're working against the design.
- **Don't call DocAI directly from a script.** The pipeline already does it correctly with cost logging and chunking. Use `/upload` with `text_extractable=false`.
- **Don't upload PII-flagged categories.** Server will 400. Don't try to bypass with a different category — you're poisoning the corpus.
- **Don't ignore the 25 MB per-file cap.** Split or skip — split usually means it's an entire HOA archive that should never have been one PDF anyway.
- **Don't use `/admin/bulk-import`.** It was removed in PR-5. The line of code where it used to live now reads "removed in PR-5".

## File map

| File | Role |
|---|---|
| `api/main.py` | `/upload`, `/upload/anonymous`, `/agent/precheck`, all admin routes |
| `hoaware/doc_classifier.py` | Categories, regex/filename/vision classifiers |
| `hoaware/pdf_utils.py` | `extract_pages` — the three-mode router on `text_extractable` |
| `hoaware/docai.py` | `extract_with_document_ai` (now supports targeted page subsets) |
| `hoaware/ingest.py` | `_ingest_pdf` + `ingest_pdf_paths` — the background pipeline |
| `hoaware/cost_tracker.py` | `log_docai_usage`, `log_embedding_usage`, pricing constants |
| `scripts/hoa_precheck.py` | Per-PDF precheck CLI |
| `scripts/score_ocr_quality.py` | Identifies docs whose existing OCR is garbled |
| `scripts/reocr_with_docai.py` | Replaces bad-OCR chunks with DocAI output (cost-capped) |
| `scripts/backfill_categories.py` | Populates `documents.category` from the audit report |
| `scripts/cleanup_legacy_db.py` | One-shot DB cleanup of bulk-importer accounts, orphans, source values |

## See also

- [`ops-cleanup.md`](ops-cleanup.md) — the one-time prod migration steps still to run after PR-1..6 deployed.
- `CLAUDE.md` — short pointer + project-wide conventions.
- Commit messages for each PR (`5b6d03d` through `67447bc`) — the design rationale for each change.
