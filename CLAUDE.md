# HOAware — Claude Code Instructions

## Project Overview
HOAware is a semantic search / Q&A platform for HOA documents, with a proxy voting system and legal corpus. Stack: FastAPI, SQLite (WAL mode), Qdrant vector DB, OpenAI embeddings.

**Key files:**
- `api/main.py` — FastAPI app (all routes)
- `hoaware/db.py` — SQLite schema + all CRUD functions
- `hoaware/auth.py` — JWT auth, password hashing
- `hoaware/config.py` — Settings loaded from `settings.env` or env vars
- `hoaware/law.py` — Query API for state HOA law rules
- `hoaware/proxy_templates.py` — Jinja2 proxy form template engine
- `hoaware/esign.py` — E-signature abstraction (click-to-sign MVP)
- `hoaware/email_service.py` — Email delivery stub (logs only)
- `api/static/js/auth.js` — Shared frontend auth (JWT in localStorage, Bearer injection)

## Environment Setup
```bash
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Settings are loaded from `settings.env` (gitignored). Key values:
- `OPENAI_API_KEY` — secret, get from OpenAI dashboard
- `JWT_SECRET` — secret, use a strong random string in production
- `HOA_DB_PATH=data/hoa_index.db`
- `HOA_DOCS_ROOT=casnc_hoa_docs`
- `QDRANT_URL=http://localhost:6333` (local; no API key needed locally)
- `HOA_ENABLE_OCR=1`, `HOA_OCR_DPI=300`
- `HOA_CHUNK_CHAR_LIMIT=1800`, `HOA_CHUNK_OVERLAP=200`

**Google Document AI (enabled):**
- `HOA_ENABLE_DOCAI=1`
- `HOA_DOCAI_PROJECT_ID=hoaware`
- `HOA_DOCAI_LOCATION=us`
- `HOA_DOCAI_PROCESSOR_ID=2972d086333484a3`
- `GOOGLE_APPLICATION_CREDENTIALS=hoaware-598872615131.json` — GCP service account key file (gitignored, transfer separately)

**Render deployment:**
- Service ID: `srv-d62kms68alac738h67b0`
- `RENDER_API_KEY` — secret, get from Render dashboard

## Running & Testing
```bash
# Run API server
uvicorn api.main:app --reload

# Run all tests
python -m pytest tests/ -q

# Legal corpus pipeline
python3 scripts/legal/run_pipeline.py --skip-validate --include-aggregators
```

## Frontend Style
Vanilla HTML/CSS/JS — no build step, no framework. Match existing style:
- Fonts: Manrope (body), Space Grotesk (headings)
- Colors: `--accent: #1662f3`, `--bg: #eef5ff`, `--ink: #12233a`
- Auth: always load `/static/js/auth.js`, use `Auth.renderNav()`, `Auth.requireAuth()`, `Auth.fetchJson()`

## Proxy Voting System (in progress)
Milestones 1–4 complete and committed. Milestone 5 (Frontend Polish) is in progress.
See `docs/implementation-plan.md` for the full 8-milestone plan.

**Proxy lifecycle:** `draft → signed → delivered → acknowledged / revoked / expired`

**Route ordering rule:** In `api/main.py`, always define specific routes (`/proxies/mine`) before parameterized routes (`/proxies/{proxy_id}`).

## Legal Corpus Pipeline
ETL pipeline in `scripts/legal/`:
`build_source_map.py` → `fetch_law_texts.py` → `normalize_law_texts.py` → `extract_rules.py` → `assemble_profiles.py` → `validate_corpus.py`

- Rules stored in `legal_corpus/metadata/extracted_rules.jsonl` (not SQLite)
- Source registry: `data/legal/state_source_registry.json`
- 47/51 jurisdictions have assembled profiles; OK, PA, SD, WY are inaccessible via static HTML

## Data Directories (not in git — transfer separately)
- `data/` — SQLite DBs, Qdrant local store
- `casnc_hoa_docs/` — Main HOA document library (~1.9 GB)
- `scraped_hoa_docs/` — Uploaded HOA PDFs
- `legal_corpus/` — Raw/normalized law texts
- `settings.env` — Secrets

## Working Style
- Proceed autonomously — do not ask clarifying questions unless truly blocked
- Commit after each milestone with a descriptive message
- Write tests as you go; fix failures before moving on
- This is a real product, not a prototype — write clean, production-quality code
- Do not over-engineer; keep solutions focused on what's asked
