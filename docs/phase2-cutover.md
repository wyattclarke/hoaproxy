# Phase 2 cutover playbook

*Companion to `docs/scaling-proposal.md` §Phase 2. Use this when flipping
`ASYNC_INGEST_ENABLED` from `0` to `1` in production.*

## TL;DR

Phase 2 introduces a background ingest worker that drains a
`pending_ingest` SQLite queue. The cutover is a single env-var change
(`ASYNC_INGEST_ENABLED=1`). The synchronous fallback path stays in the
code, so rollback is also a single env-var change (`=0`).

This is a no-downtime cutover. The worker process is co-deployed in the
same Render container as the web service (Render persistent disks can't
be shared across services as of 2026). Each lives in a separate Python
heap, so the OCR/embedding RAM spike in the worker can't OOM the
request handler.

## Pre-flight (T-30 min)

Verify the deploy is healthy under `ASYNC_INGEST_ENABLED=0` (default):

```bash
curl -s https://hoaproxy.org/healthz
# {"status": "ok"}

# Worker process is running but the queue is empty (flag off).
curl -s "https://hoaproxy.org/admin/ingest/queue-stats" \
    -H "Authorization: Bearer $LIVE_JWT_SECRET"
# {"counts": {}, "by_state": []}
```

Check the Render service log to confirm the `ingest_worker` started:

```
[start_web_with_worker] starting hoaware.ingest_worker
...
2026-05-10 ... INFO [hoaproxy.ingest_worker] ingest_worker starting (db=/var/data/hoa_index.db poll=2.0s heartbeat=30.0s max_attempts=3)
2026-05-10 ... INFO [hoaproxy.ingest_worker] ingest_worker heartbeat counts={}
```

If the worker is NOT running, set `INGEST_WORKER_ENABLED=1` via Render
PATCH and wait for redeploy before continuing.

## Smoke test (T-10 min)

Manually enqueue a single test job to verify the worker drains
correctly while the flag is still off. The flag only affects whether
`/upload` and `/admin/ingest-ready-gcs` enqueue or run synchronously;
the worker itself ALWAYS drains the queue when there are pending rows.

```bash
# Pick a small ready bundle to use as the test.
gcloud auth login   # if not already
gsutil ls gs://hoaproxy-ingest-ready/v1/FL/ | head -3
# Take one prefix, e.g. gs://hoaproxy-ingest-ready/v1/FL/dade/some-hoa/abc123/

# Synthetic enqueue via direct DB insert (workaround until cutover):
# ssh into Render web service (Render shell)
sqlite3 /var/data/hoa_index.db <<EOF
INSERT INTO pending_ingest (job_id, bundle_uri, state, enqueued_at, status, source)
VALUES (
    'smoke-' || hex(randomblob(8)),
    'gs://hoaproxy-ingest-ready/v1/FL/.../abc123',
    'FL',
    strftime('%s','now'),
    'pending',
    'manual'
);
EOF
```

Watch the worker logs — within ~2s it should claim, process, and mark
done. Verify via:

```bash
curl -s "https://hoaproxy.org/ingest/status/smoke-..."
# {"status": "done", "result": {...}}
```

If the worker fails the smoke test, do NOT proceed with cutover. Common
issues:

- Worker can't load `api.main` because of an import error → fix the
  import, redeploy, retry.
- DocAI quota exhausted (status="failed" with "quota_exceeded" error)
  → wait for the rolling 24h tracker to drain (`/admin/costs/docai-alert`).
- Bundle status mismatch (bundle already 'imported') → expected; the
  worker reports "skipped".

## Coordinate parallel sessions (T-5 min)

Any state-scraper sessions hitting `/admin/ingest-ready-gcs` in a loop
should pause AT THE LOOP BOUNDARY. They can keep discovering, banking,
and preparing — only the drain step needs to pause.

Sessions to coordinate as of 2026-05-10:

- **CA scraper** (`state_scrapers/ca/...`) — uses synchronous drain.
- **AZ scraper** (`state_scrapers/az/...`) — `prepare_bank_for_ingest`
  running locally; not yet hitting `/admin/ingest-ready-gcs`.
- **NY scraper** (if active) — same shape as CA.
- **FL drain** (`/tmp/fl_wave_runner.sh`) — currently PAUSED awaiting
  Phase 2. This is the use case driving the cutover.

Drop a note in their orchestrator logs:

```bash
echo "[$(date -u +%FT%TZ)] PAUSE for Phase 2 cutover at T+0" \
    >> state_scrapers/_orchestrator/$STATE_overnight.log
```

## Flip the flag (T+0)

Use **PATCH** on the single env-var, not the full env list — see
`reference_render_api.md` (the full PUT silently drops `sync: false`
secrets like `OPENAI_API_KEY`).

```bash
# Find the env-var ID once:
RENDER_SVC=srv-d62kms68alac738h67b0
ENV_VAR_ID=$(curl -s "https://api.render.com/v1/services/$RENDER_SVC/env-vars" \
    -H "Authorization: Bearer $RENDER_API_KEY" \
    | jq -r '.[] | select(.envVar.key=="ASYNC_INGEST_ENABLED") | .envVar.id')

# Flip to 1:
curl -X PATCH "https://api.render.com/v1/services/$RENDER_SVC/env-vars/ASYNC_INGEST_ENABLED" \
    -H "Authorization: Bearer $RENDER_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"value": "1"}'
```

Render redeploys in ~2 min. Watch the service log for:

```
[start_web_with_worker] starting hoaware.ingest_worker
...
INFO ingest_worker starting (...)
```

## Verify (T+5 min)

Confirm the flag is live:

```bash
curl -s "https://hoaproxy.org/admin/ingest/queue-stats" \
    -H "Authorization: Bearer $LIVE_JWT_SECRET"
# {"counts": {...}, "by_state": [...]}
```

Test a fresh /upload:

```bash
curl -X POST "https://hoaproxy.org/upload" \
    -H "Authorization: Bearer $USER_JWT" \
    -F "hoa=Cutover Test HOA" \
    -F "files=@small-ccr.pdf" \
    -F "categories=ccr" \
    -F "text_extractable=true"
# Expected: {"queued": true, "job_id": "...", "status_url": "/ingest/status/..."}
```

Within 30s the job should be `done`:

```bash
curl -s "https://hoaproxy.org/ingest/status/$JOB_ID"
# {"status": "done", "result": {"indexed": 1, ...}}
```

Test `/admin/ingest-ready-gcs` enqueueing:

```bash
curl -X POST "https://hoaproxy.org/admin/ingest-ready-gcs?state=FL&limit=5" \
    -H "Authorization: Bearer $LIVE_JWT_SECRET"
# Expected: {"async": true, "enqueued": 5, "job_ids": [...]}
# NOT: the legacy {"results": [{"status": "imported", ...}]} shape.
```

## Resume parallel sessions (T+10 min)

Notify CA/AZ/NY/FL sessions to resume their drains. The loop body
doesn't need to change — `/admin/ingest-ready-gcs` now returns 202-ish
shape but the existing scripts check for empty-result loop exit, which
the new shape still respects.

For the FL drain specifically, the `/tmp/fl_wave_runner.sh` script's
75s pacing can be DROPPED (back to the pre-OOM 50-limit + no sleep).
The pacing was there because the synchronous path OOMed; the async
enqueue is cheap.

## Health check (T+30 min)

Sample 10 jobs from the queue:

```sql
SELECT job_id, status, attempts, error
FROM pending_ingest
WHERE enqueued_at > strftime('%s','now') - 1800
ORDER BY enqueued_at DESC LIMIT 10;
```

All should be `done` with `attempts=1`. If any are `failed` or `dead`,
inspect:

```sql
SELECT * FROM pending_ingest WHERE status IN ('failed', 'dead') ORDER BY failed_at DESC LIMIT 5;
```

Common failure modes:

| error text | meaning | action |
|---|---|---|
| `quota_exceeded` | DocAI day cap hit | Wait for rolling 24h, then `/admin/ingest/retry-dead` |
| `missing GCS object` | bundle.json or sidecar missing | The bundle is malformed; investigate the prepare step |
| `prepared bundle is missing text sidecars` | sidecar not generated | rerun `prepare_bank_for_ingest.py` for that HOA |
| OS-level error in worker logs | crashed worker | Render auto-restarts; job re-enqueued on next claim |

For mass-failure recovery:

```bash
curl -X POST "https://hoaproxy.org/admin/ingest/retry-dead" \
    -H "Authorization: Bearer $LIVE_JWT_SECRET"
# {"reset_count": N}
```

## Rollback

If `done` count stops growing or the web service shows new errors:

```bash
curl -X PATCH "https://api.render.com/v1/services/$RENDER_SVC/env-vars/ASYNC_INGEST_ENABLED" \
    -H "Authorization: Bearer $RENDER_API_KEY" \
    -d '{"value": "0"}'
```

Render redeploys. The synchronous code path resumes serving `/upload`
and `/admin/ingest-ready-gcs` as before. The worker keeps draining
pending rows in the background until the queue empties — no row is
lost.

For an emergency stop of the worker only (e.g. it's eating RAM and you
don't want to redeploy yet), set `INGEST_WORKER_ENABLED=0` via PATCH
and the next container restart will start uvicorn alone.

## Done criteria

- [ ] 10 sample jobs completed successfully under async mode.
- [ ] `/upload` returns `{"queued": true, "job_id": ...}` shape.
- [ ] `/admin/ingest-ready-gcs` returns `{"async": true, "enqueued": N}` shape.
- [ ] All paused state-scraper sessions resumed and progressing.
- [ ] FL drain throughput ≥ pre-Phase-2 rate (was ~20 HOAs/30min with
      75s pacing; expect ≥ 60 HOAs/30min in async mode).

After 24h of healthy operation: remove the 75s pacing line from
`/tmp/fl_wave_runner.sh` permanently and check the Phase 2 milestone
off in `docs/scaling-proposal.md`.
