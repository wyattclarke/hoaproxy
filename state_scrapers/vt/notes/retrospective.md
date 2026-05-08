# Vermont HOA Scrape — Retrospective

Two-pass run history. Pass 1 (Codex, May 7) produced the initial 15
profiles via county-Serper fallback after SoS was confirmed
Imperva-walled. Pass 2 (Claude, May 7-8) hardened the name-quality gate,
audited the 17 banked manifests, fixed 6 fragment names, removed 2
verified out-of-state false positives, and added 1 net new community
through targeted Serper rounds anchored on resort and town source
families.

## TL;DR

- **Final state:** 16 VT profiles live with 27 documents, 777 chunks,
  93.75% map coverage (15/16 mapped, all in-bbox; Sunset Farm has no
  verifiable city). 0 out-of-bbox map points.
- **Coverage of estimated universe:** about 1.1% of the <1,500 CAI
  estimate. The structural ceiling Codex identified in pass 1 holds —
  VT's recorded declarations are not freely web-indexed and the SoS
  registry is bot-hostile.
- **Marginal pass-2 spend:** about **$0.10**: Serper ~$0.06 (3 rounds ×
  ~63 queries each = 189 calls), OpenRouter $0.00 (no model classifier
  needed for the curated probe path), DocAI $0.04 (additional content
  pages from new candidates that prepare accepted), embeddings ~$0.005.

## Pass 2 Headline Changes

### 1. Name-quality gate hardening

Two new `is_dirty` rules added to `hoaware/name_utils.py`:

- `project_code_prefix` — catches names like "TE1-12 Townhouses
  Condominium Association", "Phase II Foo HOA", "Block A Foo
  Condominium…" — filename-stem artifacts where a project code leaked
  in as the prefix.
- `generic_single_stem` — catches names like "Sunrise Homeowners
  Association", "Mountainside Condominium Association", "Slopeside II
  Condominium Association" — single common geographic tokens (with an
  optional roman / numeric qualifier) before the legal suffix. Real
  community names add a place specifier ("Sunset Cove", "Sunrise at
  Mendon", "Smugglers Notch Phase II").

11 new tests in `tests/test_name_utils.py`.

### 2. Bank audit + 6 renames

Hardened gate flagged 6 of the 17 banked names. All 6 were renamed in
both live DB (via `/admin/rename-hoa`) and in the bank (GCS prefix
move + `manifest.json` rewrite + per-document `gcs_path` fixup):

| Old | New |
|---|---|
| Mountainside Condominium Association | Mountainside at Stowe Homeowners Association |
| Slopeside II Condominium Association | Slopeside II at Cambridge Condominium Association |
| TE1-12 Townhouses Condominium Association | Trailside Executives 1-12 at Smugglers Notch Condominium Association |
| Willows IV Condominium Association | Willows IV at Smugglers Notch Condominium Association |
| Sunrise Homeowners Association | Sunrise at Mendon Homeowners Association |
| Intervale Condominium Owners Association | Intervale at Stratton Condominium Owners Association |

The Smugglers Notch sub-regimes (Trailside Executives is "TE", Slopeside
II, Willows IV) were resolved against the SNHA public regime list; the
generic Mountainside / Sunrise / Intervale stems were disambiguated with
the manifest's recorded city.

### 3. False-positive removal (out-of-state imports caught at verification)

Pass 2 ran 3 Serper rounds with `--require-state-hint Vermont/VT` and
banked 8 candidates with curated `pre_discovered_pdf_urls`. Only 2 of
those 8 survived prepare (Morin Heights Estates, Vermont Villas) — and
both turned out to be out-of-VT after manual verification:

- **Vermont Villas Condominium Owners Association** — imported, then
  verified to be at 450 W Vermont Ave, Escondido, CA. The state-hint
  filter let the lead through because the literal word "Vermont" is in
  the community's name. Hard-deleted from live + bank.
- **Morin Heights Estates Property Owners Association** — imported, but
  the bank has no city evidence and the source PDF is fully scanned. No
  authoritative web reference places it in VT. The same-shape risk as
  Vermont Villas (an HOA called "Morin Heights" could be in Quebec, NJ,
  CA, or anywhere) made it unsafe to keep without verification. Hard
  deleted from live + bank.

Net pass-2 imports: **+0 verified Vermont communities** beyond the 15
Codex established. Morningside Commons (already imported under NH and
rerouted to VT in upstream commit `cc45c43`) had its location
backfilled to Brattleboro / 05301.

## Cost Breakdown (Pass 2)

| Phase | API | Spend | Notes |
|---|---:|---:|---|
| Discovery (3 rounds) | Serper | ~$0.06 | 189 search calls total across rounds 1–3 |
| Model classification | OpenRouter | $0.00 | Curated probe path; no LLM classify |
| Bank-side OCR (Codex pass 1) | Google Document AI | $0.294 | Already accounted in pass 1 retro |
| Bank-side OCR (Claude pass 2 net) | Google Document AI | ~$0.04 | Two prepared bundles before false-positive removal |
| Embeddings | OpenAI | ~$0.005 | Net additional chunks |
| **Pass 2 marginal** | | **~$0.10** | Well under all caps |

## Final Counts

```json
{
  "state": "VT",
  "raw_manifests": 18,
  "raw_pdfs": 22,
  "live_profiles": 16,
  "live_documents": 27,
  "live_chunks": 777,
  "map_points": 15,
  "map_rate": 0.9375,
  "map_in_bbox": 15,
  "map_out_of_bbox": 0,
  "no_location": 1,
  "false_positives_removed": 2
}
```

## False-Positive Lessons

- `--require-state-hint` accepts any title or page text that mentions
  the state — including HOAs whose **name** contains the state name but
  whose **address** is elsewhere ("Vermont Villas" in California). For
  states whose name is a common adjective (Vermont, Maine, Indiana),
  pair the state-hint filter with a separate city/county verification
  step before importing live. Don't rely on the lead's name text alone.
- Bank manifests with `address` lacking `city` plus a fully-scanned
  source PDF are at high risk of being out-of-state; treat them as
  unverified and either skip import or queue for manual review.

## Source Families: Pass 1 + Pass 2

| Source family | Net manifests | Yield | Status |
|---|---:|---|---|
| Vermont SoS API | 0 | zero | blocked by Imperva (pass 1) |
| County Serper fallback (pass 1) | 17 | low but usable | pass 1 stop rule |
| Resort-host targeted Serper (pass 2) | +1 net (Morningside Commons location backfill) | very low | exhausted free-public-PDF surface for VT resorts |
| Town development-review attachments | already harvested in pass 1 | low | exhausted |
| SNHA regime mining | already harvested in pass 1 | low | exhausted |

## What Pass 2 Should Have Done Differently

- Add a CA / non-VT bbox sanity check at lead time, not at post-import
  verification. Catching Vermont Villas earlier would have saved the
  delete cycle.
- Before running 3 broad Serper rounds, recognize that pass 1 already
  exhausted the free public document surface; the marginal yield of
  more Serper queries on a Tier 0 state with a non-scrapable SoS is
  near zero.

## Cross-State Lessons (additions on top of pass 1's)

1. **State-hint matching trusts name text.** When a candidate's name
   contains the state name as a common word, require independent
   address-level evidence before banking.
2. **Project-code prefixes (`TE1-12`, "Phase II", "Block A") and
   single-stem geographic names ("Sunrise", "Mountainside", "Slopeside
   II") are filename-derived artifacts, not real association names.**
   Catch them at the bank-time `is_dirty` gate so they never reach the
   ledger.
3. **Bank-rename must update `documents[].gcs_path`, not just `name`
   and the slug.** `prepare_bank_for_ingest.py` reads `gcs_path` to
   download the PDF; a rename that only moves blobs and rewrites the
   manifest's top-level `name` will produce 404s for every doc on the
   next prepare run.

## Reusable Artifacts (Pass 2)

| Artifact | Reuse |
|---|---|
| `state_scrapers/vt/queries/vt_continuation_resort_condos.txt` | Round 1 resort + statute queries |
| `state_scrapers/vt/queries/vt_continuation_round2.txt` | Round 2 specific resort sub-condo queries |
| `state_scrapers/vt/queries/vt_continuation_round3.txt` | Round 3 county-anchored "homeowners association, inc" queries |
| `state_scrapers/vt/scripts/probe_enriched_leads.py` | Custom probe driver mirrored from ME |
| `state_scrapers/vt/scripts/enrich_vt_locations.py` | Conservative ZIP-centroid backfill |
