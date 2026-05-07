# Florida HOA Discovery Handoff

State: **FL**
Started: 2026-05-05

## Why Florida is special

FL is the first state where we depart from pure Serper-driven county sweeps. Two FL-specific facts shape the approach:

1. **Sunbiz bulk corporation download.** The Florida Division of Corporations publishes the entire corporation registry as quarterly bulk files via public SFTP (`sftp.floridados.gov`, user `Public` / pwd `PubAccess1845!`). Files: `doc/quarterly/cor/cordata.zip` split into 10 shards (records ending in 0..9). Fixed-width 1440-char records; field map at https://dos.sunbiz.org/data-definitions/cor.html. We filter to `Status=A` nonprofits whose name matches HOA/condo/POA/master patterns and use that as a seed list to drive targeted Serper queries.

2. **HB 1203 (effective Jan 1, 2025).** Every FL HOA with 100+ parcels must publish governing docs to a website (or app). The mandated portal is private/password-gated, so HB 1203 doesn't directly create public URLs — but the rollout pushed thousands of HOAs onto common platforms (HOA-Express, HOA Sites, eNeighbors, FrontSteps, CINC Systems, gogladly, fsresidential, hmsft-doc) that often leak public pages. Skip walled-portal hosts (CINC, FrontSteps, AppFolio).

DBPR is **not** a useful registry — Chapter 720 explicitly leaves HOAs out of DBPR's regulatory scope (DBPR only handles condos/coops/timeshares/mobile homes).

## Scope reminder

"HOA" includes condos in this project — see CLAUDE.md and the playbook. FL has both Chapter 720 HOAs and Chapter 718 condominium associations; both are in scope and bank under the same `gs://hoaproxy-bank/v1/FL/{county}/{slug}/` layout.

## Bank coverage

| Snapshot | Manifests | Notes |
|---|---|---|
| 2026-05-05 start | 22 | All under GA county names (`bryan, carroll, chatham, dekalb, fulton, glynn, hall, henry, houston, paulding, walton`) — mis-routed cross-state from the GA pass. Cleanup pending. |

## Drivers

- **A — Sunbiz-name-driven Serper.** For each top-30 FL county, take that county's Sunbiz HOAs and run targeted `"<Name>" Florida documents bylaws filetype:pdf` queries.
- **B — Standard per-county sweeps.** Mirror of `run_ga_county_sweep_v2.sh` with FL counties + cities, architectural-anchored-on-Declaration queries.
- **C — Registered-agent host expansion.** Identify top 30 FL property management companies via Sunbiz registered-agent column, run `site:<domain>` Serper sweeps.

## Productive source families (carried forward from GA)

`img1.wsimg.com`, `static1.squarespace.com`, `nebula.wsimg.com`, `rackcdn.com`, `s3.amazonaws.com`, `fsresidential.com`, `eneighbors.com/p/`, `gogladly.com/connect/document/`, `inurl:hmsft-doc`, `inurl:/wp-content/uploads/`. Skip: `cincsystems.net`, `frontsteps.com` (walled).

## FL-specific blocked hosts (added to `_STATE_BLOCKED_HOSTS`)

`leg.state.fl.us`, `flsenate.gov`, `dos.fl.gov`, `myfloridalicense.com`, `flrules.org`, `flcourts.org`, `flgov.com`.

## OpenRouter spend

Starting budget: ~$8 of $20 cap remaining as of GA pass. User cleared additional Serper budget for FL.

## Log

- 2026-05-05: Investigated FL state-level data sources. Found Sunbiz bulk SFTP. Confirmed DBPR has no HOA registry. Decided on Drivers A/B/C plan. Updated CLAUDE.md and playbook to clarify "HOA" is condo-inclusive.
- 2026-05-05: Sunbiz Non-Profit quarterly download complete (43 MB → 10 × 30 MB fixed-width). Parser yielded 36,644 active FL HOAs/condos (`data/fl_sunbiz_hoas.jsonl`). 99% DOMNP filing type. Top registered agents: Sentry Mgmt 434, Specialty Mgmt 164, Vesta 126, Leland 106, Resort Mgmt 105, Associa Gulf Coast 86, Home Encounter 85.
- 2026-05-05: ZIP→county map built from Census 2020 ZCTA crosswalk + 14 synthetic prefix ranges → 1,987 FL ZIPs. 36,185 of 36,644 Sunbiz rows tagged (98.7%); the 459 nulls are out-of-state mailing/registered-agent addresses. Top counties by HOA count: miami-dade 4,708 / palm-beach 3,252 / broward 3,201 / hillsborough 2,169 / orange 2,091 / pinellas 2,074 / collier 2,001 / lee 1,865 / sarasota 1,230 / brevard 1,181.
- 2026-05-05: Mis-routed manifest cleanup complete. 11 of 22 recovered to correct FL counties (broward, st-johns, collier, palm-beach, alachua, clay, duval, seminole, walton-FL); 11 unrecoverable junk (garbled OCR names, regulatory boilerplate, attorney CVs) moved to `_unknown-county/`. `state_scrapers/fl/scripts/fl_repair_misrouted_manifests.py` is idempotent.
- 2026-05-05: Driver B launched in background (`run_all_fl_counties_v2.sh`). Started Miami-Dade with 34 cities, --max-queries 800, --max-leads 800. Expected ~17 min Serper + validate + clean + probe per county.
- 2026-05-05: Drivers A and C scaffolded but not yet run. A targets top 20 counties × Sunbiz HOA names (~30k queries, ~$30 Serper). C targets 9 verified management-company domains × 7 patterns = 63 queries, ~$1.50 Serper. Holding both until Driver B finishes its first county to avoid concurrent Serper hammering.
- 2026-05-05: Lifted `--max-queries` cap from 800 to 5000 in `run_fl_county_sweep_v2.sh` (effectively uncapped — no FL county's query file exceeds ~1100). Killed the in-flight Miami-Dade run + restarted from scratch with a small smoke-test on Sumter (The Villages) instead, since Sumter's ~190-query file completes the full pipeline in ~10 min and gives a faster validation gate on whether the FL pipeline works end-to-end.
- 2026-05-05: Investigated Miami-Dade County Recorder of Deeds (ORIPS spike). Old ASP.NET system is gone; replaced by React SPA at onlineservices.miamidadeclerk.gov. Blocker: reCAPTCHA v3 on all search endpoints + TOS restricts bulk storage without written permission or Developer API. Scaffolded `state_scrapers/fl/scripts/fl_miami_dade_recorder.py`. Phase 3 (banking) not executed. See "Miami-Dade County Recorder of Deeds — Spike Report" section above for full findings and recommendations.

## Miami-Dade County Recorder of Deeds — Spike Report (2026-05-05)

### What we investigated

Miami-Dade Clerk's Official Records search system (ORIPS):
- Old URL (www2.miami-dadeclerk.gov/officialrecords/StandardSearch.aspx) is **gone** — 404.
- New system is a React SPA at: `https://onlineservices.miamidadeclerk.gov/officialrecords/`

### ORIPS URL structure and parameter dictionary

**Document type endpoint (no auth, GET):**
```
GET https://onlineservices.miamidadeclerk.gov/officialrecords/api/home/documentTypes
```
Returns a JSON array of all instrument type strings, e.g. `"DECLARATION OF CONDOMINIUM  - DCO"`.

**HOA-relevant instrument codes:**
| Code | Full name |
|------|-----------|
| DCO  | DECLARATION OF CONDOMINIUM |
| RES  | RESTRICTIONS (CC&Rs / deed restrictions) |
| COV  | COVENANT |
| AIN  | ARTICLES OF INCORPORATION |
| PLT  | PLAT |
| DEE  | DEED |

**Standard search (POST, requires reCAPTCHA v3):**
```
POST https://onlineservices.miamidadeclerk.gov/officialrecords/api/home/standardsearch
     ?partyName=<url-encoded-name>
     &dateRangeFrom=<MM/DD/YYYY>
     &dateRangeTo=<MM/DD/YYYY>
     &documentType=<code>
     &searchT=<code>
     &firstQuery=true
     &searchtype=documentType
Headers:
  x-recaptcha-token: <Google reCAPTCHA v3 token>
  content-type: application/json; charset=utf-8
Response: {"isValidSearch": bool, "qs": "<string>|null"}
```

**Result pagination (GET, uses qs token from search):**
```
GET /officialrecords/api/SearchResults/getStandardRecords?qs=<qs-token>
```

**PDF image fetch (GET, uses book/page from result row):**
```
GET /officialrecords/api/DocumentImage/getdocumentimage
    ?sBook=<book>&sPage=<page>&sBookType=<booktype>&redact=false
GET /officialrecords/api/DocumentImage/getimagepaths?cfnMasterID=<id>
GET /officialrecords/api/DocumentImage/getEncryptedImagePath?cfnMasterID=<id>
```

**Developer API (requires registered account + AuthKey, avoids reCAPTCHA):**
```
GET https://www2.miamidadeclerk.gov/Developers/api/OfficialRecords/StandardSearch
    ?authKey=<key>&documentType=<code>&dateRangeFrom=<MM/DD/YYYY>&dateRangeTo=<MM/DD/YYYY>
```
Register at: https://www2.miamidadeclerk.gov/Developers/Home/MyAccount (305-275-1155)

### TOS verdict — RESTRICTED (written permission or Developer API required for bulk storage)

Exact clause from the system's JS-embedded terms of service:

> "Other than making limited copies of this website's content, you may not reproduce, retransmit, redistribute, upload or post any part of this website, including the contents thereof, in any form or by any means, **or store it in any information storage and retrieval system, without prior written permission from the Clerk and Comptroller's Office.**"
>
> "If you are interested in obtaining permission to reproduce, retransmit, or store any part of this website beyond that which you may use for personal use, as defined above, visit our Web API Services."

The Miami-Dade County Disclaimer (https://www.miamidade.gov/global/disclaimer/disclaimer.page) does not explicitly prohibit automated access but does prohibit unauthorized system alteration.

**robots.txt** (`www.miamidadeclerk.gov/robots.txt`): permits all crawlers, only disallows `/library/Sealed/`. No explicit crawl-delay or disallow on official records paths.

**Verdict:** Storing records in GCS (the bank) requires either (a) the Developer API license, or (b) written permission from the Clerk's office. The robots.txt doesn't block crawling, but the application-level TOS is the binding constraint.

### Blocker — Google reCAPTCHA v3 on all search endpoints

The standard search POST requires a live Google reCAPTCHA v3 token (site key: `6LfI8ikaAAAAAH0qlQMApskMGd1U6EqDyniH5t0x`). The server validates the token server-side. Empty/fake tokens return `{"isValidSearch": false, "qs": null}`. There is no plain-HTTP fallback.

The UI shows "Login to avoid reCaptcha" — a logged-in Clerk account session may bypass it, but we haven't tested this path.

### Whether the spike succeeded

**Phase 3 (bank a sample) was not executed.** The reCAPTCHA blocker prevents obtaining a `qs` token without either a real token or a Developer API key. The script exits cleanly with a diagnostic message. Phases 1 (investigation) and 2 (script scaffolding) are complete.

- Records fetched: **0** (blocked before any search)
- Records banked: **0**

### Slug quality assessment

Not testable without live records. However, the grantor field on Florida Declaration of Condominium and Restrictions instruments is typically the developer entity name (e.g. "SUNSET LAKES DEVELOPMENT LLC") or the association name (e.g. "SUNSET LAKES HOMEOWNERS ASSOCIATION INC"). The `slugify()` function strips "homeowners", "association", "inc", "llc" etc., so "SUNSET LAKES HOMEOWNERS ASSOCIATION INC" → slug `sunset-lakes`. Developer names would produce less clean slugs (e.g. "sunset-lakes-development"). A post-hoc name-repair pass would be needed for developer-named grantors.

### Estimated effort to scale to all 67 FL counties

Each FL county has its own recorder system. Miami-Dade's new React SPA is not typical — many smaller counties use older ASP.NET or Granicus/Tyler systems with different APIs. A rough survey:

- Miami-Dade: SPA + reCAPTCHA (investigated here)
- Broward: likely Tyler/iQS or similar — needs its own investigation
- Palm Beach: PBC public records portal — different system
- 64 remaining: each county is potentially bespoke

**Estimate: 1 person-month** for all 67 FL counties with working code. If reCAPTCHA/auth is solved for Miami-Dade, many counties may share similar patterns (e.g. Tyler Munis or Granicus), which could reduce to **1 person-week per county-cluster** once a pattern is established. Some counties (especially rural) may not have online search at all, requiring in-person FOIA requests.

### Whether instrument types map cleanly

The instrument type taxonomy is Miami-Dade-specific. DCO (Declaration of Condominium) and RES (Restrictions) are excellent, high-precision types — virtually all records of these types in a county recorder system are mandatory-association instruments. COV (Covenant) is slightly noisier (includes utility easement covenants etc.) but still high-signal. AIN (Articles of Incorporation) is noisier — many non-HOA nonprofits file articles. PLAT is too broad (all subdivisions, not just HOA-governed ones).

**Recommended instrument type priority:** DCO first (condos, zero noise), then RES (CC&R subdivisions), then COV with manual filtering if volume warrants.

### Blockers summary

1. **reCAPTCHA v3** on all search endpoints — primary blocker.
2. **TOS restriction** on bulk storage — requires Developer API or written permission.
3. **SPA architecture** — no stable GET URL for results; all state lives in server-side `qs` token.
4. **PDF redaction flag** — `redact=false` parameter works for full images; some documents may be redacted regardless.

### 3 recommendations

1. **Register for the Developer API first.** The path of least resistance: register at https://www2.miamidadeclerk.gov/Developers/Home/MyAccount, get an AuthKey, and test the `/Developers/api/OfficialRecords/StandardSearch` endpoint. This resolves both the reCAPTCHA blocker and the TOS question (the Developer API is the commercial data service the TOS points to). Cost is likely per-query "units" — evaluate pricing before committing to production scale.

2. **Do not prioritize the recorder path over Sunbiz Drivers A/B/C.** The recorder path is high-precision for instrument type but requires non-trivial auth setup and is county-by-county bespoke. Drivers A/B/C (Sunbiz + Serper) are already scaffolded and running. The recorder path is best as a late-stage supplement — once a county's standard Serper pass plateaus, the recorder becomes a high-confidence mopping-up source.

3. **When you do productionize, target DCO first.** Chapter 718 (condo) declarations are universally recorded and the DCO instrument type is zero-noise. A 1-year lookback (`dateRangeFrom` = 5 years ago) would capture most active FL condo associations. The grantor field on condo declarations is typically the association name, so slugs will be cleaner than for Chapter 720 HOAs (where the grantor is often the developer).

---

## Action plan when Sumter smoke test completes

When the Monitor fires `SUMTER_PROCESS_EXITED` (or earlier `banked=N` / `Traceback`):

1. **Inspect**: read `benchmark/results/fl_county_v2_Sumter/{validated.jsonl,cleaned.jsonl,probe.jsonl}` line counts; check `gsutil ls 'gs://hoaproxy-bank/v1/FL/sumter/' 2>/dev/null | wc -l` for new manifests.
2. **If banks > 0** (pipeline works):
   - Launch full Driver B in OS background: `bash benchmark/run_all_fl_counties_v2.sh` — runs all 36 counties starting from Miami-Dade. Will re-run Sumter at #36 (tiny cost; bank dedups via slug merge).
   - Launch Driver C in OS background concurrently: `bash benchmark/run_fl_mgmt_host_sweep.sh` — 63 queries × 7 patterns, ~3–5 min total.
   - After Driver B is ~5 counties in (~2 hours), launch Driver A: `bash benchmark/run_all_fl_sunbiz_counties.sh` — top 20 counties × Sunbiz HOA names. Stagger to avoid simultaneous DeepSeek validate calls.
3. **If banks = 0 but pipeline ran clean** (no Traceback): unusual; means Sumter genuinely had no banks despite The Villages. Still launch full Driver B since the pipeline is sound; investigate Sumter's Sunbiz / search results separately.
4. **If pipeline crashed**: debug the error (likely candidates: validator JSON parse, paramount cleaner regex on FL phrasings, probe robots.txt mismatch). Fix in code, retry Sumter, do not launch the full driver until the smoke passes.
5. **Always**: append per-county summary lines to this handoff doc as banks land. Watch for Serper / DeepSeek rate-limit signals in logs and back off if needed.
