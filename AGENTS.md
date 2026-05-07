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
# Python 3.10+; .venv lives in project root
python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest httpx  # test deps, not in requirements.txt
```

Settings are loaded from `settings.env` (gitignored). Copy `settings.env.example` to get started:
```bash
cp settings.env.example settings.env
# Edit settings.env with your API keys
```

Key values:
- `OPENAI_API_KEY` — secret, get from OpenAI dashboard
- `JWT_SECRET` — secret, use a strong random string in production
- `HOA_DB_PATH=data/hoa_index.db`
- `HOA_DOCS_ROOT=hoa_docs`
- `QDRANT_URL=http://localhost:6333` (local; no API key needed locally)
- `HOA_CHUNK_CHAR_LIMIT=1800`, `HOA_CHUNK_OVERLAP=200`

**Google Document AI (sole OCR provider):**
- `HOA_ENABLE_DOCAI=1` (default in production) + configure `HOA_DOCAI_PROJECT_ID`, `HOA_DOCAI_LOCATION`, `HOA_DOCAI_PROCESSOR_ID`
- `GOOGLE_APPLICATION_CREDENTIALS` — path to your GCP service account key file (gitignored)
- Tesseract is **not** in the runtime path. See `CLAUDE.md` for the agent-driven ingestion contract.

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

## State Scraping

When starting a fresh state-scraping session (covering one or more US states/DC), read `docs/multi-state-ingestion-playbook.md` first. It is the canonical, tier-stratified playbook (Tier 0 tiny → Tier 4 huge), supersedes the prior small-state plan / discovery playbook / bank-to-live plan / GCS prepared plan / prompt template, and includes:

- the per-tier run shape (parallel autonomous batches for tiny states; phased operator-supervised for larger ones)
- the `is_dirty()` name-quality gate at the bank, now backed by `hoaware/name_utils.py`
- the mandatory Phase 10 retrospective at `state_scrapers/{state}/notes/retrospective.md` (see GA, RI, TN exemplars)

Use `state_scrapers/_template/` as the runner skeleton. Copy it to `state_scrapers/{state}/`, replace the six placeholder constants in `scripts/run_state_ingestion.py`, and consult Appendix D of the playbook for the recommended discovery mode and DocAI budget for the target state.

## Proxy Voting System
See `docs/proxy-voting-plan.md` for full details.

**Key patterns:**
- DB startup migration: `lifespan` handler in `api/main.py` runs `db.SCHEMA` + expiry sweep on boot
- Rate limiter: in-memory per-IP, `_check_rate_limit(request, limit=N)` — skips `testclient` host
- Health check (`/healthz`): verifies all required tables exist, returns 503 if missing
- E-sign: click-to-sign MVP
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
- All 51 jurisdictions now have official source URLs in the registry (OK, PA, SD, WY were migrated from dead aggregators to official state sources)

## Data Directories (not in git)
- `data/` — SQLite DBs, Qdrant local store
- `hoa_docs/` — HOA document library (uploaded PDFs)
- `legal_corpus/` — Raw/normalized law texts
- `settings.env` — Secrets

## Working Style
- Proceed autonomously — do not ask clarifying questions unless truly blocked
- Commit after each milestone with a descriptive message
- Write tests as you go; fix failures before moving on
- This is a real product, not a prototype — write clean, production-quality code
- Do not over-engineer; keep solutions focused on what's asked
- Security: load secrets from `settings.env` via `os.environ`, never hardcode or echo in commands/logs
