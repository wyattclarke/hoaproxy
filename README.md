# HOAware "Understand" MVP

This repo now includes a backend + frontend for the "Understand" phase:

- Residents upload HOA PDF documents per community.
- Documents are OCR'd/chunked and indexed into semantic search.
- Residents run grounded search and ask an LLM questions with citations.

Data is stored in:

- **SQLite** – stores HOAs, documents, chunk metadata, and the Qdrant point ids.
- **Qdrant** – holds semantic vectors per HOA (one collection per association).

The ingestion/retrieval pipeline uses OpenAI `text-embedding-3-small` for embeddings.

## Setup

1. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Provide configuration (environment variables or `.env`):

| Variable | Description | Default |
| --- | --- | --- |
| `OPENAI_API_KEY` | Required for embeddings | – |
| `HOA_DOCS_ROOT` | Folder with HOA subdirectories | `casnc_hoa_docs` |
| `HOA_DB_PATH` | SQLite DB path | `data/hoa_index.db` |
| `QDRANT_URL` | Qdrant endpoint | `http://localhost:6333` |
| `QDRANT_API_KEY` | Optional auth token | – |
| `HOA_QDRANT_LOCAL_PATH` | Embedded Qdrant path used as fallback when `QDRANT_URL` is unavailable | `data/qdrant_local` |
| `HOA_ENABLE_OCR` | `1` to OCR blank pages with Tesseract | `1` |
| `HOA_OCR_DPI` | DPI used when rasterizing PDFs for OCR | `300` |
| `HOA_ENABLE_DOCAI` | `1` to use Google Document AI for OCR | `0` |
| `HOA_DOCAI_PROJECT_ID` | GCP project hosting the processor | – |
| `HOA_DOCAI_LOCATION` | Document AI location/region | `us` |
| `HOA_DOCAI_PROCESSOR_ID` | Processor ID from Document AI | – |
| `HOA_DOCAI_ENDPOINT` | (Optional) Custom API endpoint | computed |
| `HOA_DOCAI_CHUNK_PAGES` | Split PDFs into chunks (default 10 pages) | `10` |
| `HOA_LEGAL_SOURCE_MAP_PATH` | Jurisdiction law source map JSON | `data/legal/source_map.json` |
| `HOA_LEGAL_CORPUS_ROOT` | Legal corpus snapshots + metadata root | `legal_corpus` |

The app auto-loads `settings.env` (and `.env` if present), so you can run `uvicorn` without manually exporting variables.

### OCR prerequisites

OCR is on by default and requires:

- [Tesseract OCR](https://tesseract-ocr.github.io/) – e.g. `brew install tesseract`.
- Poppler utilities for `pdf2image` – e.g. `brew install poppler`.

Set `HOA_ENABLE_OCR=0` if you want to skip OCR for now.

### Google Cloud Document AI (recommended for scans)

For higher-fidelity OCR (especially the legacy legal docs), you can enable [Document AI](https://cloud.google.com/document-ai):

1. In GCP, create or reuse a Document AI **Processor** (the “OCR” or “Form Parser” model works well).
2. Create a service account with the `Document AI Editor` role and download its JSON key.
3. Export:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account.json"
   export HOA_ENABLE_DOCAI=1
   export HOA_DOCAI_PROJECT_ID="your-project-id"
   export HOA_DOCAI_LOCATION="us"          # or us-latency, eu, etc.
   export HOA_DOCAI_PROCESSOR_ID="1234567890abcdef"
   ```
   (Optional) override `HOA_DOCAI_ENDPOINT` if you need a private endpoint.

When `HOA_ENABLE_DOCAI=1`, ingestion asks Document AI for text on pages that PyPDF couldn’t read; Tesseract remains a fallback if Document AI is disabled or still returns blanks.

3. Ensure Qdrant is running locally (Docker example):

```bash
docker run -p 6333:6333 -p 6334:6334 -d --name qdrant qdrant/qdrant
```

If Qdrant is not reachable, the app automatically falls back to embedded local storage at `HOA_QDRANT_LOCAL_PATH`.

## CLI Usage

List available HOAs:

```bash
python -m hoaware.cli hoas
```

Ingest a single HOA (e.g., “Park Grove”):

```bash
python -m hoaware.cli ingest "Park Grove"
```

Run a semantic search confined to an HOA:

```bash
python -m hoaware.cli search "What are the ARC requirements?" --hoa "Park Grove" -k 5
```

Ask an LLM to answer using retrieved context:

```bash
python -m hoaware.cli qa "Do I need ARC approval to add a fence?" --hoa "Park Grove" -k 6 --model gpt-4o-mini
```

## API server

A FastAPI service (`api/main.py`) exposes:

- `GET /` – single-page web UI
- `GET /healthz` – health check
- `GET /hoas` – list HOA workspaces
- `GET /hoas/{hoa_name}/documents` – list indexed docs and metadata for an HOA
- `POST /upload` – multipart upload (`hoa`, `files[]`) and immediate ingestion
- `POST /search` – semantic chunk search (`hoa`, `query`, `k`)
- `POST /qa` – body `{ "hoa": "Park Grove", "question": "...", "k": 6, "model": "gpt-4o-mini" }`
- `GET /law/jurisdictions` – legal corpus jurisdiction coverage summary
- `GET /law/{jurisdiction}/profiles` – normalized legal profiles (optional `community_type`, `entity_form`)
- `POST /law/qa` – deterministic legal QA for a jurisdiction and question family
- `GET /law/{jurisdiction}/proxy-electronic` – status/evidence for electronic proxy assignment + e-signature acceptance
- `GET /law/proxy-electronic/summary` – per-jurisdiction summary for electronic proxy statuses

Run locally:

```bash
uvicorn api.main:app --reload
```

Open the UI at `http://127.0.0.1:8000`.

Run with Docker Compose (includes Qdrant):

```bash
docker compose up --build
```

Defaults expect docs in `casnc_hoa_docs/` and `settings.env` containing your keys (OpenAI, optional Document AI). Override with `HOA_DOCS_ROOT` / `HOA_DB_PATH` as needed.

Chunks are batched, embedded, and upserted into the Qdrant collection `hoa_<slug>`. SQLite retains chunk text plus the Qdrant point ids so we can refresh individual documents when files change.

## Hosting (Render + GoDaddy)

Recommended production setup:

- Deploy this repo to a Render **Web Service** (Docker runtime).
- Attach a Render persistent disk for docs + SQLite + local Qdrant fallback data.
- Point `app.wyattclarke.com` (GoDaddy DNS) to the Render service.

This repo includes `render.yaml` for a starter blueprint.

### 1) Create service in Render

1. Push this repo to GitHub.
2. In Render, create a new service from the repo.
3. Render should detect `render.yaml`; use that configuration.
4. Create a secret file in Render named `gcp-service-account.json` (if using Document AI).

### 2) Required environment settings

Set these in Render (the blueprint already defines most of them):

- `OPENAI_API_KEY` (required)
- `HOA_DOCS_ROOT=/var/data/casnc_hoa_docs`
- `HOA_DB_PATH=/var/data/hoa_index.db`
- `HOA_QDRANT_LOCAL_PATH=/var/data/qdrant_local`
- `QDRANT_URL=` (blank to use embedded local Qdrant)
- `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/gcp-service-account.json` (if using Document AI)
- `HOA_ENABLE_DOCAI=1` plus Document AI project/location/processor values (if using Document AI)

### 3) GoDaddy DNS

In Render, add custom domain `app.wyattclarke.com`, then copy the DNS values Render provides.
In GoDaddy DNS for `wyattclarke.com`, create the exact records Render asks for (usually CNAME or A record).

After DNS propagates, Render will provision TLS automatically.

### 4) Verify deployment

- Open `https://app.wyattclarke.com/healthz` and confirm `{"status":"ok"}`.
- Open `https://app.wyattclarke.com/` and test upload/search/QA.

## Working in Codex Web

To work in Codex Web against this repo:

1. Push this repository to GitHub and open it in Codex Web.
2. Create a branch for each task using the prefix `codex/` (for example: `codex/upload-error-handling`).
3. Use `settings.env` or `.env` for local config; do not commit secrets.
4. Run the API from the repo root:
   ```bash
   uvicorn api.main:app --reload
   ```
5. For full local stack validation (API + Qdrant), run:
   ```bash
   docker compose up --build
   ```

Recommended workflow in Codex Web:

- Start tasks with a quick repo scan (`README.md`, `api/main.py`, `hoaware/`).
- Keep commits scoped (one feature/fix per commit).
- Run a lightweight check before pushing:
  ```bash
  python -m compileall -q api hoaware
  ```
- Open a PR from your `codex/*` branch into `master`.

## State Law Corpus Pipeline

The repo now includes a jurisdiction legal-corpus workflow under `scripts/legal/`:

```bash
python scripts/legal/build_source_map.py
python scripts/legal/fetch_law_texts.py --state NC
python scripts/legal/normalize_law_texts.py --state NC
python scripts/legal/extract_rules.py --state NC
python scripts/legal/assemble_profiles.py --state NC
python scripts/legal/validate_corpus.py
```

One-command runner:

```bash
python scripts/legal/run_pipeline.py --state NC
python scripts/legal/run_pipeline.py --state NC --refresh-fetch --force-normalize
python scripts/legal/run_pipeline.py --rebuild-source-map --rebuild-proxy-matrix --limit 500
```

Pipeline behavior:
- Fetch and normalize are idempotent by default.
- `run_pipeline.py` will not rebuild `data/legal/source_map.json` unless `--rebuild-source-map` is set.
- `run_pipeline.py` will build `data/legal/proxy_requirement_matrix.json` if missing (or force with `--rebuild-proxy-matrix`).
- Use `--skip-validate` to skip validation/progress-index refresh.

CLI shortcuts:

```bash
python -m hoaware.cli law-jurisdictions
python -m hoaware.cli law-profiles --jurisdiction NC
python -m hoaware.cli law-qa NC --community-type hoa --question-family records_and_sharing
python -m hoaware.cli law-pipeline --state NC
python -m hoaware.cli law-proxy-electronic FL --community-type hoa
python -m hoaware.cli law-proxy-electronic-summary --community-type hoa
```

## Next Steps

- Add auth + tenant boundaries (resident accounts and HOA-level permissions).
- Add async ingestion jobs with background worker + progress tracking.
- Add source-link UX (jump from answer citation to exact document chunk/page).
