# IL Discovery Handoff

State: Illinois · Tier 3 · CAI ~19,750 · Mode: keyword-Serper-per-county
with Wave-0 directory probe and host-family + statute-anchored sweeps.

## Strategy

Cook + DuPage dominate the universe. Discovery proceeds in three waves:

- **Wave 0 — directories first.** Probe Cook/Chicago/Illinois aggregator
  directories before spending Serper. Goal: seed the bank with deterministic
  name lists and identify productive host families before per-county sweeps.
  Targets:
  - CAI Illinois Chapter directory (cai-illinois.org)
  - IDFPR community association manager license search (idfpr.illinois.gov)
  - Cook County Recorder of Deeds search (cookrecorder.com)
  - Cook County Clerk subdivision name index
  - Major Chicago-area mgmt cos: FirstService Residential, Foster Premier,
    ACM, Sudler, Vanguard, Habitat, Lieberman, Heil/Heil/Smart/Golee,
    RealManage, Property Specialists, Inland — scrape rosters where public
  - HOA-USA / HOA-Talk Illinois pages
  - Illinois Condo Connection / similar condo directories

- **Wave A — Chicago metro (high-density).** Per-county Serper sweeps for:
  Cook, DuPage, Lake, Will, Kane, McHenry, Kendall.
  Cook query file augmented with city-anchored variants (Chicago, Evanston,
  Oak Park, Schaumburg, Arlington Heights, Palatine, Hoffman Estates,
  Tinley Park, Orland Park, Skokie, Des Plaines, Cicero) because Cook
  governing docs more often anchor on city/village than on "Cook County".

- **Wave B — downstate metros.** Winnebago, Sangamon, Champaign, Peoria,
  McLean, St. Clair, Madison, Rock Island, Tazewell, Kankakee.

- **Wave C — long tail (deferred).** Remaining ~85 counties only after A/B
  saturate; tighter stop rule.

## Cost ceilings (per user, 2026-05-08)

- DocAI: $150 (Tier 3 default)
- Serper: $6 (raised from $3 default given Cook volume)
- OpenRouter: $8 (raised from $5)
- Stop with partial retro on first cap hit.

## Run-id format

`il_{YYYYMMDD_HHMMSS}_claude` (e.g. `il_20260508_180000_claude`).

## IL-specific gotchas

- "Illinois" / "Chicago" / "Cook County" are common-token rejects when
  used alone for name overlap scoring.
- Condo-heavy state — do NOT downweight "condominium" / "condo" in
  name matching; that's where Cook's universe lives. Master Deed and
  Declaration of Condominium Ownership are core IL phrasings under
  765 ILCS 605.
- Trust-form titles ("Condominium Trust") less common in IL than MA/NH
  but worth keeping.
- Will County straddles into IN (Lake/Porter); Lake County straddles
  into WI (Kenosha); rely on cross-state re-routing in clean step.
- Cook County Recorder is now the Cook County Clerk Recordings dept —
  doc index lives at cookrecorder.com / cookcountyclerkil.gov.

## Generic-token rejects for scoring

`condominium` / `association` / `Illinois` / `Chicago` / `Cook` —
require ≥1 specific (non-generic) name-token overlap to keep a candidate.

## Bank counts (running)

- Initial (pre-run): see `gsutil ls gs://hoaproxy-bank/v1/IL/...` snapshot
  in preflight.json.

## Sources attempted

(populated as run progresses — log every directory probed, every host
family promoted/demoted, every false-positive pattern blocked)

## Next branches

(populated as run progresses)
