"""Phase 2 background ingest worker.

Drains the `pending_ingest` SQLite queue. Owned by the dedicated Render
Background Worker service (`hoaproxy-ingest`); the web service only
enqueues. See docs/scaling-proposal.md §Phase 2 + docs/phase2-cutover.md.

Run shape:

    python -m hoaware.ingest_worker

Loop:
  1. Claim oldest `status='pending'` row via `db.claim_next_pending_ingest`
     (atomic UPDATE WHERE status='pending'; sets started_at and increments
     attempts).
  2. Resolve `bundle_uri` (gs://prepared-bucket/v1/{state}/.../{bundle}) →
     call `api.main._process_prepared_bundle` with `claim=False` so we
     don't re-acquire the GCS-side claim (the web service already did).
  3. On success → `db.mark_pending_ingest_done`. On exception →
     `db.mark_pending_ingest_failed` which either re-enqueues for retry
     (status='pending') or marks dead after max_attempts.

Concurrency: single-process by default. Render gives us a ~4GB worker;
ingest is RAM-bound (OCR-text → embedding → SQLite upsert), so one job at
a time keeps RAM in budget. If we ever need more throughput, scale by
running multiple workers — the `claim_next_pending_ingest` UPDATE is
safe under writer concurrency.

Shutdown: catches SIGTERM and finishes the in-flight job before exiting.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from urllib.parse import urlparse

from . import db
from .config import load_settings

logger = logging.getLogger("hoaproxy.ingest_worker")

_POLL_INTERVAL_SEC = float(os.environ.get("INGEST_WORKER_POLL_SEC", "2.0"))
_HEARTBEAT_INTERVAL_SEC = float(os.environ.get("INGEST_WORKER_HEARTBEAT_SEC", "30.0"))
_MAX_ATTEMPTS = int(os.environ.get("INGEST_WORKER_MAX_ATTEMPTS", "3"))
# Exponential backoff between retries — capped because the queue is small.
_BACKOFF_BASE_SEC = float(os.environ.get("INGEST_WORKER_BACKOFF_BASE", "5.0"))
_BACKOFF_CAP_SEC = float(os.environ.get("INGEST_WORKER_BACKOFF_CAP", "120.0"))

_shutdown_requested = False


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        global _shutdown_requested
        logger.info("Worker received signal %s — will exit after current job", signum)
        _shutdown_requested = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # Not running in main thread (e.g. unit tests) — skip.
            pass


def _bundle_uri_to_prefix(bundle_uri: str) -> tuple[str, str]:
    """`gs://bucket/v1/STATE/.../bundle1` → (bucket, prefix)."""
    parsed = urlparse(bundle_uri)
    if parsed.scheme != "gs":
        raise ValueError(f"unsupported bundle_uri scheme: {bundle_uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def process_job(row, *, settings, processors) -> dict:
    """Run one job. Caller handles claim/mark-done/mark-failed.

    `processors` is a dict of URI-scheme → callable. Two schemes are
    currently supported: `gs://` (prepared GCS bundles) and `local://`
    (upload sidecars written by /upload's async path).

    Returns the inner per-prefix result dict (success or failure shape).
    Raises on hard failure so the caller can route to mark_pending_ingest_failed.
    """
    from pathlib import Path

    bundle_uri = row["bundle_uri"]
    state_n = row["state"]

    if bundle_uri.startswith("gs://"):
        bucket_name, prefix = _bundle_uri_to_prefix(bundle_uri)
        return processors["gs"](
            prefix,
            state_n=state_n,
            bucket_name=bucket_name,
            settings=settings,
            claim=False,
            dry_run=False,
        )
    if bundle_uri.startswith("local://"):
        sidecar_path = Path(bundle_uri[len("local://"):])
        return processors["local"](sidecar_path, settings=settings)
    raise ValueError(f"unsupported bundle_uri scheme: {bundle_uri!r}")


def _import_processors():
    """Lazy-import the shared processors from api.main.

    api.main imports a lot of FastAPI machinery, OpenAI client, etc. The
    worker pays that import cost once on boot and then loops.
    """
    from api.main import _process_prepared_bundle, _process_local_upload_sidecar  # noqa: WPS433
    return {
        "gs": _process_prepared_bundle,
        "local": _process_local_upload_sidecar,
    }


def run_loop(*, poll_interval: float = _POLL_INTERVAL_SEC) -> None:
    settings = load_settings()
    processors = _import_processors()
    last_heartbeat = 0.0

    logger.info(
        "ingest_worker starting (db=%s poll=%.1fs heartbeat=%.1fs max_attempts=%d)",
        settings.db_path,
        poll_interval,
        _HEARTBEAT_INTERVAL_SEC,
        _MAX_ATTEMPTS,
    )

    consecutive_failures_per_job: dict[str, int] = {}

    while not _shutdown_requested:
        now = time.time()
        if now - last_heartbeat > _HEARTBEAT_INTERVAL_SEC:
            try:
                with db.get_connection(settings.db_path) as conn:
                    counts = db.count_pending_ingest_by_status(conn)
                logger.info("ingest_worker heartbeat counts=%s", counts)
            except Exception:
                logger.exception("ingest_worker heartbeat DB read failed")
            last_heartbeat = now

        row = None
        try:
            with db.get_connection(settings.db_path) as conn:
                row = db.claim_next_pending_ingest(conn)
        except Exception:
            logger.exception("ingest_worker claim failed")
            time.sleep(min(_BACKOFF_CAP_SEC, poll_interval * 5))
            continue

        if row is None:
            time.sleep(poll_interval)
            continue

        job_id = row["job_id"]
        attempts = int(row["attempts"])
        logger.info(
            "ingest_worker claimed job_id=%s bundle=%s attempts=%d",
            job_id,
            row["bundle_uri"],
            attempts,
        )

        try:
            result = process_job(
                row,
                settings=settings,
                processors=processors,
            )
        except Exception as exc:
            logger.exception("ingest_worker job failed job_id=%s", job_id)
            try:
                with db.get_connection(settings.db_path) as conn:
                    next_status = db.mark_pending_ingest_failed(
                        conn,
                        job_id,
                        error=f"{type(exc).__name__}: {exc}",
                        max_attempts=_MAX_ATTEMPTS,
                    )
                logger.info("ingest_worker marked job_id=%s status=%s", job_id, next_status)
            except Exception:
                logger.exception("ingest_worker failed to record failure for job_id=%s", job_id)
            # Exponential backoff before claiming the next pending row so we
            # don't tight-loop on a poison job that keeps being re-enqueued.
            backoff = min(_BACKOFF_CAP_SEC, _BACKOFF_BASE_SEC * (2 ** max(0, attempts - 1)))
            consecutive_failures_per_job[job_id] = consecutive_failures_per_job.get(job_id, 0) + 1
            time.sleep(backoff)
            continue

        # The inner per-prefix result already includes status='failed' for
        # bundle-level validation failures (these are not exceptional — they
        # mean the bundle was malformed). Treat as a hard failure so the
        # caller can decide to fix the bundle and retry.
        if result.get("status") not in {"imported", "skipped", "ready"}:
            err = result.get("error") or f"bundle status={result.get('status')!r}"
            logger.warning("ingest_worker job_id=%s soft-failed: %s", job_id, err)
            try:
                with db.get_connection(settings.db_path) as conn:
                    next_status = db.mark_pending_ingest_failed(
                        conn, job_id, error=err, max_attempts=_MAX_ATTEMPTS
                    )
                logger.info("ingest_worker marked job_id=%s status=%s", job_id, next_status)
            except Exception:
                logger.exception("ingest_worker failed to record soft-failure for job_id=%s", job_id)
            backoff = min(_BACKOFF_CAP_SEC, _BACKOFF_BASE_SEC * (2 ** max(0, attempts - 1)))
            time.sleep(backoff)
            continue

        try:
            with db.get_connection(settings.db_path) as conn:
                db.mark_pending_ingest_done(conn, job_id, result=result)
            consecutive_failures_per_job.pop(job_id, None)
            logger.info("ingest_worker job_id=%s done result=%s", job_id, result)
        except Exception:
            logger.exception("ingest_worker failed to mark done job_id=%s", job_id)

    logger.info("ingest_worker shutdown complete")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("INGEST_WORKER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    _install_signal_handlers()
    try:
        run_loop()
    except KeyboardInterrupt:
        logger.info("ingest_worker interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
