# HOA Document Ingestion MVP

This repo now includes a Python CLI that ingests HOA PDF documents into:

- **SQLite** – stores HOAs, documents, chunk metadata, and the Qdrant point ids.
- **Qdrant** – holds semantic vectors per HOA (one collection per association).

The CLI is powered by Typer and uses OpenAI `text-embedding-3-small` for embeddings.

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
| `HOA_ENABLE_OCR` | `1` to OCR blank pages with Tesseract | `1` |
| `HOA_OCR_DPI` | DPI used when rasterizing PDFs for OCR | `300` |
| `HOA_ENABLE_DOCAI` | `1` to use Google Document AI for OCR | `0` |
| `HOA_DOCAI_PROJECT_ID` | GCP project hosting the processor | – |
| `HOA_DOCAI_LOCATION` | Document AI location/region | `us` |
| `HOA_DOCAI_PROCESSOR_ID` | Processor ID from Document AI | – |
| `HOA_DOCAI_ENDPOINT` | (Optional) Custom API endpoint | computed |
| `HOA_DOCAI_CHUNK_PAGES` | Split PDFs into chunks (default 10 pages) | `10` |

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

- `GET /healthz` – health check
- `GET /hoas` – list HOA folders
- `POST /qa` – body `{ "hoa": "Park Grove", "question": "...", "k": 6, "model": "gpt-4o-mini" }`

Run locally:

```bash
uvicorn api.main:app --reload
```

Run with Docker Compose (includes Qdrant):

```bash
docker compose up --build
```

Defaults expect docs in `casnc_hoa_docs/` and `settings.env` containing your keys (OpenAI, optional Document AI). Override with `HOA_DOCS_ROOT` / `HOA_DB_PATH` as needed.

Chunks are batched, embedded, and upserted into the Qdrant collection `hoa_<slug>`. SQLite retains chunk text plus the Qdrant point ids so we can refresh individual documents when files change.

## Next Steps

- Add OCR fallback (Tesseract or cloud OCR) for image-only PDFs.
- Automate ingestion for all HOAs, plus change detection/watchers.
- Expose a thin API that wraps semantic search + LLM responses per HOA.
