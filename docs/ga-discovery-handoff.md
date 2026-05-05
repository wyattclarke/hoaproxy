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
- 2026-05-04 (initial sweep): 200 candidates -> DeepSeek validation kept 120 -> JSONL filter kept 118 -> probe banked ~113 manifests / 108 PDFs. Productive hosts in this pass: dryden-homes.com, dunhammarsh.com, copperridge.net, swc-hoa.com, lookbooklink.com, thekeymanagers.com, gene-clark-4rsa.squarespace.com, fieldstonerp.com, www.northfarmhoa.org, www.gwinnettcounty.com (DocumentCenter PDFs), riverbrookeofsuwanee.com, knobhillpoa.org. Bad-but-frequent hosts: ecorp.sos.ga.gov (corporation registry HTML, no PDF), legis.ga.gov (legislative bill PDFs), hoa.texas.gov (out of state), luederlaw.com (law firm marketing), sfmohcd.org (San Francisco listings).
- 2026-05-04 (host-family sweep): `ga_host_family_queries.txt` -> 250 candidates -> DeepSeek kept 137 -> dedup 105 -> probe banked Bayswater (7 PDFs in one go), Knob Hill, Longleaf Pointe, Princeton Corners, Wynfield, Magnolia Creek, Woodland Trace. Big hosts that were productive: static1.squarespace.com, img1.wsimg.com, nebula.wsimg.com, rackcdn.com, fieldstonerp.com, thekeymanagers.com, fsresidential.com, mariettaga.gov, gwinnettcounty.com, brookhavenga.gov.
- 2026-05-04 (legal-phrase sweep): `ga_legal_phrase_queries.txt` -> 198 candidates -> deterministic clean kept 93 -> probe banked 92 PDFs. Names need repair (frequent leading "of", "laws -", or "Georgia Property Owners' Association Act" prefixes); plan to run `openrouter_repair_lead_names.py` on next batch before probing.
- 2026-05-04 (CDN-direct sweep): `ga_cdn_pdf_queries.txt` -> 182 candidates -> deterministic clean kept 61 -> dedup vs prior 34 -> probe banked 34/34. Productive new hosts: dryden-homes, fieldstonerp, irp.cdn-website, athensbestproperties.
- 2026-05-04 (mgmt-co sweep): `ga_management_co_queries.txt` -> 127 candidates -> clean kept 38 (12 unique vs prior, 5 high-confidence after name repair) + DeepSeek validate kept 59 (16 unique). Probed both: 5 cleaned banked, 16 validated banked Hickory Bluffs etc. Hoaleader.com pages bank as junk because they are educational articles, not specific HOAs.
- 2026-05-04 (eNeighbors+hmsft sweep): `ga_eneighbors_hmsft_queries.txt` -> 97 candidates -> clean kept 12 (1 unique) + DeepSeek validate kept 39 (21 unique). Probed validated set: Tamerlane, Lake Spivey CA, Hidden Valley Estates, Brierfield, Long Leaf Pointe, Magnolia Ridge (12 PDFs), The Laurels (12 PDFs). One Fox Lake `eNeighbors` site banked 11 PDFs but inferred name was the page boilerplate "Georgia ... Download community" — slug needs post-hoc repair.
- 2026-05-04 (extra-metros sweep): `ga_extra_metros_queries.txt` -> 194 candidates -> clean+validate in flight. Targets coastal/north/west GA + architectural-standards/modification variants.
- Spend so far: about $0.04 OpenRouter (~$10.78 of $20 cap used; net $0.04 added during this GA run).
- Bank coverage at last check: ~360 manifests / ~399 PDFs across the GA prefix. Many manifests under `_unknown-county/` because validated leads do not currently carry county fields.

## Lead Quality Stance

The user's preference is **breadth over polish**: bank every lead that has a plausible HOA name plus a town/county or a public document URL. Manifests with no PDFs are kept if name+location are present; manifests with malformed names are kept if there is a real PDF, with name repair deferred to a post-hoc pass. The only hard rejects are out-of-state hits, generic legal pages without a specific community, private portals, and obvious junk hosts. See the "What Counts As A Worthwhile Lead" section in `docs/state-hoa-discovery-playbook.md`.

## County Routing (Outstanding Debt)

The current GA passes ran with statewide query files and statewide validation, so almost every manifest landed under `gs://hoaproxy-bank/v1/GA/_unknown-county/...`. Per the playbook's "Always Run County-By-County" rule, this is a known debt that should be worked off:

- For ongoing GA work, every new sweep should be one county at a time. Generate per-county queries (`openrouter_ks_planner.py county-queries --county Fulton ...`), pass `--default-county Fulton` to the Serper scraper, pass `--county Fulton` to `validate-leads`, and let the existing probe pipeline carry the county to the bank.
- For already-banked `_unknown-county/...` manifests: a follow-up pass should walk them, re-derive the county from the PDF first-page text or the source URL host, and re-bank under the correct county prefix. Until that runs, GA county analytics are blocked.

## Known Bank-Slug Issues

- Several manifests live under malformed slugs from the legal-phrase pass (e.g. `_unknown-county/a-section-44-3-220.../`, `_unknown-county/all-residents-of-.../`, `_unknown-county/and-restated-articles-of-incorporation-of-wicks-creek/`). The PDFs inside are real and classified correctly; only the directory name is bad. Future work: walk `gs://hoaproxy-bank/v1/GA/` for manifests whose slug matches a malformed pattern, reread the first page of each PDF, derive a clean HOA name, and rewrite the manifest under the new slug. Do not delete the underlying PDFs.
- About 45 GA manifests have empty `documents: []` arrays (probe found a community page but couldn't harvest a governing PDF). Per the breadth-over-polish stance these are kept if the name+state are real, since a future drain worker can use the name to look up other sources. Only delete if the name is also junk (matches the malformed-slug patterns above).
