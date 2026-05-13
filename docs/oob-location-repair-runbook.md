# Out-of-bbox (OOB) Location Repair Runbook

**Goal:** For every state, find HOAs whose `hoa_locations.latitude/longitude`
lands outside the state's bbox. Try to repair via OCR-text address extraction
+ HERE Geocoder; demote to `city_only` (with `clear_coordinates=true`) only if
HERE cannot place the entity inside the bbox.

**Scope notes:**
- This pass fixes the **OOB centroid** problem (sponsor / mailing-address /
  registered-agent ZIPs leaking into property coords). It does **not** fix
  in-bbox-but-wrong centroids (subdivision plats and similar mass imports
  that landed on city centroids inside the bbox). Those are a different
  cleanup — `scrub_non_hoa_entities.py --mode delete-source`.
- This pass is **location-only**. It does not rename HOAs, delete entities,
  or touch documents.

---

## Hard rules (don't violate)

1. **Don't touch in-bbox rows.** The OOB check uses a `0.05°` padding (~3.5
   mi N–S, ~4 km E–W at 40° N) so legitimate ZIP-centroid hits in
   adjacent-state ZIPs don't get false-positive demoted.
2. **Skip rows where `location_quality == "polygon"`.** A polygon row with
   an OOB centroid is a *bad polygon* — escalate to a manual investigation,
   don't demote the geometry. Log them but do not write.
3. **`/admin/backfill-locations` COALESCEs every column.** When demoting,
   always pass `location_quality: "city_only"` *and* `clear_coordinates:
   true` *and* `clear_boundary_geojson: true`. When repairing, write the
   new lat/lon + `location_quality: "address"`, but keep city/state/postal
   as-is (don't pass them — let COALESCE preserve the originals).
4. **Anchor-aware OCR address extraction.** First-in-state-match is brittle
   (NY sponsor LLC at Manhattan law-firm address gets picked over the
   actual Suffolk-County property). Prefer addresses near "premises",
   "subject property", "located at", "known as", "commonly known as".
   Deprioritize addresses near "attorneys", "counsel", "law office",
   "registered agent", "prepared by", "returned to".
5. **HERE call cap.** Pre-flight tally first; refuse to start the
   write-pass without an operator-supplied (or default `5000`) cap. Stop
   on first HTTP 429.
6. **Snapshot the DB first** via `POST /admin/backup-full`. Record the
   resulting blob path in the ledger header.
7. **Append-only ledger, one fsync per row.** A mid-state 429 must leave
   resumable state.

---

## Architecture

The repair pass extends `scripts/enrich_locations_from_ocr_here.py`
(reuses its `STATE_FULL`, `here_geocode`, `STREET_TYPES`, `CITY_STATE_ZIP_RE`,
`ZIP_RE`, `best_record`, `live_admin_token`). New code lives in
`scripts/audit/repair_oob_locations.py`.

### Inputs

- **State bboxes:** `state_scrapers/_meta/state_bboxes.json` — all 51
  jurisdictions (50 states + DC). Padding is **not** pre-applied; the
  script adds it at read time.
- **Sources:** union of (a) every `source` string in `REGISTRIES`,
  (b) every `source` string observed in `state_scrapers/*/leads/*.jsonl`,
  (c) `gcs_prepared_ingest`, and (d) the burned 2026-05-09 source list.
  Stored as a `KNOWN_SOURCES` constant in the script.
- **HERE_API_KEY:** loaded from `settings.env`.
- **Admin token:** `HOAPROXY_ADMIN_BEARER`, falling back to Render env-var
  lookup, falling back to local `JWT_SECRET`.

### Modes

- `--mode tally` — count OOB rows per state. Writes
  `state_scrapers/_orchestrator/oob_repair_{date}/tally.json` and prints
  projected HERE budget. **Never writes** to the live DB.
- `--mode repair` — execute the per-state repair pass. Refuses to run
  unless `tally.json` exists and an explicit `--max-here-calls N` is
  passed. Stops on first HTTP 429.

### Per-state flow (mode=repair)

For each state, in descending order of OOB count:

1. Fetch all rows from `/admin/list-corruption-targets` for that state's
   relevant sources (`require_lat=true`). Filter to
   `state == STATE AND lat is not None`.
2. Bucket each row:
   - **in_bbox** → skip.
   - **out_of_bbox AND location_quality == "polygon"** → log to ledger
     with `decision: "manual_review_polygon"`, no write.
   - **out_of_bbox AND location_quality != "polygon"** → candidate for
     repair.
3. For each repair candidate:
   - Fetch OCR via `/hoas/{name}/documents` → list of docs;
     then `/hoas/{name}/documents/searchable?path={rel}` per doc (cap at
     5 docs, ~5000 chars total). Strip `<pre>` blocks.
   - Run `extract_address_candidates_anchored(...)` — returns ranked
     candidates with anchor scores.
   - For each candidate (top-3 max), call HERE. Accept iff:
     - `resultType in {"houseNumber", "street"}`
     - `address.stateCode` matches target state
     - new `(lat, lon)` is in padded state bbox
4. Decide:
   - **HERE in-state hit:** POST `/admin/backfill-locations` with
     `{hoa, latitude, longitude, street, location_quality: "address"}`.
     Don't pass city/state/postal/`source`.
   - **No valid hit:** POST `/admin/backfill-locations` with
     `{hoa, location_quality: "city_only", clear_coordinates: true,
       clear_boundary_geojson: true}`.
5. Append one ledger row per HOA, fsync after each:
   `{ts, hoa_id, hoa, state, source, old_lat, old_lon,
     old_quality, ocr_address, here_address, new_lat, new_lon,
     decision: "repaired"|"demoted"|"skipped_polygon"|"skipped_in_bbox",
     reason}`.

### Rate limits

- HERE: 4 req/s (250 ms gap)
- `/admin/backfill-locations`: 25-record batches, 0.5 s sleep
- `/hoas/{name}/documents/searchable`: 1 req per 2.5 s per HOA (24/min);
  but parallelism is 1 (sequential per state) so this is the
  per-HOA cap not a per-IP throttle

### Output

- Per-state ledger:
  `state_scrapers/{state}/results/oob_repair_2026_05_12/oob_repair_ledger.jsonl`
- Per-state summary:
  `state_scrapers/{state}/results/oob_repair_2026_05_12/summary.json`
- Orchestrator summary:
  `state_scrapers/_orchestrator/oob_repair_2026_05_12/summary.json`
  (totals + per-state breakdown)

---

## Pre-flight checklist

1. `HERE_API_KEY` exists in env; smoke-test with a Times Square query.
2. `HOAPROXY_ADMIN_BEARER` (or `JWT_SECRET`) resolves.
3. `GET /admin/state-doc-coverage` returns 200.
4. `state_scrapers/_meta/state_bboxes.json` covers all 51 jurisdictions.
5. `POST /admin/backup-full` succeeds; record blob path.
6. Run `--mode tally`. Inspect `tally.json`. Decide cap. **Stop here if
   the projected HERE-call count exceeds your monthly remaining quota.**

## Skip thresholds

- Skip states with `live < 100` *or* `with_docs < 50` in
  `/admin/state-doc-coverage` — the OCR-extract path has nothing to chew
  on for tiny states. For those, a future `demote_oob_*_locations.py`
  pass (no OCR, just demote) is the right tool.

## Resume semantics

On restart with the same `--run-id` (default `oob_repair_2026_05_12`),
the script reads the existing per-state ledger and skips any
`hoa_id` already present. HERE cache (`data/here_geocode_cache.json`)
survives restarts. The tally is not re-run.

---

## Known issues / lessons from the 2026-05-13 first run

1. **`/hoas/{name}/documents` latency.** During the live demote pass this
   endpoint averaged 3–8 s; during the immediate retry it climbed to
   30–180 s (and Cloudflare-524'd on some names). The `fetch_hoa_ocr_text`
   timeout is now `240 s` for `/documents` to absorb the worst case.
   Schedule retry passes off-peak (overnight ET) or accept long wall
   time. Root-cause is likely SQLite contention from the bulk
   `/admin/backfill-locations` writes — a future pass should `sleep 60`
   between the demote phase and any HERE-repair phase.

2. **Inherited regex bug in `scripts/enrich_locations_from_ocr_here.py`.**
   That script's `CITY_STATE_ZIP_RE` was written with literal `{{1,40}}`
   instead of `{1,40}` and has never matched a single OCR address. The
   repair script overrides the regex locally — but the enrich script
   itself is still broken and should be fixed in a follow-up. The reason
   the enrich script appeared to work earlier is that its three other
   candidate patterns (HOA-name + manifest city, ZIP + state coarse
   fallback) produce coarse hits that often geocode to *something*
   plausible-but-imprecise.

3. **`retry-demoted` ledger semantics.** `retry_ledger.jsonl` is
   load-and-skip just like the primary ledger. If you re-run after a
   failed retry, move the stale retry ledger aside first (e.g.
   `mv retry_ledger.jsonl retry_ledger_attemptN_*.jsonl`) so the second
   attempt actually re-processes the rows. The script does not detect
   stale entries; this is intentional (resume semantics) but tripped
   the first 2026-05-13 follow-up.
