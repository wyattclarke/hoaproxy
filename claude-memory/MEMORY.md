# HOAware Project Memory

## Project Overview
HOAware is a semantic search / Q&A platform for HOA documents. Stack: FastAPI, SQLite, Qdrant vector DB, OpenAI embeddings, GPT-4o-mini. See [hoaware/__init__.py](hoaware/__init__.py) and [api/main.py](api/main.py).

## Legal Corpus Pipeline
The core legal work is an ETL pipeline at [scripts/legal/](scripts/legal/):
- `build_source_map.py` → `fetch_law_texts.py` → `normalize_law_texts.py` → `extract_rules.py` → `assemble_profiles.py` → `validate_corpus.py`
- Pipeline orchestrated by `run_pipeline.py`
- Data: `data/legal/state_source_registry.json` (source registry), `legal_corpus/` (raw/normalized/metadata)
- Rules stored in `legal_corpus/metadata/extracted_rules.jsonl` (NOT SQLite — `data/legal/legal_corpus.db` is empty)
- App query API: `hoaware/law.py`

## Completed Legal Corpus Work (Feb-Mar 2026)
All 7 plan phases executed. See plan at `/Users/wyattclarke/.claude/plans/delegated-watching-porcupine.md`.

**Final state:**
- Registry: 170+ entries; records_access went from 12 → 93+ sources
- Rules: 8,383 total (6,181 records_access, 2,086 proxy_voting, 116 records_sharing_limits)
- Jurisdictions: 47/51 with assembled profiles (up from 27)
- States with 0 rules: 4 (OK, PA, SD, WY — genuinely inaccessible via static HTML)

## Key Technical Findings

### Source Quality System ([scripts/legal/source_quality.py](scripts/legal/source_quality.py))
- `official_primary` = .gov domains + OFFICIAL_HOST_ALLOWLIST
- `aggregator` = AGGREGATOR_HOST_TOKENS (justia, findlaw, lawserver, onecle, etc.)
- `unknown` = everything else — **NEVER extracted** (not even with --include-aggregators)
- `oscn.net` added to OFFICIAL_HOST_ALLOWLIST (Oklahoma State Courts Network)
- `law.onecle.com` added to AGGREGATOR_HOST_TOKENS

### DC Not in US_STATES
`build_source_map.py` hardcodes `US_STATES` — DC was missing. Added "DC" to that list.

### JavaScript-Heavy Sites (Cannot Scrape)
- `iga.in.gov` (Indiana) — React SPA
- `sdlegislature.gov` (South Dakota) — React SPA
- `oscn.net` (Oklahoma) — Cloudflare Turnstile CAPTCHA
- `legis.state.pa.us` (Pennsylvania) — JS-rendered
- `govt.westlaw.com` — JS-rendered (used as PA proxy_voting source but empty)

### Working Aggregator URL Patterns (codes.findlaw.com)
Works for: GA, IN, NJ, MS (with correct title slug)
URL format: `codes.findlaw.com/{state-abbrev}/{title-slug}/{state-abbrev}-code-sect-{section}.html`
- Title slug must exactly match findlaw's title name (e.g., MS uses `title-89-real-and-personal-property` not `title-89-property-rights-and-tenures`)
- State prefix varies: `ga-code`, `in-code`, `nj-st`, `ms-code`, `pa-csa`
- Justia.com now 403-blocks many specific section URLs; findlaw is more reliable

### Normalization Quality Filter
`_is_navigation_heavy()` in [scripts/legal/normalize_law_texts.py](scripts/legal/normalize_law_texts.py):
- Fires when <15% long words AND (<30 long words OR 3+ nav patterns)
- Can be too aggressive for short statutes — check raw HTML if normalized file shows "Navigation Page (low quality)"

### Running the Pipeline
```bash
# Full pipeline
python3 scripts/legal/run_pipeline.py --skip-validate --include-aggregators

# Per-state with re-fetch (after changing registry URLs)
python3 scripts/legal/run_pipeline.py --refresh-fetch --state TX --skip-validate --include-aggregators

# After registry URL changes, must rebuild source_map first
python3 scripts/legal/build_source_map.py

# Extract and assemble only (no fetch/normalize)
python3 scripts/legal/extract_rules.py --include-aggregators
python3 scripts/legal/assemble_profiles.py

# Human review queue
python3 scripts/legal/export_review_queue.py
```

### Source Map vs Raw Files
- Source map tracks what SHOULD be fetched
- Raw files track what WAS fetched
- After updating registry URLs: must rebuild source_map AND delete stale raw/normalized files before re-fetching
- Stale raw files for a given (state, community_type, bucket) must be deleted manually

## User Preferences
- No constant approval prompts — user gets frustrated; proceed autonomously
- User is building this as a real product (not just a prototype)
