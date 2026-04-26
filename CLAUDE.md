# HOAproxy — Claude Code Instructions

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
- Tesseract is **not** in the runtime path. Scanned pages with no DocAI configured come back blank.

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

## Document Ingestion (agent-driven)

The canonical ingestion path is **an LLM agent that finds an HOA, fetches a polygon, fetches its public docs, classifies each one locally, and uploads with full metadata**. The server is a trusted-but-verifying executor — it routes on the agent's hints, doesn't re-derive judgments.

**Agent loop:**
1. Discover HOA name + polygon (OSM `place=neighbourhood` via Nominatim, or Town/County GIS)
2. Fetch candidate PDFs (HOA website, management portal, county Register of Deeds)
3. For each PDF, run `python scripts/hoa_precheck.py` — JSON output with `suggested_category`, `text_extractable`, `recommendation` (`upload`/`reject`/`review`), and est DocAI cost
4. Skip PDFs flagged `reject` (junk or PII risk)
5. POST to `/upload` (authenticated) or `/upload/anonymous` with parallel form arrays:
   - `files`, `categories`, `text_extractable`, `source_urls` (one per file)
6. The server uses the agent's `text_extractable` hint to route extraction:
   - `True`  → PyPDF only, no OCR
   - `False` → DocAI directly
   - omitted → PyPDF first, DocAI for blank pages only

**Key files:**
- `hoaware/doc_classifier.py` — VALID_CATEGORIES (ccr, bylaws, articles, rules, amendment, resolution, minutes, financial, insurance), REJECT_PII (membership_list, ballot, violation), REJECT_JUNK (court, tax, government, real_estate, unrelated)
- `hoaware/pdf_utils.py:extract_pages` — three-mode router on the agent's hint
- `hoaware/ingest.py:_ingest_pdf` — accepts `category`, `text_extractable`, `source_url`
- `api/main.py /agent/precheck` — server-side equivalent of the precheck CLI

**Categories the agent should prefer for upload:** ccr, bylaws, articles, amendment, rules. Optionally minutes/financial/resolution/insurance. **Never** upload `membership_list`, `ballot`, `violation` (PII).

**Don't reach for:** there is no batch-import script, no per-corpus uploader, no queue runner. If you find yourself wanting to write one, you're working against the design — agents discover and upload one HOA at a time.

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
- All 51 jurisdictions now have official source URLs in the registry (OK, PA, SD, WY were migrated from dead aggregator URLs to oscn.net, palegis.us, sdlegislature.gov/api, wyoleg.gov, oklegislature.gov)

## Database Backup
- `POST /admin/backup` — snapshots SQLite DB via `VACUUM INTO`, uploads to GCS bucket `hoaproxy-backups`
- Protected by admin auth (`Bearer {JWT_SECRET}`)
- Triggered twice daily (6am/6pm ET) by cron-job.org
- Blobs stored as `gs://hoaproxy-backups/db/hoa_index-{timestamp}.db`
- GCS uses the `hoaware-ocr` service account (same as Document AI)
- **Recovery:** download latest blob from bucket, upload to Render persistent disk at `/var/data/hoa_index.db`
- Qdrant does NOT need backup — it's rebuildable by re-running the ingestion pipeline

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
