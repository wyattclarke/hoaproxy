# California HOA + Condo Ingestion Playbook

CA-specific operational doctrine for an exhaustive, multi-day end-to-end run
(discovery → bank → OCR → geometry → live upload → close). This is a
**concrete, executable plan** layered on top of
[`docs/giant-state-ingestion-playbook.md`](./giant-state-ingestion-playbook.md);
where the two disagree, **this doc wins for CA**.

> **"HOA" includes condos.** Davis-Stirling Common Interest Developments
> (Cal. Civ. Code §4000 et seq.) are the universe — Chapter 718-style condos,
> Chapter 720-style HOAs, master associations, planned developments,
> stock cooperatives, and community apartment projects all fall in scope.

---

## 0. Why CA needs its own playbook

CA's structural differences from FL (the giant-state reference) reshape the
strategy in five concrete ways:

1. **Purpose-built CID registry exists.** Davis-Stirling §5405 mandates the
   biennial Statement of Information for Common Interest Developments
   (Form SI-CID), filed separately from the generic nonprofit corp filing.
   This is a CID-only list — no name-regex filtering needed (unlike FL's
   Sunbiz nonprofit dump).
2. **DRE Subdivision Public Reports** (CA-unique). Every CA subdivision >5
   units requires a DRE Public Report; these often reference recorded CC&Rs
   directly. Captures new construction the biennial SI-CID lags 2 years
   behind.
3. **Tract-based geometry is structural, not bespoke.** Every CID in CA is
   recorded against a tract number; county assessor parcel layers expose
   `TRACT_NO` as a field. Joining tract → polygon gives per-HOA polygons
   uniformly across ~8 of the top 10 counties.
4. **Mass concentration in 10 counties.** ~85% of CIDs live in LA, OC, SD,
   RIV, SBD, ALA, SCL, SCC, CC, VEN, SAC. The giant-state "geographic
   dispersion" rotation (FL panhandle → north → central → SE) is wrong;
   replace with **metro-stratified** rotation: LA Basin → Bay Area → SD →
   IE → OC → Sac → Central Coast → Central Valley.
5. **Naming density.** Dozens of "Park Place HOA" / "The Vineyards"
   duplicates statewide. Bank dedup key is `(state, county, city, slug)`,
   not the playbook's `(state, county, slug)`.

CA bank target: **≥ 25k entities** (50% of 51,250 CAI estimate);
**≥ 5k live entities** post-Phase 10 filter.

---

## 1. CA-specific data sources

### 1a. Registries (Phase 1)

| Source | Format | Volume | Refresh | Status |
|---|---|---|---|---|
| **CA SoS bulk corp dump** (already on disk: `data/california_hoa_entities.csv`) | CSV pre-filtered to HOA candidates | 34,938 | Quarterly | **Available** |
| **SI-CID list** (CA SoS Common Interest Development list under §5405) | CSV/Excel; subset of the above with CID flag | ~30–40k expected | Biennial | **Acquisition step** |
| **DRE Subdivision Public Reports** | Web-scraped per-county | ~thousands of new subdivisions/year | Continuous | **Driver D** |
| **Legacy `data/california/hoa_index.jsonl`** | JSONL | 5,840 (SD/OC/Sac only) | One-time | **Available** |
| **Legacy `data/california/hoa_details.jsonl`** | JSONL with pm_address | 2,145 detailed | One-time | **Available** |

**Phase 1 deliverable**: `data/ca_registry_hoas.jsonl` — merged, deduped,
city/county-tagged. Keys: `name, doc_no, status, source, mail_city,
mail_state, agent_city, agent_state, county, county_source, city_norm,
zip` (when extractable from legacy address).

### 1b. CA-specific search lexicon (Driver B patterns)

Beyond the giant-state playbook's host-family list, CA-specific patterns:

- `"Davis-Stirling"` — CA-only statute reference; appears in nearly every
  modern CA CC&R
- `"Common Interest Development"` — CA-only term of art (mirrors §4100)
- `"Mutual Benefit Corporation"` — CA-specific corp form many CIDs use
- `"Civil Code"` + `"Section 4"` (or §5/§6) — CA-only statute prefix
- `"Annual Policy Statement"` — Davis-Stirling §5310 disclosure (publicly
  posted by many HOAs)
- `"Annual Budget Report"` — §5300 disclosure

CA-specific publisher/host families to query via `site:`:

- `adamsstirling.com` — Adams Stirling PLC (largest CA HOA law firm; public
  knowledge base)
- `berdingweil.com` — Berding Weil legal updates
- `echo-ca.org` — Educational Community for Homeowners (Bay Area HOA assoc)
- `cacm.org` — California Association of Community Managers
- `caionline.org/Chapters` — CAI California chapter directories
- `davis-stirling.com` — Adams Stirling's statute walkthrough site (public
  CC&R links)

### 1c. CA top mgmt-co hosts (Driver C seeds)

Initial seed list (verify each via WebFetch before sweep):

- `fsresidential.com` — FirstService Residential California (largest in CA)
- `associa.us` — Associa
- `actionlife.com` — Action Property Management
- `seabreezemgmt.com` — Seabreeze Management
- `powerstonepm.com` — Powerstone Property Management
- `keystonepacific.com` — Keystone Pacific Property Management
- `euclidmgmt.com` — Euclid Management
- `heritageweci.com` — Heritage Western Communities
- `spectrumamg.com` — Spectrum Association Management
- `commonareas.com` — Common Areas
- `proactivepm.com` — ProActive Professional Management
- `walters-management.com` — Walters Management

CA portal hosts (state-agnostic but heavy CA usage):

- `vantaca.com`
- `frontsteps.com`
- `cinc.io`
- `smartwebs.net`
- `nabrnetwork.com` (CA condo high-rise common)
- `condocerts.com`

### 1d. County GIS layers (Phase 6 E1)

| County | Tract polygon source | Per-HOA polygon source |
|---|---|---|
| Los Angeles | LA County GIS — Tract Polygons | LA Assessor parcel `TRACT` field |
| Orange | OC GIS — Subdivision Tract Maps | OC Assessor parcel `TR_NUM` |
| San Diego | SD County SanGIS — Parcels w/ TRACT | SanGIS subdivision layer |
| Riverside | RivCo GIS — Subdivisions FS | RivCo Assessor `TRACT_NO` |
| San Bernardino | SBD GIS — Parcels w/ TRACT | SBD Assessor parcel layer |
| Alameda | Alameda County GIS — Subdivision boundaries | Alameda Assessor parcel |
| Santa Clara | SCC GIS — Subdivision Map index | SCC Assessor parcel |
| Sacramento | SacCounty GIS — Parcels w/ subdivision | SacCounty Assessor |

The other ~50 counties: defer until Phase 6 E1, fall back to E2/E3.

### 1e. CA recorder portals (Driver E, optional)

Most are gated; include only as last-resort enrichment. Free index search
exists in: LA RRCC, OC Clerk-Recorder, SD County Recorder, SF Assessor-
Recorder. Skip the other 54 for v1.

---

## 2. The 5-driver pattern (one more than giant-state's 4)

### Driver A — Registry-name × per-county Serper (highest precision)

Same as giant-state §2a. For each CID in the merged registry, generate
2 queries:

```
"<NAME>" "California" filetype:pdf
"<NAME>" "California" "declaration" OR "covenants" OR "CC&R"
```

Skip names < 12 chars or all-numeric. Strip suffixes (`INC`, `LLC`, `P.A.`,
`MUTUAL BENEFIT CORPORATION`) before quoting. Cap 1500/county. For 30k+ CIDs
across 15 top counties, ~30k queries.

**Script**: `state_scrapers/ca/scripts/ca_build_corp_county_queries.py`
(modeled on FL `fl_build_sunbiz_county_queries.py`).

### Driver B — Metro-stratified keyword Serper

Per top-15 county sweep with CA-specific patterns. ~17 county-level + 24
city-level patterns × N cities + CA host-family patterns.

**Hard rule (CA)**: cap `--max-queries` at 5000/county (effectively uncapped).
LA County alone has 88+ cities; queries for LA would be 88×24 = ~2100 city +
17 county = ~2117 total before host-family additions. Don't truncate.

**Script**: `state_scrapers/ca/scripts/run_ca_county_sweep.sh` (modeled on
FL `run_fl_county_sweep_v2.sh` but with CA-specific lexicon + hosts).

### Driver C — Mgmt-co host expansion

`site:<host>` × CA-specific terms across the 12-domain seed list.
~12 hosts × 7 patterns = 84 queries. Trivial spend (~$0.15) but
high yield because CA portals serve thousands of HOAs from one host.

**Script**: `state_scrapers/ca/scripts/ca_top_management_companies.py`
+ `benchmark/run_ca_mgmt_host_sweep.sh`.

### Driver D — DRE Subdivision Public Reports (CA-unique)

DRE's public lookup at `https://www2.dre.ca.gov/PublicASP/SubdivisionsPub.aspx`
exposes per-subdivision metadata; some include linked CC&Rs as exhibits.
Loop by county FIPS, paginate, fetch report PDFs, extract embedded CC&Rs.

This driver is **the giant-state playbook's missing tier** — it captures
new-construction CIDs that haven't yet shown up in the SI-CID biennial.

**Script**: `state_scrapers/ca/scripts/dre_subdivision_scrape.py` (new).

### Driver E — County recorder direct (optional, mostly shelved)

Same as giant-state §2d Driver D. Free index in LA RRCC, OC, SD; skip
the rest. Keep shelved unless bank coverage gap > 50% of CAI estimate.

---

## 3. Phase shape (what changes from giant-state)

### Phase 0 — Prerequisites
Same as giant-state §3a. Confirm:
- `HERE_API_KEY` (Phase 6 E2)
- `GOOGLE_APPLICATION_CREDENTIALS` (DocAI)
- `OPENROUTER_API_KEY` (validate-leads + Phase 10 LLM rename)
- `SERPER_API_KEY`
- 5–10 GB local disk for cadastral pulls

### Phase 1 — Dual-source registry parse + city/ZIP/county maps
- **Parse** `data/california_hoa_entities.csv` (34,938 candidates), filter
  to `status=1` (active) only.
- **Apply REJECT_NAME_RE**: mobile-home parks (Civ Code §799), Mello-Roos
  (CFD), garden clubs, etc. on top of the SoS pre-filter.
- **Merge legacy** `data/california/hoa_index.jsonl` + `hoa_details.jsonl`
  by name normalization. Legacy county tags are unreliable
  (e.g., "Riverside city → Orange county"); rebuild from authoritative
  Census place-county relationship.
- **Build `data/ca_zip_to_county.json`** (Census ZCTA crosswalk, FIPS=06).
- **Build `data/ca_city_to_county.json`** (Census place→county relationship,
  FIPS=06; fallback for rows without ZIP).
- **Build `data/ca_zip_to_centroid.json`** (Census ZCTA gazetteer for E4).
- **Output**: `data/ca_registry_hoas.jsonl`, deduped by `(name_norm,
  county)`.

Aim for ≥90% county-tagging from city or ZIP match. Untagged rows: leave
`county=null`; they get re-routed at Phase 5 OCR or Phase 7 prepare.

### Phase 2 — Discovery (4-driver replenisher)

Replenisher script (modeled on `run_fl_v2_replenisher.sh`) maintains
**N=3 → N=4** concurrent county sweeps. Queue ordered for **metro
rotation**:

1. LA Basin: Los Angeles, Orange, Ventura
2. Bay Area: Alameda, Santa Clara, San Francisco, Contra Costa, San Mateo
3. San Diego
4. Inland Empire: Riverside, San Bernardino
5. Sacramento metro: Sacramento, Placer
6. Central Coast: San Luis Obispo, Santa Barbara, Monterey
7. Central Valley: Fresno, Kern, San Joaquin

Run all 4 drivers' replenishers **in parallel**; bank dedups on
`(state, county, city, slug)`.

**Wall-time profile** (CA estimate):
- Driver B (broad): 15 counties × ~3h avg (parallelized N=4) → ~12h
- Driver A (registry-name): 15 counties × ~3h avg → ~12h with N=4
- Driver C (mgmt-host): single batch ~10 min
- Driver D (DRE): per-county pagination, ~10 min × 58 counties = ~10h

Total Phase 2 elapsed: ~24–36h with all four drivers parallelized.

### Phase 3 — Pre-OCR metadata repair
Same as giant-state §3d. CA-specific add: **slug-variant merge for `_ca/`
vs `CA/` prefix collisions** (some legacy code emitted lowercase state
prefixes; canonical is uppercase per `gs://hoaproxy-bank/v1/CA/`).

### Phase 5 — OCR (with budget cap)
**Budget envelope: $0.025/HOA average** (slightly tighter than FL's $0.03;
CA's PDF base rate of "text-extractable" is higher because more docs are
post-2010 vector-PDFs).

For 30k+ CA HOAs that's ~$750 max but expected actual ~$40-60.

**Same architecture as giant-state §3f**, plus CA-specific OCR slug-repair
patterns:
- `DECLARATION OF COVENANTS, CONDITIONS AND RESTRICTIONS for [NAME]`
- `BYLAWS OF [NAME] HOMEOWNERS ASSOCIATION`
- `MASTER DEED OF [NAME]`
- `[NAME], a California Mutual Benefit Corporation` ← CA-specific
- `Davis-Stirling Common Interest Development Act` ← CA-specific anchor
- Legal description: `Tract No. \d+` ← critical for Phase 6 E1
  (extracts the tract number for tract→polygon join)

**Phase 5b — state-mismatch reroute**: same pattern as
`fl_reroute_state_mismatches.py`. Expected reroute volume for CA: low
(50–100), since CA is the destination state for most cross-state OCR
mismatches, not the source.

### Phase 6 — 5-tier geometry stack (E0 added)

| Tier | Source | Geometry quality | Expected match rate |
|---|---|---|---|
| **E0** (CA-unique) | County tract polygon via assessor parcel join on extracted Tract No. | True per-HOA polygon | ~40% in top 8 counties |
| E1 | County GIS Subd_poly equivalent (LA, OC, SD have one) | Polygon when present | ~15% additional |
| E2 | HERE Geocoder address pass | Address-precision lat/lon | ~25% |
| E3 | OSM Nominatim place lookup | Polygon when OSM has it; place centroid otherwise | ~10% polygons + ~20% city-fallback |
| E4 | Registry city/ZIP centroid | City-centroid (~5 km blob) | Baseline ~85% |

**E0 is the CA-unique tier.** When OCR extracts `Tract No. NNNN` from a
CC&R, join NNNN against the county assessor parcel layer's tract field;
union all parcels with that tract → HOA polygon. This is the gold-standard
for CA because tract numbers are *recorded* (not inferred), so the join is
deterministic.

Run order:
1. **E4 first** (baseline; every manifest with a city or ZIP gets a
   centroid).
2. **E3** overnight (OSM Nominatim, 1.5s/req rate-limited; ~24h for
   30k manifests).
3. **E2** after E3 (HERE address-precision, free 30k/mo tier).
4. **E1** county Subd_poly (LA + OC + SD only first pass).
5. **E0** county-tract join (LA + OC + SD + RIV + SBD + ALA + SCL + SCC).

**Don't run two enrichments that both write `geometry` in parallel.**
E2/E3/E0/E1 all overwrite `geometry`; serialize them.

### Phase 5c — LLM content-grading gate (audit-mandated for CA)

**This phase did not exist in the giant-state playbook. CA mandates it.**

The 2026-05-09 quality audit
([`state_scrapers/_orchestrator/quality_audit_2026_05_09/FINAL_REPORT.md`](../state_scrapers/_orchestrator/quality_audit_2026_05_09/FINAL_REPORT.md))
sampled 80 live CA HOAs and graded **46 (57.5%) as junk** — the highest
junk rate of any state graded so far. CA junk is uniquely **diverse**, not
concentrated in one pattern like HI's DCCA biennial filings. Verified CA
junk categories from the audit:

- Government docs: city zoning resolutions, EBMUD utility agendas, county
  Board of Supervisors agendas, planning commission minutes
- Tax forms: IRS Form 990 returns, county tax delinquency notices
- Maps with no governing content (just polygon outlines + neighbor list)
- Newsletters, candidate questionnaires, pool inspection lists
- Court filings, hearings, lawsuits
- Wrong-HOA documents (CC&Rs filed under similar-name HOA elsewhere)
- Generic legal handbooks (statewide HOA law primer, not specific to the
  named HOA)
- Omnibus dumps (one CC&R + lots of unrelated fluff in the same PDF set)

**Why CA is uniquely junky.** The legacy CA pipeline used Google-Scrape
without strict name-anchoring; it banked any PDF whose body mentioned the
HOA name. So when an HOA was *referenced* in an unrelated city zoning
hearing or a county tax delinquency list, the legacy pipeline banked the
hearing/list as if it were a governing document.

**This pipeline avoids most of that** by name-anchoring queries
(`"<NAME>" "California" filetype:pdf`) at Driver A, but content grading
remains essential because:
- Driver B's broad city-level queries can still surface unrelated docs
- Mgmt-co host portals sometimes include omnibus PDFs

**Phase 5c implementation**:
1. After Phase 5 OCR completes, run an adapted version of
   [`scripts/audit/grade_hoa_text_quality.py`](../scripts/audit/grade_hoa_text_quality.py)
   against **bank manifests** (not live HOAs).
2. Per-manifest, fetch the OCR sidecar text (already produced in Phase 5).
3. DeepSeek-v4-flash grades verdict ∈ `{real, junk, no_docs, error}`.
4. Set `manifest.audit.content_grade = {verdict, category, reason,
   model, graded_at}`.
5. Phase 7 prepare-time filter: skip any manifest where
   `content_grade.verdict == "junk"`. These manifests stay in the bank
   for review but never drain to live.

Cost: ~$0.0002/HOA × ~25k bank entities = **~$5 OpenRouter spend**.
Wall-time: ~2-3h with `GRADER_RPS=3.0`.

**Critical**: this gate runs at **bank → drain** time, not at
**discovery → bank** time. Banking junk is fine (it's stored, dedup-able,
re-gradable later). Draining junk to live is what poisoned the legacy
1,101-CA list.

### Phase 7 — Prepare bundles (polygon + content-grade gates)

Two gates apply, in order:

1. **Content-grade gate** (Phase 5c): drop manifests with
   `content_grade.verdict in {"junk"}`. Approve `real` and `no_docs`
   (the latter become docless stubs per audit-restore pattern).
2. **Polygon-quality gate** (giant-state §3h): manifests with real
   polygons drain first
   (`geometry.confidence in {"tract-polygon", "subdpoly-polygon",
   "place-polygon"}`). City-centroid-only manifests drain in a second
   batch or wait for an E0/E1 upgrade.

### Phase 8 — Drain bank → /upload (75s pacing)

**Critical CA-specific concern**: at 5k+ live targets × 75s/upload =
**4.3 days of continuous upload**. The drain worker is the long pole;
it must run in background through the entire Phase 6 + 8 window.

Memory says: "75s gap between /upload calls; faster causes Render OOM
crashes." Honor strictly.

### Phase 10 — Close

LLM rename, hard-delete junk via `/admin/delete-hoa` (NOT `[non-HOA] `
tag — memory says tagging pollutes the live list), audit doc filenames,
write retrospective to `state_scrapers/ca/notes/retrospective.md`.

---

## 4. Wall-time and budget envelope (CA estimate)

| Phase | Wall time | Cost |
|---|---|---|
| 0 — prereqs | 1h | $0 |
| 1 — registry parse + maps | 2h | $0 |
| 2a — Driver A (N=4) | ~12h | ~$25 Serper |
| 2b — Driver B (N=4) | ~12h | ~$30 Serper |
| 2c — Driver C (mgmt host) | 10min | $0.15 Serper |
| 2d — Driver D (DRE) | ~10h | $0 |
| 3 — pre-OCR repair | 30min | $0 |
| 5 — OCR + slug repair + reroute | 8h | $40–60 DocAI |
| **5c — LLM content-grading gate** | ~2-3h | ~$5 OpenRouter |
| 6 E4 — city/ZIP centroid | 1h | $0 |
| 6 E3 — OSM Nominatim | ~24h | $0 |
| 6 E2 — HERE | 30min | $0 (within free tier) |
| 6 E1 — Subd_poly (3 cos) | 6h | $0 |
| 6 E0 — tract polygon (8 cos) | 12h | $0 |
| 7 — Prepare | 1h | $0 |
| 8 — Drain → /upload | **~4 days continuous** (background) | $0 (live ingest already paid) |
| 10 — Close + retro | 2h | ~$10 OpenRouter |

**Total wall-clock**: ~6–7 days, **gated by Phase 8 drain pacing**.
**Total spend**: **$110–160** (Serper $55 + DocAI $40-60 + OpenRouter $10
+ slack). Well under the $500 envelope.

---

## 5. Risks specific to CA

### 5a. Mobile-home parks
Civ Code §799 governs mobile-home parks distinctly from Davis-Stirling.
Reject at Phase 1 via REJECT_NAME_RE: `MOBILE HOME|MOBILEHOME|MHP|TRAILER
PARK|RV PARK|MANUFACTURED HOME`.

### 5b. Mello-Roos / Community Facilities Districts
CFDs look like HOAs in some directories but aren't. Reject:
`COMMUNITY FACILITIES DISTRICT|MELLO[- ]ROOS|CFD\b|ASSESSMENT DISTRICT`.

### 5c. Senior 55+ communities (Sun City, Leisure World, Rossmoor)
**Real CIDs with legal exemptions** (Civ Code §51.3). Don't filter — but
tag `audit.senior_community: true` so the live site can surface §51.3
disclosures correctly.

### 5d. Spanish-language CC&Rs
Rare but possible in border counties. DocAI handles fine; ensure
OCR-slug-repair regex is unicode-safe.

### 5e. SI-CID staleness (2-year max lag)
Driver D (DRE Subdivision Public Reports) is the freshness layer. Run it
even if SI-CID acquisition succeeds.

### 5f. Naming density (LA County alone has 50+ "Park Place"-style dups)
Bank dedup must use `(state, county, city, slug)`, not the giant-state
playbook's `(state, county, slug)`. Verify the bank's slug normalizer
includes the city before launching Phase 2.

### 5g. CA SoS suspended/dissolved CIDs that still have docs online
Filter `status=1` (active) at Phase 1 but **don't** discard suspended
entities — keep them in a separate `data/ca_registry_inactive.jsonl`
for a tertiary sweep if Phase 2 coverage falls short.

### 5h. Junk-content-grading is mandatory pre-drain
At 50%+ junk rate observed in the legacy live CA list (per audit
2026-05-09), Phase 5c content-grading is non-negotiable. Skipping it
means re-poisoning the live CA list immediately after the audit cleared
it. CA-specific junk is *diverse* (10+ distinct categories: government
docs, tax forms, zoning resolutions, maps, newsletters, court filings,
wrong-HOA, omnibus dumps, etc.) — filename regex catches some patterns
but only LLM content grading reliably catches all of them.

### 5i. /upload pacing as the hard wall
Memory says 75s/upload. **No faster.** Plan all timelines around this.
A 5k-HOA Phase 8 = 4.3 days minimum.

---

## 6. Per-state launch checklist (CA execution order)

1. **Confirm criteria** — done; CA is ~51k CIDs across 58 counties; matches.
2. **Phase 1** — parse `data/california_hoa_entities.csv` + merge legacy +
   build city/ZIP/county maps. Output `data/ca_registry_hoas.jsonl`.
3. **SI-CID acquisition** — best-effort; if blocked, proceed with corp
   dump + DRE.
4. **Build per-county query files** for top-15 counties, all 4 drivers.
5. **Launch parallel replenisher** at N=3, bump to N=4 after first batch
   shows clean exit codes.
6. **Watch for 24-36h.** Surface state-mismatch volume, slug-pollution
   patterns, RA fictitious-address patterns.
7. **Phase 5 OCR + slug-repair + Tract-No. extraction** with $0.025/HOA cap.
8. **Phase 5b reroute** — audit reroutes by destination state; CA expected
   to be net **destination** of cross-state mismatches, not source.
9. **Phase 6 5-tier geometry**: E4 → E3 → E2 → E1 → E0. Strict
   serialization; never two `geometry`-writing enrichments concurrent.
10. **Phase 7 prepare** with polygon-quality gate.
11. **Phase 8 drain → /upload** at 75s pacing in background. Plan for
    ~4 days continuous.
12. **Phase 10 close** — LLM rename, hard-delete junk, doc-filename audit,
    retrospective.

---

## 7. Doc status

- **First written:** 2026-05-09.
- **Reference state status:** in-progress (no completed retrospective yet;
  this doc IS the plan, will be amended with lessons after first-pass
  completion).
- **Companion playbooks:**
  - [`docs/giant-state-ingestion-playbook.md`](./giant-state-ingestion-playbook.md)
    — the generic giant-state framework. CA-specific adaptations live here.
  - [`docs/multi-state-ingestion-playbook.md`](./multi-state-ingestion-playbook.md)
    — small/medium states. The keyword-Serper-per-county foundation is
    reused as Driver B.
  - [`docs/name-list-first-ingestion-playbook.md`](./name-list-first-ingestion-playbook.md)
    — dense urban condo registries. Driver A here is conceptually a
    registry-anchored variant fused with Serper.
- **Open questions / blockers** (resolve as session continues):
  - SI-CID acquisition path — is the public bulk-download endpoint
    accessible, or is it gated to OPRA-style FOIA? Best-effort attempt;
    fall back to corp dump.
  - CA tract-polygon (E0) ambition — committed to 8 counties first pass.
  - Senior-community §51.3 disclosure surfacing on live site — defer to
    a separate ticket; tag at Phase 5 for now.
