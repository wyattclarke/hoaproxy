# HOAproxy Audit + Backfill Pipeline

Operational reference for the audit / scrub / backfill toolset added during the
2026-05-09 → 2026-05-10 quality run. Read alongside the historical incident
record at
[`state_scrapers/_orchestrator/quality_audit_2026_05_09/FINAL_REPORT.md`](../state_scrapers/_orchestrator/quality_audit_2026_05_09/FINAL_REPORT.md).

---

## Admin endpoints (added in this round)

### `POST /admin/clear-hoa-docs`
Delete documents + chunks for an HOA while **preserving** the `hoas` row,
its `hoa_locations` geometry, membership/proxy state, etc. Use whenever
content is junk but the entity is a real HOA worth keeping as a docless
stub.

```jsonc
// request
{ "hoa_ids": [123, 456], "dry_run": false }
// response
{ "dry_run": false, "cleared": 2, "would_clear": 0, "errors": 0,
  "results": [ { "hoa_id": 123, "name": "...", "status": "cleared",
                 "doc_count": 3, "chunk_count": 87 }, ... ] }
```

**Important:** the older delete-then-stub flow (`/admin/delete-hoa`
followed by `/admin/create-stub-hoas` with name/state/city only)
**cascades through `hoa_locations`** and silently drops
`latitude`, `longitude`, `boundary_geojson`, `street`, `postal_code`,
and `location_quality`. Never use that pattern for "real HOA, bad
content" cleanup — use `clear-hoa-docs`.

### `POST /admin/create-stub-hoas` — `on_collision` mode
Body now accepts `on_collision` ("skip" | "disambiguate") either at
body level (default for all records in the batch) or per-record.

- **`"skip"` (default)** — refuses any record whose `state` differs from
  a pre-existing same-name row. This is the bleed-stop guard from the
  2026-05-09 incident.
- **`"disambiguate"`** — for cross-state name collisions, route the
  record to a new row whose canonical name is `"{name} ({STATE})"` and
  whose `display_name` is the clean original. Use this for bulk
  registry imports where the same legal name registers in multiple
  states (e.g. "Lakewood Estates HOA").

```jsonc
{
  "on_collision": "disambiguate",
  "records": [
    { "name": "Acme HOA", "state": "TX", "city": "Austin" },
    { "name": "Acme HOA", "state": "IL", "city": "Chicago"
      /* will be created as "Acme HOA (IL)" */ }
  ]
}
```

Backed by `db.get_or_create_hoa_state_aware(conn, name, state)` (returns
`(hoa_id, canonical_name)`).

### `GET /admin/state-doc-coverage`
Per-state breakdown of HOAs with vs without documents, computed in one
SQL aggregation. ~10 ms regardless of DB size. Use to decide which
states need document discovery sweeps.

```jsonc
{
  "results": [
    { "state": "CA", "live": 25670, "with_docs": 431,
      "without_docs": 25239, "with_docs_pct": 1.68 },
    ...
  ],
  "totals": { "live": 174532, "with_docs": 7382,
              "without_docs": 167150, "with_docs_pct": 4.23 }
}
```

### `POST /admin/list-corruption-targets`
Returns `hoa_locations` rows whose `source` matches the supplied
strings, with full geometry + quality fields. Originally built for the
Pass A bbox repair; reusable for source-keyed cleanup, scrubs, and
audits.

```jsonc
// request
{ "sources": ["tx-trec-hoa-management-certificate", "fl-sunbiz"],
  "require_lat": false }
```

### `POST /admin/backup-full`
True `VACUUM INTO` snapshot of the live SQLite DB, uploaded to
`gs://hoaproxy-backups/db/`. Runs in a detached subprocess so the
request returns immediately. Use for ad-hoc snapshots before risky
operations — the cron `/admin/backup` only dumps the precious-tables
subset.

---

## Audit + cleanup scripts (`scripts/audit/`)

### Content-quality audit on live HOAs

1. **`grade_hoa_text_quality.py`** — for each live HOA in a state,
   pull chunk text via `/hoas/{name}/documents/searchable` HTML, ask
   DeepSeek-v4-flash (with Claude-Haiku-4.5 fallback) whether the
   content is a real HOA governing document or junk. Writes a per-state
   `_grades.json` with verdict + category + reason for each HOA.

   ```bash
   .venv/bin/python scripts/audit/grade_hoa_text_quality.py \
     --state CA --out state_scrapers/ca/results/audit_2026_05_09/ca_grades.json \
     --workers 2 --with-docs-only
   ```

   Read-only on live DB. Throttled by `GRADER_RPS` env (default 2.5).

2. **`clean_junk_docs.py`** — reads a `_grades.json`, classifies each
   `verdict==junk` entry as either `clear_docs` (real HOA name → call
   `/admin/clear-hoa-docs`) or `delete_entity` (name is a document
   fragment like "Stormwater Drainage Policy HOA" → call
   `/admin/delete-hoa`). Defaults to dry-run.

   Conservative bias: anything that doesn't match the explicit
   document-fragment regex (`JUNK_NAME_FRAGMENTS`) gets `clear_docs`.
   The entity stays as a docless stub; a future grading pass can
   re-attach docs or formally delete.

   ```bash
   .venv/bin/python scripts/audit/clean_junk_docs.py \
     --grades state_scrapers/co/results/audit_2026_05_09/co_grades.json \
     --apply
   ```

   **This replaces `restore_stubs.py`** for new content-cleanup work.
   `restore_stubs.py` is kept only as a recovery tool for entities
   already deleted by the old lossy flow (see its docstring).

### Bulk registry stub backfill

3. **`backfill_registry_stubs.py`** — bulk-import a state's
   authoritative HOA registry as docless stubs.

   - `REGISTRIES` dict at the top maps state-keys → seed JSONL paths
     (or `leads_glob` for multi-file county-GIS seeds) and default
     source strings.
   - `normalize_lead()` strips `location_quality` (avoids the
     2026-05-09 same-state-collision demotion bug) and always carries
     `postal_code` when the source has it.
   - `--on-collision` defaults to `disambiguate`. Pass `skip` if the
     batch shouldn't create cross-state disambiguated rows.
   - Reports `created` / `updated` / `disambiguated` / `skipped` and
     captures `skipped_samples` for inspection.

   ```bash
   .venv/bin/python scripts/audit/backfill_registry_stubs.py \
     --state MA --apply \
     --out state_scrapers/_orchestrator/quality_audit_2026_05_09/ma_backfill.json
   ```

4. **`retry_failed_batches.py`** — replays the `failed` array from a
   prior `*_backfill.json` outcome. Smaller per-batch size + longer
   sleep. Used to recover the ~5–10% of batches that hit transient
   Render 5xx during high-concurrency runs.

### Pre-import name screening (essential for county-GIS sources)

5. **`grade_entity_names.py`** — LLM-classify a sample of entity names
   from a state (or one specific source) as `hoa` / `subdivision` /
   `other` / `uncertain`. Use BEFORE running a bulk backfill to detect
   sources whose data is mostly plats and not HOAs.

   ```bash
   .venv/bin/python scripts/audit/grade_entity_names.py \
     --state OH --source-filter oh-hamilton-county-condo-polygons --sample 30
   ```

   Pattern observed on 2026-05-10: county GIS layers named
   `*_subdivisions` or `*_plats` were mostly bare subdivision /
   plat-page names with no mandatory HOA. Sources matching `*_condos`
   or `*_condo_*` were almost entirely real condo associations.
   Always sample-grade before bulk backfilling a new county source.

6. **`scrub_non_hoa_entities.py`** — for sources you've already
   backfilled that turn out to be mostly non-HOA, drop them via
   `/admin/delete-hoa` while snapshotting every row to
   `state_scrapers/{state}/leads/{state}_unverified_subdivisions.jsonl`
   first, so a future "find the HOA for this subdivision" pass can
   re-discover.

   Two modes:
   - `--mode delete-source` — drop every row tagged with the source.
     Use when sample-grading shows the source is mostly junk (e.g. MN
     LiveBy statewide subdivisions at 3% HOA rate).
   - `--mode grade-and-delete` — LLM-classify each name; only drop
     non-`hoa` rows. Use for mixed sources (e.g. WA Snohomish
     subdivisions at 37% HOA — keeps the named condo associations,
     drops the plats).

   ```bash
   .venv/bin/python scripts/audit/scrub_non_hoa_entities.py \
     --source mn-statewide-subdivisions-liveby --mode delete-source \
     --state MN --apply --ack-large
   ```

---

## Canonical workflows

### A. Content quality cleanup on a state
```
grade_hoa_text_quality.py --state XX  → XX_grades.json
clean_junk_docs.py --grades XX_grades.json --apply
```
Live count is unchanged; junk-content HOAs become docless stubs
(geometry preserved). Name-fragment entries fully deleted.

### B. Bulk-import a registry as docless stubs
```
1. Find a free public source (state SoS bulk, DORA registry, TREC
   filings, county GIS).
2. Build per-state seed JSONL at
   state_scrapers/{state}/leads/{state}_*_seed.jsonl with shape
   {name, state, city, postal_code, metadata_type, source, source_url}.
3. grade_entity_names.py --state XX --source-filter <source>  to
   sample-grade the names.
   - If ≥70% "hoa" → proceed to backfill.
   - If 30–70% → backfill, then scrub_non_hoa_entities.py
     --mode grade-and-delete.
   - If <30% "hoa" → either skip entirely, or backfill + immediate
     delete-source scrub (preserves the list for future HOA-discovery).
4. Add source to REGISTRIES dict in backfill_registry_stubs.py.
5. .venv/bin/python scripts/audit/backfill_registry_stubs.py --state XX
   --apply --out .../{state}_backfill.json.
6. retry_failed_batches.py for any failed batches.
```

### C. Decide where to scrape next
```
curl /admin/state-doc-coverage
```
Sort by `live` desc, prioritize high-`live` low-`with_docs_pct`
states for `state_scrapers/_orchestrator/namelist_discover.py`
document-discovery sweeps.

---

## Hard rules (learned the painful way)

1. **`/admin/delete-hoa` cascades.** Don't use it followed by
   `/admin/create-stub-hoas` to "refresh" a row — that strips
   `latitude`, `longitude`, `boundary_geojson`, `street`,
   `postal_code`, `location_quality`. Use `/admin/clear-hoa-docs`
   instead.

2. **`/admin/create-stub-hoas` bulk imports must pass
   `on_collision: "disambiguate"`.** The `"skip"` default exists as a
   safety guard for one-off uploads and silently drops cross-state
   collisions.

3. **Subdivisions / plats ≠ HOAs.** County GIS `*_subdivisions` and
   `*_plats` layers are mostly recorded land subdivisions with no
   mandatory HOA. Always run `grade_entity_names.py` before backfilling
   a new county source.

4. **Never reuse an existing `source` string.** Source strings are the
   primary key for `/admin/list-corruption-targets` and future scrubs.
   Pick a fresh one per source. The full list of strings burned during
   the 2026-05-09–10 run is in the FINAL_REPORT. New imports should
   pick clearly-distinct strings.

5. **`/admin/backfill-locations` and `/admin/create-stub-hoas` both
   COALESCE every column.** Passing `location_quality: "city_only"`
   against a row that already has `"address"` quality silently
   demotes it. Either omit the field (let it stay NULL) or pass a
   quality you've already verified is at least as good as what's
   there.

6. **Before any bulk backfill, snapshot.** Run `POST /admin/backup-full`
   to get a `gs://hoaproxy-backups/db/hoa_index-{ts}.db` snapshot.
   The cron `/admin/backup` only dumps precious tables (~10 KB) and
   does NOT capture the full DB. As of 2026-05-10 the most recent
   full snapshot before the audit was from April 15.
