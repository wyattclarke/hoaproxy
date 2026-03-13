# HOAproxy

HOAproxy is a semantic search and proxy voting coordination platform for homeowner associations. Residents can upload HOA governing documents, ask grounded Q&A questions with citations, browse state law summaries for HOA records and proxy voting rules, and coordinate proxy assignments with e-signature support for any HOA meeting.

## Features

- **HOA Document Search** — upload PDFs, index with OpenAI embeddings, run semantic search with GPT-4o-mini Q&A and citations
- **Proxy Voting** — create, sign, deliver, and revoke proxy assignments; delegate registration; e-signature via Documenso or click-to-sign fallback
- **Legal Corpus** — ETL pipeline for 50-state HOA/condo law covering records access, proxy voting, and records sharing limits
- **HOA Search** — universal address + name lookup with boundary polygon matching
- **Participation Tracking** — record meeting attendance and quorum data per HOA

## Quick Start

```bash
git clone https://github.com/yourorg/hoaware.git
cd hoaware

# Python 3.10+ required
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `settings.env.example` to `settings.env` and fill in your values (see table below), then:

```bash
uvicorn api.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Environment Variables

| Variable | Description | Required |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI API key for embeddings and Q&A | Required for search/QA |
| `JWT_SECRET` | Secret key for JWT auth tokens | Required (set a strong random value in production) |
| `HOA_DOCS_ROOT` | Directory for HOA PDF uploads | Optional (default: `casnc_hoa_docs`) |
| `HOA_DB_PATH` | SQLite database path | Optional (default: `data/hoa_index.db`) |
| `QDRANT_URL` | Qdrant vector DB endpoint | Optional (default: embedded local) |
| `QDRANT_API_KEY` | Qdrant auth token | Optional |
| `HOA_QDRANT_LOCAL_PATH` | Embedded Qdrant fallback path | Optional (default: `data/qdrant_local`) |
| `HOA_ENABLE_OCR` | Enable Tesseract OCR for scanned PDFs | Optional (default: `1`) |
| `HOA_ENABLE_DOCAI` | Enable Google Document AI OCR | Optional (default: `0`) |
| `HOA_DOCAI_PROJECT_ID` | GCP project for Document AI | Required if DocAI enabled |
| `HOA_DOCAI_LOCATION` | Document AI region | Optional (default: `us`) |
| `HOA_DOCAI_PROCESSOR_ID` | Document AI processor ID | Required if DocAI enabled |
| `DOCUMENSO_API_URL` | Documenso instance URL | Optional (default: `https://app.documenso.com`) |
| `DOCUMENSO_API_KEY` | Documenso API key | Optional (enables e-signature) |
| `DOCUMENSO_WEBHOOK_SECRET` | Documenso webhook HMAC secret | Optional |
| `EMAIL_PROVIDER` | Email backend: `stub`, `resend`, or `smtp` | Optional (default: `stub`) |
| `EMAIL_FROM` | From address for transactional email | Optional |
| `RESEND_API_KEY` | Resend API key | Required if `EMAIL_PROVIDER=resend` |
| `SMTP_HOST` | SMTP host | Required if `EMAIL_PROVIDER=smtp` |
| `SMTP_PORT` | SMTP port | Optional (default: `587`) |
| `SMTP_USER` | SMTP username | Optional |
| `SMTP_PASSWORD` | SMTP password | Optional |
| `PROXY_RETENTION_DAYS` | Days to retain proxy records after expiry before soft-delete | Optional (default: `90`) |
| `JWT_EXPIRY_DAYS` | JWT token expiry in days | Optional (default: `30`) |

## Running Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

All 117+ tests should pass. Tests use an in-memory SQLite database; no Qdrant or OpenAI key required.

## Deployment

### Render (recommended)

This repo includes `render.yaml` for a Docker-based Render web service with a persistent disk.

1. Push to GitHub and connect to Render
2. Render auto-detects `render.yaml`; review env vars and add secrets (`OPENAI_API_KEY`, `JWT_SECRET`)
3. Attach a persistent disk at `/var/data` (already configured in `render.yaml`)
4. If using Document AI, upload your GCP service account JSON as a secret file at `/etc/secrets/gcp-service-account.json`

### Docker Compose (local full stack)

```bash
docker compose up --build
```

This starts the API and a local Qdrant instance. Configure `settings.env` with your keys.

## State Law Corpus Pipeline

The legal corpus pipeline lives in `scripts/legal/`. To rebuild for a specific state:

```bash
python3 scripts/legal/run_pipeline.py --state NC --skip-validate --include-aggregators
```

Full rebuild:

```bash
python3 scripts/legal/run_pipeline.py --skip-validate --include-aggregators
```

## Contributing

1. Fork the repository and create a feature branch: `git checkout -b feature/my-change`
2. Make your changes and add tests
3. Run `pytest tests/ -q` — all tests must pass
4. Open a pull request against `master` with a clear description of the change

## License

MIT License. See `LICENSE` file for details.

HOAproxy is an informational tool only. Nothing on this platform constitutes legal advice.
