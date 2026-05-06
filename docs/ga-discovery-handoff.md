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

## Backfill Result (run 1, 2026-05-05)

`scripts/ga_county_backfill.py` walked all 416 `_unknown-county/...` GA manifests:

- **168 moved** to the right county prefix (PDF first/last-page text or HOA-name/URL city match).
- **248 still under `_unknown-county/`** — heuristic could not pin a county. Most are HOA names without a city hint and PDFs whose recording county sits past the first ~6 pages or in non-text-extractable scans.
- **0 collisions** (no manifest already existed at the destination).

Suggested follow-up: a second backfill pass that (a) reads more PDF pages, (b) optionally calls `openrouter_repair_lead_names` on the still-unknown rows to ask the model for a city/county guess from the PDF text snippet, then re-routes.

## Per-County Sweep Result (run 1, 2026-05-05)

`benchmark/run_all_ga_counties.sh` looped `benchmark/run_ga_county_sweep.sh`
through 115 GA counties (the bigger metros not in the initial statewide
passes plus every long-tail rural county). Each county got:

- A small per-county queries file (county + cities + Declaration/Bylaws/Articles/Architectural).
- Serper sweep with `--default-county <county>`.
- OpenRouter validate with `--county <county>` (DeepSeek primary, Kimi K2.6 fallback).
- Cross-run URL dedup + junk filter.
- Deterministic direct-PDF clean of the same Serper output.
- Combined probe with `pre_discovered_pdf_urls` preserved.

Bank delta during this pass: +118 manifests / +135 PDFs / +36 county prefixes.
Final coverage: 534 manifests, 659 PDFs, 79 county prefixes.
OpenRouter spend in this pass: ~$0.08 (total now ~$10.89 of the $20 cap).

## Host-Family Per-County Pass (run 1, 2026-05-05)

`benchmark/run_all_ga_counties_hostfamily.sh` looped
`benchmark/run_ga_county_hostfamily.sh` over the top 40 GA counties
in `benchmark/ga_top_counties_hostfamily.txt`. Each county got per-city
host-family queries (eNeighbors `/p/`, hmsft-doc, Cobalt, GoGladly,
FieldStone, FirstService, TheKeyManagers, wsimg/squarespace/rackcdn
CDN PDFs, owned-domain `wp-content/uploads`, plus
architectural/rules-and-regulations) followed by the same
validate -> dedup -> clean -> probe pipeline.

Bank delta: +97 manifests / +100 PDFs / +5 county prefixes.
Final coverage: 631 manifests, 759 PDFs, 84 county prefixes.
OpenRouter spend in this pass: ~$0.13 (running total ~$11.03 of $20 cap).

## Owned-Domain Depth + Find-Owned (run 1, 2026-05-05)

Two depth passes ran after the host-family per-county sweep:

1. `scripts/ga_owned_domain_depth.py` walked every GA manifest with a website already set + fewer than --target-pdfs PDFs and tried to crawl the documents page. Result: 510/631 manifests had no usable website (most leads came from CDN URLs), 22 already had ≥4 PDFs, 99 were probed, only +1 PDF banked. Low ROI as expected; left in the repo for future state-runs that bank more website-attached leads.

2. `scripts/ga_find_owned_website.py` — much higher yield. For every banked GA HOA with fewer than --max-pdfs-already PDFs and a usable name, runs one Serper search ("<name> <county> Georgia HOA documents") and picks the first organic hit whose host (a) isn't a CDN/portal/legal/social and (b) contains the HOA's distinctive name in its host or path. Then probes the homepage so the existing probe pipeline crawls and banks any governing PDFs. Result: 472 manifests processed, 184 probed, **436 PDFs newly banked** (mean ~2.4 PDFs/probe), 287 had no convincing owned domain. Spend was Serper-only (no model) and added the bulk of GA's new depth. This is the most productive single pass we ran for depth.

## Deep Legal-Phrase Pass #1 + #2

`benchmark/ga_deep_legal_queries.txt` (and `_2`) ran statewide
filetype:pdf queries with various recorded-document phrasings
(non-profit corporation, Articles of Incorporation, Restated Bylaws,
Amendment to Declaration, Master Deed, community-suffix variants like
Ridge/Springs/Pointe). Probed without OpenRouter validation through
`benchmark/clean_direct_pdf_leads.py`. Pass #1: 200 candidates -> 96
cleaned -> 95 banked PDFs / +76 manifests (many with malformed slugs
that the backfill then re-routed). Pass #2: 238 candidates -> clean+probe
in flight at handoff time.

## Backfill Round 2

A second `scripts/ga_county_backfill.py` pass over the
`_unknown-county/` manifests that grew during the depth +
deep-legal-1 work: 37 moved to the right county, 11 collisions
(would have overwritten an existing manifest at the destination),
276 still unrouted. Bank now at 86 county prefixes.

## Final Run Stats (2026-05-06)

After all passes — initial county sweep, host-family per-county over
top 40 metros, deep legal-phrase #1 + #2, county/city .gov + resort
sweep, find-owned rounds 2 + 3 + 4, owned-domain depth, atlanta-condo
sweep, LLM-assisted backfill, three heuristic backfills, plus the v2
deep per-county sweep over 79 GA counties with cross-state re-routing
enabled:

- **GA bank: 1843 manifests / 2746 PDFs / 97 county prefixes**
- 309 manifests still under `_unknown-county/` (mostly text-non-extractable
  scanned PDFs with no city/county hint anywhere — those need future
  Serper-based address lookup or DocAI OCR to route).
- ~1.49 PDFs / HOA average (vs. NC=4.05, TN=1.13, KS=2.40).
- **40 US state buckets** populated: AL, AR, AZ, CA, CO, DE, FL, GA,
  HI, IA, ID, IL, IN, KS, KY, LA, MD, MI, MN, MS, MT, NC, NE, NH, NJ,
  NM, NV, NY, OH, OK, OR, PA, SC, TN, TX, UT, VA, WA, WI, WY. Each is
  a free win from cross-state re-routing in the v2 sweep — those
  states won't have to rediscover those HOAs when their own state
  passes start.
- $17.69 OpenRouter spent ($2.31 remaining of the $20 cap; the v2
  driver's per-county DeepSeek validation was the main cost).

## Useful Next Branches

- **Manual merge of backfill collisions** (35 cases now): take the clean-slug manifest as canonical, copy any missing PDFs from the malformed-slug version, then delete the malformed copy. Each is a 2-minute manual diff.
- **Cobalt-managed HOA index direct fetch** of `https://cobaltreks.com/hoa-management/` (worked well in KS).
- **Owned-domain whitelist preflight** of the manifests find-owned just enriched — they got new `website` set during the probe; a focused crawl with whitelisted PDF URLs should add depth without polluting with newsletters/forms.
- **Statewide eNeighbors public-document URL pass** focused on `/p/` community pages rather than search.
- **PDF-text + model county lookup** for the 296 remaining `_unknown-county/` manifests (would cost ~$0.50 of OpenRouter spend).
- **Per-HOA Serper search for missing website** for the 510 manifests with no website set at all (the find_owned pass already used HOA name + county, but a second pass without the county hint sometimes finds owned domains the county-restricted search missed).
