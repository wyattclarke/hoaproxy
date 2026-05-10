# HOA Quality Audit + Coverage Backfill — 2026-05-09 → 2026-05-10

## What this run did

Three workstreams, in order:

1. **Content-quality audit and cleanup.** LLM-graded every live HOA's
   banked text (DeepSeek-v4-flash + Claude-Haiku fallback for empties)
   to decide whether the document content was a real HOA governing doc
   or junk (state filing receipts, biennial registrations, newsletters,
   wrong-state PDFs, etc.). Across 30 states + sample-graded 19 more.

2. **Authoritative-registry stub backfill.** For 20 states where free
   public registries exist, bulk-imported the universe of HOA/condo
   entities as docless stubs via `/admin/create-stub-hoas`. ~180,000
   stubs created or upserted across:

   | State | Source |
   |---|---|
   | FL | Sunbiz dump |
   | CA | CA SoS bizfile bulk corp dump |
   | TX | TREC HOA Management Certificate registry |
   | NY | NY DOS Active Corporations |
   | CO | DORA HOA Information Office |
   | OR | OR SoS Active Nonprofit Corporations |
   | CT | CT SoS condo associations |
   | HI | DCCA AOUO Contact List |
   | IL | Cook County Assessor (Chicagoland only) |
   | RI | RI SoS associations |
   | AZ | Pima County GIS HOA layer (Tucson only) |
   | MA | MassGIS L3 parcels owner-name extraction (statewide) |
   | MN | Statewide LiveBy subdivisions (Socrata) |
   | MO | Springfield Greene County subdivisions |
   | WA | Snohomish County subdivisions |
   | OH | Hamilton + Stark + Delaware county GIS |
   | VA | Fairfax + Loudoun + Henrico + Chesterfield + Stafford county GIS |
   | MD | Baltimore + Harford county GIS |
   | NC | Wake + New Hanover county GIS |
   | MI | Kent + Kalamazoo county GIS |

3. **2026-05-09 incident remediation + bug-fix work** (sibling session
   handled the urgent repair; this session finished the long-tail
   structural fixes once Pass B cleared):
   - Pass A: bbox + HERE reverse-geocode repaired ~8,851 corrupted rows
     across the 11 first-wave audit sources.
   - Pass B: re-geocoded 1,147 deleted-then-stubbed entities from bank
     manifests + HERE postal-code geocoding.
   - New `/admin/clear-hoa-docs` endpoint (this session) deletes
     documents+chunks while preserving `hoas` + `hoa_locations` rows
     and their geometry — replaces the lossy delete-then-stub flow that
     caused the 2026-05-09 corruption.
   - New `/admin/create-stub-hoas` `on_collision: "disambiguate"` mode
     (this session) creates `"{name} ({STATE})"` rows when the same
     legal name registers in multiple states, instead of silently
     clobbering the prior state's row.
   - New `db.get_or_create_hoa_state_aware()` underlies the
     disambiguation logic.

## Live count change

| | Before | After |
|---|---|---|
| Total live HOAs | ~20,700 | **193,539** |

Per-state gain across the 9 second-wave states:

| State | Pre | Post | Gain |
|---|---|---|---|
| MN | 40 | 24,925 | +24,885 |
| OH | 159 | 9,970 | +9,811 |
| VA | 177 | 9,727 | +9,550 |
| WA | 134 | 8,988 | +8,854 |
| MD | 77 | 7,566 | +7,489 |
| MI | 107 | 5,475 | +5,368 |
| NC | 1,072 | 6,136 | +5,064 |
| MA | 34 | 4,753 | +4,719 |
| MO | 119 | 3,449 | +3,330 |
| **Total** | **1,919** | **80,989** | **+79,070** |

Plus ~109,000 from the 11 first-wave states earlier in the run.

## Verification — bbox-pollution sweep (post Pass A + post 2nd-wave)

180,707 rows tagged with audit/backfill sources surveyed. **4 cross-state
bbox-pollution rows remaining**, all in `audit_2026_05_09_restored_stub`
(Pass B leftover). Net of the original ~114 cross-state rows we found
right after the 2026-05-09 incident, that's 96.5% repaired and the
remaining 4 are in the source Pass B is still finalizing.

The disambiguate-on-collision logic in the patched `/admin/create-stub-hoas`
prevented any new corruption. Across ~80K new stubs from the second-wave
backfill, ~2,350 cross-state name collisions were detected and routed to
separate `(STATE)`-suffixed rows instead of silently overwriting prior
states' data.

## Per-state content audit results (highlights)

| State | Graded | Real | Junk | Junk rate | Action |
|---|---|---|---|---|---|
| HI | 614 | 79 | 535 | 87% | Pass B re-geocoded |
| RI | 196 | 34 | 162 | 83% | Pass B re-geocoded |
| CT | 230 | 98 | 132 | 57% | Pass B re-geocoded |
| CA | 1,082 | 422 | 641 | 59% | Handed off to sibling session |
| CO | 505 | 294 | 196 | 39% | clear-hoa-docs (69 cleared, rest stale IDs) |
| MT | 146 | 101 | 45 | 31% | Pass B re-geocoded |
| GA | 989 | 726 | 261 | 26% | Pass B re-geocoded |
| OK | 134 | 103 | 23 | 17% | Pass B re-geocoded |
| MD | 77 | 69 | 8 | 10% | clean_junk_docs ran |
| (other states) | various | various | <10 each | <8% | Pass B re-geocoded |

Note: CA's audit is being handled by a sibling session that was already
doing CA-specific work; full handoff message is in this session's
transcript.

## Reusable scripts (canonical paths)

| Path | Purpose |
|---|---|
| `scripts/audit/grade_hoa_text_quality.py` | LLM-graded content quality audit (read-only on live DB) |
| `scripts/audit/clean_junk_docs.py` | **Going-forward content cleanup**: real-HOA junk → `clear-hoa-docs` (preserve entity); name-fragment → `delete-hoa` |
| `scripts/audit/restore_stubs.py` | **Deprecated** — recovery-only for delete-then-stub flows that lost geometry |
| `scripts/audit/delete_junk_hoas.py` | Lower-level batch delete (used by historical audit, no longer the right starting tool) |
| `scripts/audit/backfill_registry_stubs.py` | Bulk import from per-state registries; default `--on-collision disambiguate` |
| `scripts/audit/retry_failed_batches.py` | Replay failed batches from a backfill outcome JSON |
| `scripts/audit/build_alt_state_seeds.py` | Sister-session ETL that produced the 2nd-wave county-GIS seeds |

## What remains as known-future-work

- **NJ**: still blocked. NJ MOD-IV redacts owner names statewide;
  parcel-mining doesn't yield HOA matches. Documented in
  `state_scrapers/nj/leads/REGISTRY_NOTES_v2.md`.
- **AZ outside Tucson, IL outside Chicagoland, NC outside the 2 covered
  counties**: county-GIS coverage is partial; would benefit from more
  county-by-county pulls.
- **Mecklenburg County (NC)**: server resolves but every query times
  out at 60s+ from this region; another 10–15K Charlotte-area entries
  would unblock from a different IP.
- **Document discovery for the new docless stubs**: ~180K stubs are
  registered HOAs with no governing docs. Running
  `state_scrapers/_orchestrator/namelist_discover.py` against each
  state's seed file would surface real CC&Rs / bylaws for the subset
  whose docs are publicly indexed.
- **Re-grade the 5 CO HOAs the LLM still couldn't grade** (and the 17
  similar CA stubborn-error rows); both lists are in their respective
  state's grade JSON under `verdict=="error"`.

## Reference paths

- Per-state grade JSONs: `state_scrapers/{state}/results/audit_2026_05_09/{state}_grades.json`
- Per-state delete + restore outcome JSONs: same folder, `_delete.json` / `_restore.json`
- Backfill outcome JSONs: `state_scrapers/_orchestrator/quality_audit_2026_05_09/{state}_backfill.json`
- Stub restore outcome: `state_scrapers/_orchestrator/quality_audit_2026_05_09/restore_stubs_outcome.json`
- 2nd-wave seeds: `state_scrapers/{state}/leads/{state}_*_seed.jsonl`
- 2nd-wave notes: `state_scrapers/{state}/leads/REGISTRY_NOTES_v2.md`
- Cached bulk source CSVs: `data/{state}_*.csv` / `data/{state}_*.jsonl`
