# IL Source-Family Inventory

Per the new playbook rule (`docs/multi-state-ingestion-playbook.md` "Intent
And Success Framing"): every plausible source family considered for IL,
marked productive / sterile / untried-with-reason. The retrospective
cross-references this file. Updated incrementally across sessions; **NOT
exhausted yet** — session 3 is in progress.

Scope of this inventory: **non-Chicagoland IL** (Wave A — Cook + 6 collar
counties — is the parallel session's territory and is excluded from every
row below). Two sister inventories exist conceptually: this one (downstate)
and the Chicagoland session's (not present in this repo).

## Universe anchor

- IL CAI estimate: **~19,750** (per `docs/cai_state_hoa_counts.txt`).
- Realistic non-Chicagoland share: ~7,000–10,000 (Cook alone holds ~5,000+
  via the Cook County Recorder; downstate metros + long-tail counties hold
  the rest).
- **Session 2 ended at 103 non-Chicagoland live (~1.5% of downstate
  universe).** Insufficient. Target for session 3: ≥500 (≥7% of downstate
  universe), constrained by the budget envelope below.

## Budget envelope (sessions 2 + 3 combined)

- DocAI: $50 cap, ~$1 used through session 2. Plan to use $30–45 in session
  3.
- Serper: $9 cap, < $1 used through session 2. Plan to use $5–7.
- OpenRouter: $6 cap, ~$1.4 used through session 2. Plan to use $3–5.

## Source families (productive / sterile / untried)

### Tier 1 — keyword-Serper per-county (default pattern)

| Source | Status | Yield | Notes |
|---|---|---|---|
| Wave A: 7 Chicagoland counties | **out-of-scope** | n/a | Owned by parallel session |
| Wave B: 10 downstate metros, round 1 | **productive** | +91 net live, 264 manifests | Single sweep with playbook-style queries + IL-specific `-site:` exclusions. See `state_scrapers/il/queries/il_{winnebago,sangamon,...}_serper_queries.txt` |
| Wave B: round 2 with city-anchored variants | **scheduled, session 3** | TBD | Per playbook two-sweep stop rule. None of round-1 tripped stop thresholds |
| Wave C-1: 5 long-tail counties (DeKalb/LaSalle/Macon/Williamson/Adams), round 1 | **productive** | +14 net live, 26 prepared | DeKalb strongest; LaSalle/Adams noisy (court opinions, county board minutes) |
| Wave C-2: 14 remaining long-tail counties | **scheduled, session 3** | TBD | Vermilion, Knox, Effingham, Coles, Marion, Boone, Grundy, Stephenson, Whiteside, Henry, Lee, Ogle, Jackson, Franklin |
| Wave D: rest of IL's 102 counties (the small ones not on any list) | **untried-with-reason** | — | Long-tail of <500 households per county. Estimated yield <2 HOAs each per playbook stop-rule curves. Not worth Serper budget |

### Tier 2 — Source-family promotion (playbook Phase 2)

| Source | Status | Yield | Notes |
|---|---|---|---|
| HOA-owned WordPress sites with `/wp-content/uploads/*.pdf` | **scheduled, session 3** | TBD | Confirmed productive in session 2 leads: foxpoint.org, www.111echestnut.org, neuhavenhoa.com, www.wheatlandshoa.org, www.willowwalkpalatine.org, www.erjames.com, www.millerchicagorealestate.com (+others). Direct mining bypasses Serper |
| Squarespace `/s/` aliases | **untried** | — | Not yet identified in IL leads; check during session 3 |
| `inurl:/DocumentCenter/View` (municipal) | **partial productive** | unknown | Hit count not separated from per-county logs; will quantify in session 3 |

### Tier 3 — Aggregators / directories / registries (name-list-first hybrid)

| Source | Status | Yield | Notes |
|---|---|---|---|
| CAI-IL Chapter directory (cai-illinois.org) | **declined** | 0 | Member directory is paywalled / "printed only" / requires CAI National login (caionline.org). Service Directory page is mgmt-co only, no associations. ~$0 yield to public scraping. Per playbook §9: declined, written reason |
| HOA-USA Illinois pages (hoa-usa.com/management-directory/?state=Illinois) | **declined** | 0 | Lists ~80 mgmt companies serving IL but **zero specific association names**. Mgmt-co follow-up handled via Tier 5 below |
| Illinois Condo Connection | **untried-with-reason** | — | Coverage unclear; likely Chicago-only based on naming. Tier 5 below covers this niche |
| IDFPR licensed Community Association Manager (CAM) list | **untried-with-reason** | — | Lookup URL guesses returned 404. The CAM license public-lookup form requires session-stateful POST with a captcha; not scriptable in available time. Skipped |
| IL Secretary of State business entity search | **declined** | — | Single-name search only; "database may not be used to copy or download bulk information searches" per ToS. Same failure mode as IN INBiz |

### Tier 4 — Per-county Recorder of Deeds direct probes

| County | Status | Yield | Notes |
|---|---|---|---|
| Cook County Recorder (cookrecorder.com / cookcountyclerkil.gov) | **out-of-scope** | n/a | Chicagoland session's territory. First-pass retro flagged this as "likely 10× the yield of keyword Serper for Cook" |
| Sangamon County Recorder | **declined** | — | Online access via Tapestry/Laredo (third-party) is subscription-paywalled. Free public access is on-site terminals only. Not scriptable |
| Madison County Recorder | **declined** | — | Subscription paywall: $30/hour or $70/3-hour. Not scriptable |
| McLean County Recorder | **declined** | — | eSearch online portal returns IIS default page (broken or session-gated); RECORDhub portal listed but coverage unclear |
| Champaign County Recorder | **untried-with-reason** | — | Probable same paywall pattern as peer counties. Skipped without dedicated access |
| Winnebago County Recorder | **untried-with-reason** | — | Same as above |
| Other downstate recorders | **declined** | — | Tapestry / Laredo / Fidlar third-party paywall is the dominant pattern across IL. Not pursuable without subscription |

### Tier 5 — Management company portfolios

| Source | Status | Yield | Notes |
|---|---|---|---|
| FirstService Residential downstate offices | **declined** | — | FSR's IL footprint is Tinley Park, Elk Grove Village, Libertyville (all Chicagoland). No Springfield/Decatur/Peoria/QC offices. Chicagoland session's territory |
| Associa Illinois downstate | **declined** | — | Same Chicagoland-only footprint |
| ACM (Association of Condominium Managers) member listings | **out-of-scope** | n/a | Chicagoland-anchored |
| Sudler / Foster Premier / Vanguard / Habitat / Lieberman / Heil/Smart/Golee / RealManage / Inland | **out-of-scope** | n/a | Chicago-based |
| Per-domain owned-site direct PDF mining | **active, session 3** | TBD | 31 productive HOA-owned + small-municipal + developer domains being mined via `state_scrapers/il/scripts/mine_owned_domains.py`. Pure direct-PDF source-family promotion per playbook Phase 2 |

### Tier 6 — Investigated but declined

| Source | Decision | Reason |
|---|---|---|
| Cook County GIS Condo Approval Lots | **declined** | Chicagoland session's territory; equivalent to DC's name-list-first registry pattern but Cook-only |
| Public Nominatim for full-state geocoding | **declined** | 19.8% Nominatim 429 rate in first pass per IL retrospective bullet 5; using ZIP-centroid only |
| Statewide Serper without county anchor | **declined** | Playbook Phase 2 invariant: "Bare statewide Serper produces noise" |
| Gemini / Qwen Flash for classification | **declined** | Per `HOA_DISCOVERY_MODEL_BLOCKLIST` and playbook line 360 |

## Status legend

- **productive**: tried in a prior session, yielded ≥3 net new live HOAs, may be re-tried with widened anchors
- **sterile**: tried in a prior session, yielded <3 net new (tripped two-sweep stop rule)
- **scheduled**: planned for the current/next session
- **untried-with-reason**: not tried; reason documented
- **out-of-scope**: belongs to the parallel Chicagoland session
- **declined**: investigated and rejected (e.g. paywall, robots.txt, model blocklist)

## Cross-reference

- Session 1 retrospective: `state_scrapers/il/notes/retrospective.md` (first-pass)
- Session 2 retrospective: `state_scrapers/il/notes/retrospective-downstate.md` (Wave B + Wave C-1)
- Session 3 retrospective: TBD (this session, in progress)
- Source-family inventory v1 (this file): updated as session 3 progresses
