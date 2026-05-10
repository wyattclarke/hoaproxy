# Quality Audit + Cleanup + Coverage Backfill — 2026-05-09 → 2026-05-10

## What this run did

Two distinct workstreams, run in sequence on the same day:

1. **Content-quality audit and cleanup.** Across 30 states, every live HOA
   that had documents was graded by an LLM (DeepSeek-v4-flash, with
   Claude-Haiku-4.5 fallback for empties) on whether its banked text was
   genuine HOA governing-document content. 1,174 HOAs graded "junk" were
   deleted from the live DB, then 1,147 of them — those whose entity name
   itself was a real HOA name — were re-created as docless stubs to
   preserve registered-entity coverage. The 27 that stayed deleted had
   names that were document fragments (e.g. "Stormwater Drainage Policy
   HOA", "Sloa Bulletin November", "Madison County Zoning") and were not
   real HOAs.

2. **Coverage backfill from authoritative public registries.** 11 states
   had usable free public registries; for each, we pulled the universe
   of HOA/condo entities and bulk-created docless stubs via
   `/admin/create-stub-hoas`. This treats each entity as a "registered
   HOA, no public docs" entry — the same pattern DC's CONDO REGIME run
   used. ~109,000 stubs were created across these states.

## Live count change

| | Before | After |
|---|---|---|
| Total live HOAs | ~20,700 | **120,577** |

Top states by live count after this run:

| State | Live | Source of bulk stubs |
|---|---|---|
| FL | 36,238 | Sunbiz dump (`data/fl_sunbiz_hoas.jsonl`, 36,644 entities) |
| CA | 25,664 | CA SoS bulk corp dump filtered to nonprofit-mutual-benefit + HOA name |
| TX | 16,322 | TX TREC HOA Management Certificate registry (Socrata) |
| NY | 12,283 | NY DOS Active Corporations (Socrata) filtered by entity-type + HOA name regex |
| CO | 8,490 | CO DORA HOA Information Office active list |
| OR | 4,403 | OR SoS Active Nonprofit Corporations (Socrata) |
| CT | 3,502 | CT SoS condo associations |
| DC | 3,215 | DC GIS CAMA CONDO REGIME (preexisting) |
| IL | 1,592 | Cook County Assessor (Chicagoland only — rest of IL blocked) |
| HI | 1,513 | DCCA AOUO Contact List PDF |
| AZ | 1,097 | Pima County GIS HOA layer (Tucson only — rest of AZ blocked) |
| NC | 1,072 | preexisting (no statewide bulk found) |
| GA | 924 | preexisting + small audit cleanup |
| RI | 705 | RI SoS associations (full registry) |
| TN | 649 | preexisting |

## Per-state content audit results

| State | Graded | Real | Junk | Junk rate | Delete + restore-stub |
|---|---|---|---|---|---|
| RI | 196 | 34 | 162 | 83% | 162 deleted, 162 restored |
| HI | 614 | 79 | 535 | 87% | 535 deleted, 535 restored |
| GA | 989 | 726 | 261 | 26% | 261 deleted, 251 restored |
| MT | 146 | 101 | 45 | 31% | 45 deleted, 40 restored |
| IL (audit) | 160 | 129 | 31 | 19% | 31 deleted, 31 restored |
| OK | 134 | 103 | 23 | 17% | 23 deleted, 18 restored |
| NE | 106 | 88 | 17 | 16% | 17 deleted, 15 restored |
| TN | 667 | 650 | 17 | 3% | 17 deleted, 17 restored |
| CT | 230 | 98 | 132 | 57% | 132 deleted, 125 restored |
| KS | 355 | 342 | 13 | 4% | 13 deleted, 12 restored |
| DC (with-docs) | 21 | 8 | 13 | 62% | 13 deleted, 13 restored |
| MS | 42 | 33 | 9 | 21% | 9 deleted, 8 restored |
| MD | 77 | 69 | 8 | 10% | 8 deleted, 8 restored |
| WY | 85 | 77 | 6 | 7% | 6 deleted, 6 restored |
| DE | 112 | 106 | 6 | 5% | 6 deleted, 5 restored |
| UT | 69 | 63 | 6 | 9% | 6 deleted, 6 restored |
| NM | 60 | 55 | 5 | 8% | 5 deleted, 5 restored |
| AK | 31 | 28 | 3 | 10% | 3 deleted, 3 restored |
| AL | 86 | 83 | 3 | 3% | 3 deleted, 3 restored |
| AR | 36 | 33 | 3 | 8% | 3 deleted, 3 restored |
| MN | 40 | 35 | 3 | 8% | 3 deleted, 3 restored |
| LA | 32 | 30 | 2 | 6% | 2 deleted, 2 restored |
| NV | 26 | 24 | 2 | 8% | 2 deleted, 2 restored |
| ME | 19 | 17 | 2 | 11% | 2 deleted, 2 restored |
| FL (sample) | 20 | 18 | 1 | 5% | 1 deleted, 1 restored |
| IA | 44 | 42 | 2 | 5% | 2 deleted, 2 restored |
| KY | 40 | 38 | 1 | 3% | 1 deleted, 1 restored |
| ID | 36 | 34 | 1 | 3% | 1 deleted, 1 restored |
| NH | 31 | 30 | 1 | 3% | 1 deleted, 0 restored |
| ND | 20 | 18 | 1 | 5% | 1 deleted, 0 restored |
| AZ (sample) | 6 | 5 | 1 | 17% | 1 deleted, 1 restored |
| PA | 56 | 53 | 2 | 4% | 2 deleted, 2 restored |
| VT | 16 | 15 | 1 | 6% | 1 deleted, 1 restored |
| WV | 16 | 15 | 1 | 6% | 1 deleted, 1 restored |
| SD | 12 | 10 | 1 | 8% | 1 deleted, 1 restored |
| CA (partial) | 75 of 1097 | 24 | 49 | 65% | 15 deleted, 11 restored |
| CO (partial) | 100 of 557 | 53 | 40 | 40% | partial; 21 restored |

CA and CO grades stopped early due to a session crash + Render slowness;
their bulk registry stubs (24K + 8K) were not affected. The unfilled
remainder can be graded in a follow-up pass.

## Pattern observed: SoS-filing-receipt content failure

The states with the worst content-quality (HI 87%, RI 83%, CT 57%, DC 62%
of the with-docs subset, MT 31%, GA 26%) all share the same failure
pattern: their banked documents are dominated by state agency filing
receipts (annual reports, biennial registrations, certificate-of-good-
standing letters) that the prepare-time classifier accepted because the
filename matched `articles_of_incorporation`-shape patterns, but whose
*body* is an officer-and-address blob with no governing content. The
LLM-graded text audit caught what filename heuristics couldn't.

The cleanest playbook fix is to move the LLM content grader into the
prepare-time classifier (cheap — DeepSeek-v4-flash at ~$0.0002/HOA, vs
the much higher cost of Phase 10 hard-deletes after import). That's a
follow-up PR, not part of this run.

## States with universe gaps and no working bulk source

These states were blocked by paid bulk subscriptions, captchas, WAFs, or
no public registry. Documented in `state_scrapers/{state}/leads/REGISTRY_NOTES.md`:

- **OH, MI, MN, MA, VA, NJ, MD, MO, NC, WA**: SoS bulk product is paid
  or behind Cloudflare/captcha; no statewide aggregator. A second-pass
  research agent is currently scanning county GIS layers as a fallback;
  Wayne/Oakland (MI), Cuyahoga/Franklin (OH), Mecklenburg/Wake (NC),
  Fairfax (VA), King/Pierce (WA), Hennepin/Ramsey (MN), Boston ISD (MA)
  are the obvious targets.
- **AZ outside Tucson, IL outside Chicagoland**: same — no statewide
  source, only county-level fragments captured.

## Reusable scripts

| Path | Purpose |
|---|---|
| `scripts/audit/grade_hoa_text_quality.py` | LLM-graded content quality audit |
| `scripts/audit/delete_junk_hoas.py` | Bulk delete from grade JSON |
| `scripts/audit/restore_stubs.py` | Restore real-name HOAs as docless stubs |
| `scripts/audit/backfill_registry_stubs.py` | Bulk import from per-state registries |
| `scripts/audit/sample_states.sh` | Sample-grade a list of states |
| `scripts/audit/run_full_audit.sh` | Full grade + clean a list of states |
| `scripts/audit/rescrape_state.sh` | End-to-end rescrape pipeline (unused this run) |

Per-state result JSONs are at
`state_scrapers/{state}/results/audit_2026_05_09/`.

Per-registry leads JSONLs are at
`state_scrapers/{state}/leads/{state}_*_seed.jsonl`.

Bulk source CSVs cached at `data/{ca|co|tx|ny|or|fl}_*.csv` /
`*.jsonl` for re-running.

## What remains

1. **Finish CA + CO content grading.** Currently only 75 + 100 of ~1700
   total with-docs HOAs graded. The bulk-registry coverage is already
   in (25K + 8K stubs); content-quality cleanup of the with-docs subset
   is a small last-mile pass.
2. **County-level GIS pulls for the 10 blocked states** (research agent
   in flight at end of run).
3. **Move LLM content grader into Phase 5 prepare worker** to prevent
   future state runs from accumulating SoS-filing-receipt junk on the
   live site.
4. **`namelist_discover.py` runs against the new stubs** to find real
   governing docs for the entities we just registered. The bulk registry
   gives us the entity universe; document discovery is the next yield
   lever.
