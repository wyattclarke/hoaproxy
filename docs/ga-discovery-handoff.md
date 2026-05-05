# Georgia Discovery Handoff

Updated: 2026-05-04

User instruction: continue autonomously for GA. Do not stop at checkpoints. Commit or hand off as needed, then immediately keep scraping. Only final-answer if blocked, out of budget, or asked for status.

## Current State

- Bank prefix: `gs://hoaproxy-bank/v1/GA/`
- Starting count: 0 manifests, 0 PDFs (verified via `gsutil ls 'gs://hoaproxy-bank/v1/GA/...'`).
- Active strategy: deterministic Serper county/legal/source-family search; OpenRouter only for compact validation/triage.
- Reusable scraper: `benchmark/scrape_state_serper_docpages.py`.
- Initial query file: `benchmark/ga_initial_queries.txt`.
- Models: `deepseek/deepseek-v4-flash` primary; `moonshotai/kimi-k2.6` fallback. Gemini and Qwen Flash blocklisted.

## Guardrails

- Leads must use `state="GA"` so documents land under `gs://hoaproxy-bank/v1/GA/`.
- Use `hoaware.discovery.probe.probe()` / `hoaware.bank.bank_hoa()` as the write path.
- Never send secrets, cookies, resident data, private portal content, emails, payment data, or internal/work data to any model.
- Respect robots.txt with `HOA_DISCOVERY_RESPECT_ROBOTS=1` and practical delays.
- Do not commit `benchmark/results/`, `benchmark/run_benchmark.sh`, or `benchmark/task.txt`.
- `hoaware/discovery/__main__.py` started dirty; do not modify unless required.

## Counties Started

- Fulton / Atlanta / Sandy Springs / Alpharetta / Johns Creek / Milton / Roswell
- Gwinnett / Lawrenceville / Duluth / Suwanee / Snellville / Peachtree Corners
- Cobb / Marietta / Smyrna / Kennesaw / Acworth / Powder Springs
- DeKalb / Decatur / Dunwoody / Brookhaven / Tucker / Stone Mountain
- Cherokee / Woodstock / Canton / Holly Springs
- Henry, Fayette, Coweta, Douglas, Paulding, Forsyth, Clayton, Hall, Chatham, Richmond, Columbia, Bibb, Houston

## Source Families To Try

- eNeighbors `/p/{community}` and public-document URLs.
- Municipal `DocumentCenter/View` for GA cities.
- Cobalt-managed HOA pages.
- HOAMsoft / `hmsft-doc` direct PDFs (CDN at `pmtechsol.sfo2.cdn.digitaloceanspaces.com/hmsft-documents`).
- HOA Express `/file/document-page/`.
- GoGladly `/connect/document/`.
- WordPress uploads (`/wp-content/uploads/`).
- BuilderCloud / S3 / WebsiteFiles CDN PDFs.
- Recorded documents matching `Georgia non-profit corporation`, `Articles of Incorporation`, `Restated Bylaws`, `Supplemental Declaration`, `Amendment to Declaration`, `Clerk of Superior Court` (Georgia counties record HOA declarations with the clerk of superior court).

## Running Log

- 2026-05-04: Bank coverage 0 manifests / 0 PDFs. Wrote `benchmark/ga_initial_queries.txt` with broad statewide + top-county + source-family seeds. Starting first deterministic Serper sweep.
