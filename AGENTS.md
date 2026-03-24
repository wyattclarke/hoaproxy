# HOAproxy — Codex Instructions

## Project Overview
HOAproxy is a semantic search / Q&A platform for HOA documents, with a proxy voting system and legal corpus. Stack: FastAPI, SQLite (WAL mode), Qdrant vector DB, OpenAI embeddings.

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
# Python 3.10 installed via pyenv; .venv lives in project root
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest httpx  # test deps, not in requirements.txt
```

Python 3.10.12 is installed at `/Users/ngoshaliclarke/.pyenv/versions/3.10.12/`.
If .venv is missing or wrong version: `rm -rf .venv && /Users/ngoshaliclarke/.pyenv/versions/3.10.12/bin/python3.10 -m venv .venv`

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
- `RENDER_API_KEY` and `RENDER_SERVICE_ID` are in `settings.env`
- When calling Render API from scripts/shell: `set -a && source settings.env && set +a` then use `$RENDER_API_KEY` — never hardcode secrets in commands
- Render env vars API is PUT-only (replaces all); use the Python snippet pattern in this session to upsert a single key without echoing others
- `JWT_SECRET` is set on Render (added Mar 2026)

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

## Proxy Voting System — ALL 8 MILESTONES COMPLETE
See `docs/proxy-voting-plan.md` for full details. 129 tests, all passing.

**Key patterns:**
- DB startup migration: `lifespan` handler in `api/main.py` runs `db.SCHEMA` + expiry sweep on boot
- Rate limiter: in-memory per-IP, `_check_rate_limit(request, limit=N)` — skips `testclient` host
- Health check (`/healthz`): verifies all required tables exist, returns 503 if missing
- E-sign: Documenso API when `DOCUMENSO_API_KEY` set; click-to-sign fallback otherwise
- Email: `EMAIL_PROVIDER=stub|resend|smtp`; defaults to stub (logs only)
- Data retention: `PROXY_RETENTION_DAYS=90`; expiry sweep runs on startup

**Test isolation pattern:** module-level temp DB + `os.environ["HOA_DB_PATH"]`. FK delete order: `proxy_audit → proxy_assignments → delegates → membership_claims → sessions → users`.

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
- Security: load secrets from `settings.env` via `os.environ`, never hardcode or echo in commands/logs
