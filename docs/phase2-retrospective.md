# Phase 2 retrospective — pending_ingest worker cutover

*2026-05-11 — written immediately after the cutover succeeded. Updates
should be appended below as the FL drain progresses.*

## What shipped

1. **`pending_ingest` SQLite queue** (`hoaware/db.py`) — idempotent
   migration + atomic claim-by-UPDATE pattern. Survives parallel-worker
   scaling whenever we move past one process.
2. **`hoaware/ingest_worker.py`** — polling drain loop, SIGTERM-clean
   shutdown, heartbeat logging, 3-attempt retry → dead-letter, scheme
   dispatch for `gs://` (prepared GCS bundles) and `local://` (upload
   sidecars).
3. **Co-located worker** (`scripts/start_web_with_worker.sh`) — runs the
   FastAPI web server AND the worker as sibling processes in one Render
   container. Each owns its own Python heap; the heavy OCR + embedding
   RAM spike in the worker can't OOM the request-serving process. The
   75s `/upload` pacing memo is now obsolete.
4. **Route bifurcation** — `/upload`, `/upload/anonymous`, and
   `/admin/ingest-ready-gcs` all check `ASYNC_INGEST_ENABLED` and either
   run the legacy synchronous path or enqueue and return
   `{queued: true, job_id, status_url}` (HTTP 200, treat as 202).
5. **Queue inspection** — `GET /ingest/status/{job_id}` (public,
   rate-limited), `GET /admin/ingest/queue-stats`,
   `POST /admin/ingest/retry-dead` (admin).
6. **Scrape protection L1 + L3** — 60 req/IP/hour rate limit and
   `Cache-Control: public, max-age=86400, immutable` on
   `/hoas/{hoa}/documents/file`; new `scripts/gcp_egress_cap/` Cloud
   Function `stop-gcs-egress` deployed on `gcs-egress-budget-alerts`
   Pub/Sub topic, unlinks billing at $200/day GCS spend. L2 (Cloudflare
   Worker) script is in `docs/scrape-protection.md` — operator paste
   step.
7. **Cutover playbook** — `docs/phase2-cutover.md`. The single big gotcha
   we hit: Render's `PATCH /env-vars/{key}` returns 405; the correct
   single-var endpoint is `PUT /env-vars/{key}` (which is *not* the
   memory rule's footgun — that's the full-collection `PUT /env-vars`).

## Tests

- `tests/test_ingest_worker.py` — 10 tests covering queue CRUD, claim
  ordering, retry/dead-letter state machine, scheme dispatch, unknown
  scheme rejection. All pass.
- `tests/test_async_upload.py` — 8 tests covering /upload async shape,
  /admin/ingest-ready-gcs async path, duplicate prevention, status/retry
  endpoints, and the sync-fallback path under flag-off. All pass.
- Full suite: 18 net-new tests passing. The 3 pre-existing test failures
  and 43 setup errors in `tests/test_proposals.py` are unrelated
  test-pollution between modules — confirmed by checking out the
  pre-Phase-2 commit `a07c754` and seeing the same numbers.

## Deploy chronology (UTC)

- 03:17:31 — first Phase-2 deploy (flag still off). Worker boots
  cleanly, heartbeat shows empty queue. No regressions on existing
  routes.
- 03:18:16 — smoke test of `/admin/ingest-ready-gcs?state=FL&dry_run=true`
  returns `{async: False, found: 0}` (legacy shape preserved when flag
  is off). ✓
- 03:39:27 — SIGTERM to old worker; clean shutdown in 2s.
- 03:39:48 — new worker boots after env-var-trigger redeploy.
- 03:46:23 — `ASYNC_INGEST_ENABLED=1` PUT applied + deploy triggered.
  Took ~80s to go live.
- 03:46:27 — first end-to-end smoke `/upload` returns
  `{queued: true, job_id: 3fef85b2..., status_url: /ingest/status/...}`.
- 03:46:30 — job 3fef85b2... `status=done`, drain latency 3s (most of
  that was the worker's 2s poll cycle).
- 03:46:44 → 03:47:09 — batch of 9 more uploads, all drained in <250ms
  each. Total 10/10 sample jobs succeeded.

## Verified outcomes

| Check | Status |
|---|---|
| `/healthz` 200 after flag flip | ✓ |
| Worker `ingest_worker heartbeat counts={...}` every 30s | ✓ |
| `/upload` returns `{queued: true, job_id, status_url}` shape | ✓ |
| `/admin/ingest-ready-gcs?state=X` returns `{async: true, enqueued: N}` | ✓ |
| `/ingest/status/{job_id}` returns 404 for unknown, 200 for live | ✓ |
| 10 sample jobs imported successfully | ✓ |
| Legacy state-scraper exit-check `count(.results)==0` still works | ✓ (results array is empty when found=0 in both modes) |
| `kill -TERM` → worker finishes job + exits in <2s | ✓ |
| No new errors in prod logs post-cutover (excl. nominatim rate-limit) | ✓ |

## FL drain status

Relaunched `/tmp/fl_phase7_10_runner.sh` (also versioned at
`state_scrapers/_orchestrator/fl_phase7_10_runner_async.sh`) at
03:54:56Z with bumped limit (50 instead of 5) and dropped the 75s
pacing. As of the time of this writing, FL is still in Phase 7
(`prepare_bank_for_ingest`); the drain hasn't started yet because there
are zero ready bundles in `gs://hoaproxy-ingest-ready/v1/FL/`. The
async path will absorb the bundles whenever prepare completes them.

Final FL pending/done counts go here once Phase 8/9/10 finish.

## What's NEW in scaling-proposal.md territory

Two design decisions worth folding back into `docs/scaling-proposal.md`:

1. **Render disks are bound to one service — no shared-disk multi-service
   pattern available.** This is why the Phase 2 worker is co-located in
   the web container rather than its own `type: worker` Render service.
   When Phase 4 (Neon Postgres) lands, splitting the worker off
   becomes trivial (worker connects to Postgres over the network,
   stops needing the SQLite disk at all). The proposal's "+$25/mo
   Background Worker" line item turns into "+$0 (process in existing
   container) + (+$25/mo when split off in Phase 4)" — net cost
   unchanged at full 10x scale.
2. **Bulk-patch needed on existing state-scraper run_state_ingestion.py
   scripts.** 36 state scripts had a hardcoded
   `1 for r in results if (r.get("status") or "").lower() == "imported"`
   counter that breaks under async mode (results are job_ids, no
   `status` field). Loop *exit* was already correct (uses `found==0`),
   but stats reporting was misleading. Bulk-fixed in commit b3866e9.
   Future async-shape changes should add a regression test that loads
   all state-scraper scripts and asserts they import cleanly.

## What's still TODO

- **Operator: deploy Cloudflare Worker (L2).** Script is in
  `docs/scrape-protection.md`. Needs DNS + Worker bind to
  `documents.hoaproxy.org/*`. ~30 min one-time.
- **Operator: wire GCP egress Budget alert + Pub/Sub topic (L3).** Topic
  `gcs-egress-budget-alerts` and Cloud Function `stop-gcs-egress` are
  already deployed; the Budget itself needs to be created via GCP
  Console with $50 soft + $200 hard thresholds.
- **Optional cleanup:** the 10 smoke-test pending_ingest rows in state
  NV will linger in the `done` bucket forever (the worker doesn't TTL
  done rows). Add a `pending_ingest` retention sweep next session if
  it bothers anyone (it shouldn't — `done` rows are ~200 bytes each).
- **Confirm FL drain reaches done.** Currently running. The
  `state_scrapers/fl/results/fl_complete_20260510T025409Z/orchestrator.log`
  will record progress.
