# Multi-State HOA Ingestion Playbook

This is the single canonical reference for autonomous LLM-driven HOA discovery, banking, and live ingestion across all 51 US jurisdictions. It supersedes four overlapping docs (listed in "Doc Status" below) and covers every phase from context priming through mandatory retrospective. For the `/upload` API contract, see `docs/agent-ingestion.md` (unchanged).

---

## TL;DR by Tier

| CAI estimate | Tier | Approach | OCR budget | Parallelism | Wall time |
|---|---|---|---|---|---|
| < 1,500 | 0 — Tiny | Batch 3–5 unmonitored sessions | $5–8 each | 3–5 states at once | ~1 day/batch |
| 1,500–4,000 | 1 — Small | Solo unmonitored run | $10–20 | 1 state per session | 1–2 days |
| 4,000–10,000 | 2 — Medium | Phased; OCR-cost gate after Phase A | $20–40 | 1 state per session | 2–4 days |
| 10,000–25,000 | 3 — Large | Operator-supervised, county-batched | $50–150 | Sequential counties | Multi-week |
| > 25,000 | 4 — Huge | Own state-specific plan | $500+ | N/A | Months |

---

## Doc Status: What This Supersedes

- **`docs/small-state-end-to-end-ingestion-plan.md`** — original 10-phase pipeline; absorbed and extended here with Phase 0, Phase 10, tier differentiation, and multi-state batching. Kept for historical reference.
- **`docs/state-hoa-discovery-playbook.md`** — discovery techniques, model strategy, per-branch stop rules; absorbed into Phase 2 and "Discovery Technique Reference". Kept for historical reference.
- **`docs/bank-to-live-ingestion.md`** — bundle shape, status values, geography policy; absorbed into Phases 7–8. Kept for historical reference.
- **`docs/gcs-prepared-ingest-plan.md`** — bundle schema, OCR policy, Render importer behavior; absorbed into Phases 5–8. Kept for historical reference.

---

## State Sizing And Status

Source: `docs/cai_state_hoa_counts.txt`. Status as of 2026-05-07.

| State | CAI estimate | Tier | Status | Notes |
|---|---|---|---|---|
| CA | 51,250 | 4 | not-started | Own state plan; don't use this playbook |
| FL | 50,100 | 3 | done | Sunbiz bulk + per-county Serper; canonical Tier 3 |
| TX | 22,900 | 3 | not-started | TX SOS huge volume; operator-supervised |
| IL | 19,750 | 3 | not-started | — |
| NC | 15,050 | 3 | not-started | Aggregator-first (Closing Carolina, CASNC) |
| NY | 14,500 | 3 | not-started | — |
| CO | 11,700 | 3 | not-started | — |
| MA | 11,600 | 3 | not-started | — |
| GA | 11,300 | 3 | done | Per-county Serper; canonical Tier 3 |
| AZ | 10,200 | 3 | not-started | — |
| WA | 10,900 | 3 | not-started | — |
| VA | 9,200 | 2 | not-started | — |
| OH | 8,800 | 2 | not-started | — |
| MI | 8,700 | 2 | not-started | — |
| MN | 8,000 | 2 | not-started | — |
| SC | 7,500 | 2 | partial | Only benchmarks done |
| NJ | 7,200 | 2 | not-started | — |
| MD | 7,200 | 2 | not-started | — |
| PA | 7,150 | 2 | not-started | — |
| WI | 5,650 | 2 | not-started | — |
| MO | 5,750 | 2 | not-started | — |
| TN | 5,400 | 2 | done | Per-county Serper; canonical Tier 2 keyword run |
| IN | 5,200 | 2 | in-progress | Active CT/IN sessions; do not modify |
| CT | 5,150 | 2 | in-progress | Active CT/IN sessions; do not modify |
| OR | 4,150 | 2 | not-started | — |
| KY | 2,500 | 1 | not-started | — |
| LA | 2,200 | 1 | not-started | — |
| MT | >2,000 | 1 | not-started | — |
| KS | <2,000 | 1 | done | Per-county Serper; canonical Tier 1/2 keyword run |
| OK | <2,000 | 1 | not-started | — |
| ME | <2,000 | 1 | not-started | — |
| NH | <2,500 | 1 | not-started | SoS-first recommended |
| NV | 3,800 | 1 | not-started | — |
| UT | 3,700 | 1 | not-started | — |
| HI | 1,600 | 1 | not-started | SoS-first recommended |
| AL | >3,000 | 1 | not-started | — |
| ID | <3,000 | 1 | not-started | — |
| IA | <3,000 | 1 | not-started | — |
| RI | <1,250 | 1 | done | SoS-first canonical Tier 1 |
| NE | <1,200 | 0 | not-started | — |
| DE | <1,500 | 0 | done | Open-portal (PaxHOA) + Serper supplement |
| DC | <1,500 | 0 | not-started | SoS-first recommended |
| VT | <1,500 | 0 | not-started | SoS-first recommended |
| NM | <1,500 | 0 | not-started | — |
| AK | <1,000 | 0 | not-started | — |
| AR | <1,000 | 0 | not-started | — |
| MS | <1,000 | 0 | not-started | — |
| WV | <1,000 | 0 | not-started | — |
| ND | <750 | 0 | not-started | — |
| WY | <750 | 0 | not-started | — |
| SD | <600 | 0 | not-started | — |

---

## The Unmonitored Run Shape (Universal Phases)

Pipeline flow: `Context priming → Preflight → Discovery/banking → Metadata repair → Doc filter → OCR → Geo enrichment → Prepared bundles → Render import → Verify → Retrospective`

### Phase 0 — Context Priming

Mandatory before any new state run.

**Required reading (in order):**
1. This playbook (`docs/multi-state-ingestion-playbook.md`)
2. `CLAUDE.md` (or `AGENTS.md` for Codex) — environment, secrets, cost rules
3. `docs/agent-ingestion.md` — `/upload` contract, categories, OCR routing, budget caps
4. At least one prior handoff: `state_scrapers/ks/notes/discovery-handoff.md` (most detailed) or `state_scrapers/tn/notes/discovery-handoff.md` (recent, thinner)

**Mandatory workflow gates** (from `docs/state-discovery-prompt-template.md`) — applied before every model call or bank write:
1. Refresh exact-source dedup against live GCS manifests for the target state.
2. Reject signed, credentialed, private, portal, payment, resident, login, and internal URLs.
3. Reject non-governing types: newsletters, minutes, budgets, forms, applications, directories, facility docs, real-estate listings, court packets, government planning packets.
4. Require governing-document evidence from filename, URL, title/snippet, page text, or extracted PDF text.
5. Require state/county evidence, or reroute to correct state/county when clear.
6. Use OpenRouter only on surviving compact public metadata (`name`, `source_url`, `title`, `snippet`, `filename`, deterministic category, state/county hints).

**Sub-agent roles:**
- **Explorer** — inspect one county or host family; return likely query/source patterns. Uses low reasoning.
- **Runner** — execute deterministic search/dedupe/clean/probe commands for one county/source family. Uses low reasoning.
- **Curator** — review compact public metadata after deterministic gates; propose keep/reject/name repairs. Uses medium reasoning.
- **Verifier** — check bank counts, probe output, handoff consistency, dirty git scope. Uses low reasoning.

Orchestrator reserves judgment for: choosing next source-family branch, reading validator audits, calling the two-sweep stop rule, cross-state routing edge cases, and safety/policy questions.

### Phase 1 — Preflight

Fail fast if any prerequisite is missing:
- GCS credentials can list `hoaproxy-bank` and `hoaproxy-ingest-ready`.
- Document AI config present; one-page OCR smoke test passes.
- Serper key present.
- `HOAPROXY_ADMIN_BEARER` or Render API credentials can reach admin endpoints.
- Local disk has room for PDFs, sidecars, ledgers, and caches.
- Git worktree status captured so generated outputs don't mix with unrelated dirty files.

Expected output:

```json
{
  "state": "RI",
  "gcs_bank_ok": true,
  "prepared_bucket_ok": true,
  "docai_ok": true,
  "serper_ok": true,
  "render_admin_ok": true,
  "max_ocr_budget_usd": 15
}
```

### Phase 2 — Discovery And Raw Banking

Bank everything plausible. False positives are cheaper than false negatives here.

**Minimum bar to bank:** HOA name + (town/city or county OR public doc URL).

**Mandatory association only.** Signals that indicate mandatory: Declaration of Covenants, CC&Rs, Restrictive Covenants, Master Deed, Articles of Incorporation of an HOA, "Bylaws of \<community\> Homeowners Association" tied to a recorded declaration. Voluntary signals to skip: standalone Architectural Guidelines with no CC&R reference, civic-association meeting minutes, garden-club bylaws.

**Discovery source selection:**

| Pattern | Use when | Reference states |
|---|---|---|
| Keyword Serper per county | County/town names are nationally unique; recorder `.gov` sites publish PDFs | TN, KS, GA |
| SoS-registry first, Serper enrichment | Small population OR county/town names overlap other states (Bristol, Newport, Washington) | RI; recommended for CT, NH, ME, VT, HI, DC |
| Open-portal scrape | Public recorder exposes recorded instruments without payment | DE (PaxHOA New Castle) |
| Aggregator harvest | Strong third-party directory exists with names + linked HOA websites | NC (Closing Carolina, CASNC) |

**SoS-registry specifics:**
- "Contains" searches need single-word patterns (`condominium`, `homeowners`, `owners`, `civic`, `townhouse`, `estates`, `village`, `commons`); multi-word may return zero hits even when the substring exists.
- POST to `response.url` (not the GET URL) and preserve full hidden-field set including `__VIEWSTATEENCRYPTED` and `__LASTFOCUS` across pagination.
- Post-filter with name-pattern regex to drop generic hits (`Civic Initiatives LLC`, `Townhouse Pizza`).
- Filter mailing address to in-state; keep `--include-out-of-state` flag for management-co audit.
- Stock `probe-batch` CLI ignores extra keys including `pre_discovered_pdf_urls`. Use `state_scrapers/ri/scripts/probe_enriched_leads.py` when handing curated PDF URLs.
- SoS corporate-filing PDFs (e.g. `business.sos.<state>.gov/CORP_DRIVE/.../...pdf`) are first-class governing documents — score them positively, do not block.

**Bank path:**
```text
gs://hoaproxy-bank/v1/{STATE}/{county}/{hoa-slug}/
```

**Required manifest fields:**
- `name`, `aliases`
- `metadata_type` (HOA / condo / coop / timeshare when clear)
- `address.state`, `address.county`, `address.city`, `address.street`, `address.postal_code` (public only)
- `website.url`, platform/manager hints, login-wall flag
- Per-field provenance
- Geography clues: subdivision/neighborhood name, ZIPs from PDFs, plat/subdivision labels, GIS/map links
- `documents[]` with URL, filename, SHA, page count when known, source
- `source_urls`, `discovery_notes`

**Ambiguous category:** bank with `suggested_category=null`; the prepared worker runs page-one review before final keep/reject.

**Owned-site preflight:** before probing HOA-owned websites, scrape page links and whitelist only governing-doc URLs; pass them as `pre_discovered_pdf_urls` to avoid banking newsletters/minutes/pool docs.

**Out-of-state hits:** reroute to correct state prefix automatically. Do not drop them — they save a future state's discovery cost. `clean_direct_pdf_leads.py` uses `detect_state_county()` to detect and overwrite `Lead.state`/`Lead.county` before probing.

**Per-branch stop thresholds:**
- < 5 candidates from ~20 search calls → stop that branch.
- Two consecutive sweeps with < 3 net-new manifests AND < 10 net-new PDFs AND > 80% rejects → stop that source family, move to next.

**Per-state two-sweep stop rule:** stop active discovery for the state when two consecutive sweeps both meet all three thresholds above. Allowed follow-up: dedup audits, unknown-county repair, name repair, targeted re-mining of already-downloaded result sets (no new Serper/OpenRouter spend).

**Per-branch pivot order:**
1. County sweeps dry → host-family expansion.
2. Source family stops → legal-phrase searches over recorded documents (`Register of Deeds`, `{state} not-for-profit corporation`, `Articles of Incorporation`, `Amendment to Declaration`, `Restated Bylaws`, `Supplemental Declaration`).
3. All flatten → owned-domain whitelisted preflights.

**`HOA_DISCOVERY_MODEL_BLOCKLIST` enforcement:** Gemini and Qwen Flash variants (`qwen/qwen3.5-flash`, `qwen/qwen3.6-flash`) are blocked for all autonomous scraping. Override only for an explicit benchmark.

**Source-family deterministic-mode promotion:** after two successful sweeps in one host family, stop using models on it except for compact name repair.

**Always run county-by-county.** Every Serper sweep and probe batch is scoped to one county so manifests land under the correct GCS county prefix. Out-of-scope hits are re-routed, not rejected.

### Phase 3 — Pre-OCR Metadata Repair

Before any OCR spend, repair manifests using scrape metadata and source URLs only.

Required repair attempts:
- Fill missing county from bank prefix, source URL, or recorder site.
- Fill missing city from HOA website, source URL, or place search.
- Normalize HOA names and aliases.
- Dedupe obvious duplicate manifests.
- Mark wrong-state candidates; keep audit trail.

**Postal village mapping is mandatory.** USPS place names often don't match incorporated municipalities. Build a village→municipality lookup before prepare or expect `_unknown-county/` slugs. Examples: `Chepachet → Glocester` (RI), `Rumford → East Providence` (RI). Audit `_unknown-county/` after discovery and backfill or fix before prepare.

### Phase 4 — Document Filtering

Apply only hard safety/cost rejects before page-one OCR:
- Exact duplicate SHA already banked, prepared, or live.
- PII-risk: directories, ballots, violation notices, owner rosters, filled forms.
- Unsupported file type.
- Wrong-state evidence.
- Page count over the configured OCR cap.

Everything else gets page-one review before exclusion. Do not reject solely from title, filename, or link text. Title-only filtering caused false negatives in KS.

**`--include-low-value` default policy:** `minutes`, `financial`, and `insurance` documents are included by default when page-one/full text shows they belong to the HOA. Pass `--include-low-value` explicitly to enable; omit to restrict to the primary governing categories.

**`documents.hidden_reason` semantics** (from `docs/agent-ingestion.md`): documents hidden from the live site carry an explicit reason string (`pii:*`, `junk:*`, `unsupported_category:*`, `page_cap:*`, `docai_budget`, `duplicate`). Every rejection must write this field in the ledger.

Accepted live categories: `ccr`, `bylaws`, `articles`, `rules`, `amendment`, `resolution`, `plat`, `minutes`, `financial`, `insurance`.

Rejected before OCR (hard): `membership_list`, `ballot`, `violation`, `court`, `tax`, `government`, `real_estate`, `unrelated`.

### Phase 5 — OCR Strategy

Run OCR locally or in a GCP worker. Never on Render for bulk ingestion.

| Document state | Action |
|---|---|
| Text-extractable | PyPDF locally |
| Scanned, page-one relevant | Full DocAI locally/GCP |
| Scanned, page-one irrelevant | Reject with page-one audit (never title-only) |
| Scanned, page-one ambiguous | Keep if budget allows; else mark `budget_deferred` |
| Duplicate or PII | No OCR |

**Cost estimate:** `estimated_cost = docai_pages * 0.0015`

**Operational parameters:**
- `HOA_DOCAI_CHUNK_PAGES` default: 10 pages per DocAI request.
- `MAX_PAGES_FOR_OCR` hard guard: 200 pages; reject over-cap PDFs before OCR.
- `extracted_texts` sidecar cap: 10 MB; truncate at page boundary if exceeded.
- Ingest semaphore: `Semaphore(1)` — serializes `/upload` calls; faster triggers Render OOM.

**Cost guard layers:**
```bash
# Pre-run alert threshold
curl "https://hoaproxy.org/admin/costs/docai-alert?threshold_usd=N&hours=24&notify=true" \
  -H "Authorization: Bearer $LIVE_JWT_SECRET"

# Running totals
curl "https://hoaproxy.org/admin/costs" \
  -H "Authorization: Bearer $LIVE_JWT_SECRET"
```

Stop runner before exceeding `--max-docai-cost-usd`. Write ledger and mark remaining candidates `budget_deferred`, not silently rejected.

Sidecar shape:
```json
{
  "pages": [{"number": 1, "text": "..."}],
  "docai_pages": 12
}
```

### Phase 6 — OCR-Assisted Geography

Run after Phase 5, before prepared bundles. OCR text from declarations, plats, recorder stamps, minutes, budgets, and insurance certificates contributes city/county/ZIP/subdivision clues.

**Best-effort resolution order:**
1. Manifest public street address or subdivision community address.
2. OSM/Nominatim polygon — only if the public instance is responding (see warning).
3. Serper Places result with strict state + name checks.
4. ZIP centroid: `https://api.zippopotam.us/us/{zip}` (small scale) or Census ZCTA (larger).
5. City-only fallback for profile context; hidden from map.

**Public Nominatim warning:** rate-limits hard above ~100 sequential requests; `Retry-After: 0` persists 15+ minutes even at 1.2s+ inter-request delay. Budget for ZIP centroid as the primary production fallback; treat Nominatim polygons as a bonus. RI achieved 99.5% map coverage with `zip_centroid` alone. When `geo_enrichment_error` rows mention `nominatim.openstreetmap.org` 429s, run post-import backfill via `POST /admin/backfill-locations` rather than retrying.

**Location quality enum:** `polygon` (credible boundary) | `address` (street-level) | `zip_centroid` (repeated ZIP evidence) | `city_only` (profile only, hidden from map) | `unknown`. Map shows only first three.

**Guardrails:** reject candidates outside state bounding box; reject management-company offices unless HOA is the named place; reject senior living, apartment, law firm, city office, and unrelated business categories; require strong normalized name overlap; cache all geocoder/search responses.

### Phase 7 — Prepared Bundle Creation

```bash
python scripts/prepare_bank_for_ingest.py \
  --state {STATE} \
  --bucket hoaproxy-bank \
  --bank-bucket hoaproxy-bank \
  --prepared-bucket hoaproxy-ingest-ready \
  --limit 10000 \
  --max-docai-cost-usd 15 \
  --ledger data/prepared_ingest_{state}_$(date +%Y%m%d_%H%M%S).jsonl \
  --geo-cache data/prepared_ingest_geo_cache.json
```

Additional flags: `--county {Name}`, `--include-low-value`, `--skip-geo-enrichment` (emergency only), `--dry-run`.

**`precheck.json` hint vs authority:** use as a hint for skip decisions; re-run local precheck from PDF bytes if missing or weak.

Prepared output path:
```text
gs://hoaproxy-ingest-ready/v1/{STATE}/{county}/{hoa-slug}/{bundle-id}/
  bundle.json
  status.json
  docs/{sha256}.pdf
  texts/{sha256}.json
```

**Full `bundle.json` schema:**
```json
{
  "schema_version": 1,
  "bundle_id": "sha-or-uuid",
  "source_manifest_uri": "gs://hoaproxy-bank/v1/KS/johnson/example/manifest.json",
  "state": "KS",
  "county": "Johnson",
  "hoa_name": "Example Homes Association",
  "metadata_type": "hoa",
  "website_url": "https://example.org",
  "address": {"city": "Overland Park", "state": "KS", "county": "Johnson"},
  "geometry": {
    "boundary_geojson": null,
    "latitude": null,
    "longitude": null,
    "location_quality": "zip_centroid"
  },
  "documents": [
    {
      "sha256": "...",
      "filename": "declaration.pdf",
      "pdf_gcs_path": "gs://hoaproxy-ingest-ready/v1/KS/johnson/example/bundle/docs/sha.pdf",
      "text_gcs_path": "gs://hoaproxy-ingest-ready/v1/KS/johnson/example/bundle/texts/sha.json",
      "source_url": "https://source.example/declaration.pdf",
      "category": "ccr",
      "text_extractable": false,
      "page_count": 28,
      "docai_pages": 28,
      "filter_reason": "valid_governing_doc"
    }
  ],
  "rejected_documents": [
    {"sha256": "...", "source_url": "...", "reason": "junk:minutes"}
  ],
  "created_at": "2026-05-05T00:00:00Z"
}
```

**Ledger fields per document:** manifest URI, document SHA, category decision, text_extractability decision, page count, DocAI pages, cost estimate, prepared GCS paths, rejection reason.

**Pre-import bundle verification checklist:**
- `bundle.json` validates against schema.
- All PDFs exist at declared GCS paths.
- All text sidecars exist and at least one page has non-empty text.
- Location metadata present when available.
- No PII category present.

### Phase 8 — Render Import

```bash
# Dry run first
curl -sS -X POST \
  "https://hoaproxy.org/admin/ingest-ready-gcs?state={STATE}&limit=50&dry_run=true" \
  -H "Authorization: Bearer $LIVE_JWT_SECRET"

# Apply
curl -sS -X POST \
  "https://hoaproxy.org/admin/ingest-ready-gcs?state={STATE}&limit=50" \
  -H "Authorization: Bearer $LIVE_JWT_SECRET"
```

Repeat until `results` array is empty. Cap is **50 per call** (not 100; 100 returns 400). Count imports by walking `results[]`, not top-level fields.

**`status.json` allowed values:** `ready` → `claimed` → `imported` | `failed` | `skipped`.

**Live JWT drift:** `JWT_SECRET` often diverges from local `settings.env` after Render env-var edits (Render's API silently drops sensitive values on fetch-then-PUT). Resolve at runtime:
```python
def _live_admin_token():
    if os.environ.get("HOAPROXY_ADMIN_BEARER"):
        return os.environ["HOAPROXY_ADMIN_BEARER"]
    api_key = os.environ.get("RENDER_API_KEY")
    service_id = os.environ.get("RENDER_SERVICE_ID")
    if api_key and service_id:
        r = requests.get(
            f"https://api.render.com/v1/services/{service_id}/env-vars",
            headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
        r.raise_for_status()
        for env in r.json():
            e = env.get("envVar", env)
            if e.get("key") == "JWT_SECRET" and e.get("value"):
                return e["value"]
    return os.environ.get("JWT_SECRET")
```

**ZIP extraction before backfill-locations:**
```bash
curl -sS -H "Authorization: Bearer $LIVE_JWT_SECRET" \
  "https://hoaproxy.org/admin/extract-doc-zips?state={STATE}"
```

**Importer internal behavior (step-by-step):**
1. List `ready` bundles for the requested state.
2. Claim one bundle using GCS generation precondition on `status.json`.
3. Download `bundle.json`, PDFs, text sidecars.
4. Save PDFs under `HOA_DOCS_ROOT/{hoa_name}/`.
5. Build `metadata_by_path` dict with `category`, `text_extractable`, `source_url`, `pre_extracted_pages`.
6. Upsert location metadata from bundle address/geometry.
7. Call `ingest_pdf_paths(..., pre_extracted_pages=...)`.
8. Mark bundle `imported` or `failed`. Never calls DocAI; fails bundle if sidecar missing.

### Phase 9 — Post-Import Verification

```bash
# HOA count
curl -sS "https://hoaproxy.org/hoas/summary?state={STATE}"

# Map coverage
curl -sS "https://hoaproxy.org/hoas/map-points?state={STATE}"

# Zero-chunk docs check (must be clean)
curl -sS -H "Authorization: Bearer $LIVE_JWT_SECRET" \
  "https://hoaproxy.org/admin/zero-chunk-docs"
```

Expected final state report:
```json
{
  "state": "RI",
  "raw_manifests": 128,
  "prepared_bundles": 96,
  "imported_bundles": 96,
  "live_profiles": 96,
  "live_documents": 312,
  "map_points": 84,
  "map_rate": 0.875,
  "by_location_quality": {"polygon": 40, "address": 18, "zip_centroid": 10},
  "ocr_cost_usd": 8.42,
  "rejected_documents": 211,
  "budget_deferred": 0,
  "failed_bundles": 0
}
```

**Required checks:**
- `/hoas/summary?state={STATE}` count matches imported bundle count within expected dedupe collisions.
- `/hoas/map-points?state={STATE}` returns no out-of-state coordinates.
- Every imported document has `chunk_count > 0` unless explicitly hidden.
- No `failed` prepared bundles remain without a documented reason.
- Rejected sample review includes random direct links from each rejection class.
- Map rate target: ≥ 80% for Tier 0/1; ≥ 70% for Tier 2/3 (county-level resolution is harder).

If map rate is below target:
1. OCR clue extraction for city/county/ZIP/subdivision names.
2. Serper Places cleanup with strict state + name + category filters.
3. OSM/Nominatim polygon retry from aliases and city/county.
4. ZIP centroid fallback from repeated OCR ZIPs.
5. Demote suspicious or out-of-state records.

### Phase 10 — Retrospective (Mandatory)

Write before the state is considered done.

**Path:** `state_scrapers/{state}/notes/retrospective.md`

**Required fields:**
- Cost estimate per HOA scraped, broken down by: Serper + OpenRouter + DocAI. State assumptions when exact metering is unavailable.
- Main false-positive classes and the cleanup steps needed.
- Final counts: raw bank / prepared / live / docs / chunks / map coverage / out-of-bounds.
- Source families attempted vs productive.
- Lessons learned to fold back into this playbook.

**Exemplars:** `state_scrapers/ri/RI_SCRAPE_RETROSPECTIVE.md` (Tier 1 SoS-first), `state_scrapers/ga/` (Tier 3 per-county Serper).

---

## Tier-Specific Run Shapes

### Tier 0 — Tiny (< 1,500 estimated HOAs)

**Remaining states (10):** AK, AR, DC, MS, ND, NE, NM, SD, VT, WV, WY

Batch 3–5 in parallel autonomous LLM sessions. Each session writes under its own `state_scrapers/{state}/results/{run_id}/`.

- SoS-first discovery typically suffices; county/town name overlap is the main risk.
- Census ZCTA centroid is the map fallback (zippopotam.us free at this scale).
- Per-state OCR budget: $5–8. Stop conditions before completion are rare since the universe is small.
- 1-day end-to-end per batch.
- Coordination: per-batch cost ceiling tracked via `/admin/costs`; sessions are independent.

### Tier 1 — Small (1,500–4,000)

**Remaining states (13):** AL, HI, ID, IA, KY, LA, ME, MT, NE, NH, NV, OK, UT

Solo autonomous run per state. 1–2 days.

- SoS-first OR per-county keyword Serper based on whether SoS is open and HOA-shaped.
- Aggregator harvest as supplement when present.
- RI is the canonical Tier 1 SoS-first run. KS is the canonical Tier 1/2 keyword-Serper-per-county run.
- Per-state OCR budget: $10–20.

### Tier 2 — Medium (4,000–10,000)

**Remaining states (~13):** CT (in-progress), IN (in-progress), MD, MI, MN, MO, NJ, OH, OR, PA, SC (partial), VA, WI

Phased solo run per state:
- **Phase A (no OCR):** 4–8 hour discovery sweep. Capture metadata in bank. Snapshot raw manifests and PDF count.
- **OCR gate:** if total OCR estimate < $25, continue automatically. Otherwise wait for operator green-light.
- **Phase B:** prepare + import + verify.

KS and TN are the canonical Tier 2 keyword-Serper-per-county references.

Per-state OCR budget: $20–40.

### Tier 3 — Large (10,000–25,000)

**Remaining states (8):** AZ, CO, IL, MA, NC, NY, TX, WA

NOT unmonitored. Operator-supervised, county-batched. Multi-week per state.

- GA and FL are the canonical Tier 3 reference runs.
- NC has aggregators (Closing Carolina, CASNC, Seaside OBX, Triad, Wilson PM, Wake/Mecklenburg GIS) — start there.
- TX: TX SOS-like open registry but huge volume.
- Per-state OCR budget: $50–150; OpenRouter: $20–50.

Do not attempt a Tier 3 state unmonitored.

### Tier 4 — Huge (> 25,000)

**Only CA.** Own state-specific plan. Do not use this playbook. Reference FL pattern (Sunbiz bulk + per-county Serper) but expect months of work and $500+ OCR.

---

## Multi-State Batching Playbook

For Tier 0/1 parallel runs:

1. Open 3–5 separate LLM sessions. Each targets one state.
2. Each session writes exclusively under `state_scrapers/{state}/results/{run_id}/` where `run_id` embeds a session identifier (e.g. `ri_20260507_a1b2`).
3. No cross-session coordination needed; GCS bank dedup handles collisions via `(state, county, slug)` merge.
4. Track per-batch cost ceiling via `GET /admin/costs` before each session start and after each session completes.
5. If any session hits a stop condition (budget, blocked auth, DocAI failure), the others continue independently.
6. Daily review: check `final_state_report.json` and `retrospective.md` from completed sessions before launching the next batch.
7. Each session must commit its `notes/discovery-handoff.md` and `notes/retrospective.md` before exit.

---

## Discovery Technique Reference

### Two-Sweep Stop Rule

Stop active discovery when two consecutive sweeps both produce: < 3 net-new valid in-state manifests, < 10 net-new PDFs, and > 80% rejects. A sweep is one concrete executed pass. Allowed follow-up: dedup audits, unknown-county repair, name repair, re-mining already-downloaded results — no new Serper/OpenRouter spend.

### Source-Family Promotion to Deterministic Mode

After two successful sweeps in one host family (eNeighbors, hmsft-doc, WordPress uploads), stop using models on it except for compact name repair. Encode recurring rejects as deterministic filters. This was the biggest KS cost saving.

### Owned-Site Whitelist Preflight

Before `probe(lead)` crawls an HOA-owned site, scrape page links and pass only whitelisted governing-doc URLs as `pre_discovered_pdf_urls`. If a host times out on homepage crawl, retry with `website=null` + known PDF URLs.

### Direct PDF Escalation Procedure

When validated pages create HOA manifests but no PDFs:
1. Run Serper `--include-direct-pdfs` on the same county query file.
2. Keep only HOA-owned or clearly community-specific hosts.
3. Clean malformed names; group PDFs under one `Lead`; probe one at a time with subprocess timeout.

```bash
OPENROUTER_TIMEOUT_SECONDS=80 python benchmark/run_ks_openrouter_discovery.py \
  --models deepseek/deepseek-v4-flash --run-id deepseek_{county}_pdf_1 \
  --queries-file benchmark/results/{state}_{county}_deepseek_queries.txt \
  --skip-seed-queries --model-queries 0 \
  --max-queries 30 --results-per-query 10 --max-results 120 --max-pdfs 25 \
  --triage-batch-size 4 --search-delay 0.15
```

### Out-of-State Hit Rerouting

Do not drop HOA hits outside the sweep's target state. Overwrite `Lead.state`/`Lead.county` before probing. `clean_direct_pdf_leads.py` uses `detect_state_county()` to extract the correct values from PDF text. Bank merges by `(state, county, slug)` — a second sighting appends a `metadata_source` entry. Same logic within a state: a Fulton sweep finding a Cobb HOA banks it under `v1/GA/cobb/<slug>/`.

### Per-Branch Pivot Order

1. County sweeps dry → host-family expansion.
2. Source family stops → legal-phrase searches:
   ```text
   filetype:pdf "{County} County, {STATE}" "Declaration of Covenants" "Homeowners Association"
   filetype:pdf "{STATE} not-for-profit corporation" "Homeowners Association"
   filetype:pdf "Register of Deeds" "{County} County, {STATE}" "Homes Association"
   filetype:pdf "Amendment to Declaration" "{County} County, {STATE}" "Homes Association"
   ```
3. All flatten → owned-domain whitelisted preflights.

---

## Mandatory Workflow Gates

Applied before every model call or bank write:

1. **Source dedup** — refresh exact-source dedup against live GCS manifests for the target state. Skip already-banked source URLs.
2. **Privacy filter** — reject signed, credentialed, private, portal, payment, resident, login, and internal URLs before any model sees them.
3. **Document type filter** — reject newsletters, minutes, budgets, forms, applications, directories, facility/pool docs, real-estate listings, court packets, and government planning packets deterministically.
4. **Governing-doc evidence** — require at least one signal from: filename, URL, title/snippet, page text, or extracted PDF text indicating a governing document.
5. **State/county evidence** — require state/county evidence or reroute to correct prefix. Leads with no clear state go to the validator with `state=null`.
6. **Model input hygiene** — only compact public metadata reaches any model: `name`, `source_url`, `title`, `snippet`, `filename`, deterministic category, state/county hints. Never send secrets, cookies, logged-in pages, resident data, private portal content, emails, payment data, or full unreviewed document text.

---

## State-Specific Guardrails (Lessons Learned)

- **Choose discovery source before writing queries.** Broad keyword Serper drowns small or name-overlapping states (Bristol, Newport, Washington appear in many states). For states where county/town names are not nationally unique, anchor on SoS first.
- **Public Nominatim is not a production dependency.** Rate-limits hard once tripped; treat polygons as a bonus and budget for ZIP centroid (zippopotam.us or Census ZCTA) as the primary fallback.
- **SoS corporate-filing PDFs: let the classifier decide, don't pre-tag.** Articles of Incorporation, Restated Articles, Amendments-to-Articles are correctly tagged `articles`. Annual Reports (RI Form 631), change-of-agent filings are correctly rejected as `junk:government`. Force-tagging SoS filings as `articles` feeds Annual Reports into the wrong category.
- **Postal village names are not municipalities.** Bake a village→municipality lookup into the state-local scraper. RI: `Chepachet → Glocester`, `Rumford → East Providence`, `Greenville → Smithfield`, `Wakefield → South Kingstown`.
- **`probe-batch` drops unknown lead keys** including `pre_discovered_pdf_urls`. SoS-first flows that carry curated PDF URLs need `state_scrapers/ri/scripts/probe_enriched_leads.py`.
- **Live `JWT_SECRET` drifts from local `settings.env`.** Read it at runtime via the Render API for all admin endpoint calls.
- **`/admin/ingest-ready-gcs` caps at 50 per call.** Count imports by walking `results[]`, not top-level fields.
- **SoS Annual Reports are not governing docs.** RI run: 66 SoS filings survived as `articles`; 231 Annual Reports correctly rejected as `junk:government`.
- **`city_only` stays hidden from the map.** Stacked pins for an entire city are worse than no pin.
- **Every automated decision needs a ledger entry.** Random sample review catches systematic false negatives before they reach the live site.
- **Deployment of new `location_quality` values must precede importing records that use them.**
- **`HOA_DISCOVERY_MODEL_BLOCKLIST`:** Gemini is blocked (too expensive per yield, per May 2026 KS activity export). Qwen Flash variants are blocked (runaway hidden reasoning-token usage).
- **Turn boundary is not a blocker.** A final response stops the execution turn; it is not a valid reason to stop autonomous scraping. Only stop when there is a real blocker, the budget is exhausted, or the user asks for status.

---

## Reusable Scripts

| Phase | Script / endpoint | Notes |
|---|---|---|
| Bank manifest operations | `hoaware/bank.py` | GCS manifest, merge, slug, document banking helpers. |
| Discovery primitives | `hoaware/discovery/` | Probe/search helpers and state verification. |
| Prepared bundle creation | `scripts/prepare_bank_for_ingest.py` | Filtering, page-one review, OCR sidecars, geography enrichment, GCS writes. |
| Bundle import | `POST /admin/ingest-ready-gcs` | Render admin import. Cap 50 per call. |
| Location backfill | `POST /admin/backfill-locations` | Polygon / address / place / ZIP record cleanup. |
| ZIP extraction | `GET /admin/extract-doc-zips?state=XX` | Extract ZIPs from doc text before backfill. |
| Zero-chunk check | `GET /admin/zero-chunk-docs` | Post-import verification. |
| Keyword Serper discovery | `benchmark/scrape_state_serper_docpages.py` | Per-county query files + `site:` / `filetype:pdf`. |
| SoS-registry discovery | `state_scrapers/ri/scripts/scrape_ri_sos.py` | RI pattern; adapt for any open SoS registry. |
| Serper enrichment | `state_scrapers/ri/scripts/enrich_ri_leads_with_serper.py` | Exact-name PDF lookups after SoS extraction. |
| Custom probe driver | `state_scrapers/ri/scripts/probe_enriched_leads.py` | Preserves `pre_discovered_pdf_urls`; required when lead JSONL carries curated PDF URLs. |
| Mgmt-co harvesting | `state_scrapers/ri/scripts/find_mgmt_companies.py` | Discover management companies for a state. |
| Mgmt-co bulk harvest | `state_scrapers/ri/scripts/harvest_mgmt_companies.py` | Batch harvest mgmt-co HOA lists. |
| Site-restricted Serper | `state_scrapers/ri/scripts/site_restricted_serper.py` | Per-state site-restricted search patterns. |
| ZIP centroid enrichment | `state_scrapers/ri/scripts/enrich_ri_locations.py` | Production-grade map fallback; uses zippopotam.us + city-centroid table; posts to `/admin/backfill-locations`. |
| OCR ZIP cleanup | `state_scrapers/ks/scripts/enrich_live_locations_from_ocr.py` | Copy/adapt per state. |
| Serper Places cleanup | `state_scrapers/ks/scripts/enrich_live_locations_from_serper_places.py` | Subdivision/place centroid repair; adapt state guardrails. |
| County query generation | `benchmark/openrouter_ks_planner.py county-queries` | Despite filename, state-agnostic. |
| Lead validation | `benchmark/openrouter_ks_planner.py validate-leads` | State-agnostic; pass `--county`. |
| Direct PDF triage | `benchmark/run_ks_openrouter_discovery.py` | State-agnostic despite filename. |
| Manifest/name repair | `state_scrapers/ga/scripts/ga_slug_cleanup.py` | Example of post-scrape manifest repair against GCS. |
| Open-portal pattern | `state_scrapers/de/scripts/scrape_sussex_landmark.py` | DE PaxHOA open-portal; adapt for similar portals. |
| Category classifier | `hoaware/doc_classifier.py` | Categories and page-text classification rules. |
| OpenRouter activity analysis | `benchmark/analyze_openrouter_activity.py` | Analyze cost exports before changing model routing. |

---

## Stop And Escalate Conditions

**Auto-stop silently (write ledger, leave resume command):**
- OCR budget exhausted (`budget_deferred` status on remaining candidates).
- Two-sweep stop rule triggered per state.
- Per-branch stop threshold hit.

**Stop and write stop report, then continue other sessions:**
- OpenRouter credit budget exhausted for a session.
- Serper quota hit mid-run.

**Halt and notify operator:**
- GCS or Render admin auth fails.
- DocAI smoke test fails.
- Live import produces bundle failures with no clear root cause.
- Map verification finds out-of-state coordinates.
- Rejection audit shows systematic false-negative pattern not already in blocklist.
- Any Tier 3 state run that hits unexpected data volume or cost spike.

For each halt: write the exact command to resume and the files to inspect.

**Tier 2 OCR gate:** if total OCR estimate after Phase A ≥ $25, write Phase A counts to `notes/discovery-handoff.md` and await operator green-light rather than proceeding to Phase B automatically.

---

## Appendix A — Kickoff Prompt Template

Substitute `{STATE}`, `{state-name}`, and `{METRO_LIST}` before use.

---

You are in `/Users/ngoshaliclarke/Documents/GitHub/hoaproxy`. Read `CLAUDE.md` (or `AGENTS.md` for Codex), then `docs/multi-state-ingestion-playbook.md`, `docs/agent-ingestion.md`, and at least one prior handoff. `state_scrapers/ks/notes/discovery-handoff.md` is the most detailed; `state_scrapers/tn/notes/discovery-handoff.md` is thinner and more recent. Do not let any single state's specific choices constrain you.

Other state runs may be active in parallel. Coexist gracefully on rate limits. Do not edit shared files actively touched by another run unless you have a specific reason.

### Task

Autonomously scrape public {state-name} HOA governing documents into the existing GCS bank. Use `state="{STATE}"` on leads so documents land under `gs://hoaproxy-bank/v1/{STATE}/...`. Do not create a new bucket.

### Constraints

- Continue autonomously. Turn boundaries are not blockers — see "Autonomy Failure Mode" in the playbook. Only send a final response when there is a real blocker, the explicit budget is exhausted, or the user asks for status.
- Do not use Gemini. Do not use Qwen Flash variants for bulk classification. Both are blocklisted.
- Prefer deterministic search → fetch → preflight → bank over model calls.
- Primary model: `deepseek/deepseek-v4-flash`. Quality fallback: `moonshotai/kimi-k2.6` for the bounded subset of candidates DeepSeek rejects/cannot name/scores below threshold after deterministic gates. Do not retry whole failed DeepSeek batches on Kimi.
- Never send to any model: secrets, cookies, logged-in pages, resident data, private portal content, emails, payment data, or internal/work data.
- Respect `robots.txt` and practical per-host delays.
- Log all model usage to `data/model_usage.jsonl`. Do not log prompts, completions, document text, cookies, or API keys.
- Commit reusable code and docs after each milestone with a descriptive message, then keep scraping.

### Sub-agent right-sizing

Delegate to cheaper subagents (Explorer/Runner/Curator/Verifier roles as defined in Phase 0) for mechanical work. Reserve orchestrator for judgment: choosing next source-family branch, reading validator audits, calling the two-sweep stop rule, cross-state routing edge cases, safety/policy questions.

### Initial strategy

1. Count current {STATE} bank coverage:
   ```bash
   gsutil ls 'gs://hoaproxy-bank/v1/{STATE}/**/manifest.json' 2>/dev/null | wc -l
   gsutil ls 'gs://hoaproxy-bank/v1/{STATE}/*/*/doc-*/original.pdf' 2>/dev/null | wc -l
   ```
2. Start with the highest HOA-density metros:
   {METRO_LIST}
3. Run discovery county-by-county using the pattern appropriate for {state-name} (see "Discovery source selection" table in the playbook).
4. Maintain `state_scrapers/{state}/notes/discovery-handoff.md` with running bank counts, source families attempted, query files used, false-positive patterns to block, model spend, and next branches. Commit as you go.

### Stop rules

See "Per-branch stop thresholds" and "Per-state two-sweep stop rule" in Phase 2. Stop only when source families are genuinely exhausted, the OpenRouter budget is exhausted, or the user explicitly asks for status.

### Required artifacts on completion

- `state_scrapers/{state}/results/{run_id}/final_state_report.json`
- `state_scrapers/{state}/notes/retrospective.md` (see Phase 10 requirements)

---

## Appendix B — Bank Manifest Schema

Required fields:
- `name` (canonical), `aliases[]`
- `metadata_type`: HOA / condo / coop / timeshare
- `address.state`, `address.county`, `address.city`, `address.street`, `address.postal_code`
- `website_url` (or `website.url`)
- `source_urls[]`
- `documents[]`: `url`, `filename`, `sha256`, `page_count` (when known), `source`, `suggested_category`
- `discovery_notes`
- `management_company` (when observed)

Additional field for provenance tracking:
- `metadata_sources[]`: array of `{field, value, source_url, source_type}` records. Used by skip-existing dedup in open-portal patterns (e.g. DE Sussex Landmark) to identify source-of-record per field and avoid re-banking already-seen entries.

---

## Appendix C — Endpoints Reference

| Endpoint | Method | Purpose | Notes / Cap |
|---|---|---|---|
| `/upload` | POST | User/one-off HOA creation + doc upload | Authenticated; 75s gap between calls |
| `/upload/anonymous` | POST | Public contributor upload | 3 req/hour/IP |
| `/agent/precheck` | POST | Classify a PDF before upload | Returns category hint |
| `/admin/ingest-ready-gcs` | POST | Import prepared GCS bundles to live site | Cap 50/call; `dry_run=true` param |
| `/admin/backfill-locations` | POST | Upsert location metadata post-import | Accepts polygon/address/place/zip_centroid |
| `/admin/extract-doc-zips` | GET | Extract ZIPs from doc text for a state | `?state=XX`; run before backfill-locations |
| `/admin/zero-chunk-docs` | GET | List docs with 0 chunks post-import | Use for post-import verification |
| `/admin/costs` | GET | All-time and per-month DocAI cost dashboard | Admin auth |
| `/admin/costs/docai-alert` | GET | Set DocAI spend alert threshold | `?threshold_usd=N&hours=24&notify=true` |
| `/hoas/summary` | GET | Live HOA count by state | `?state=XX` |
| `/hoas/map-points` | GET | Live map coordinates by state | `?state=XX`; use to verify no out-of-state points |
