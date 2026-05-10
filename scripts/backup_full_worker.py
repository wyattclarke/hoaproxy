#!/usr/bin/env python3
"""Detached worker for /admin/backup-full.

Throttled incremental backup of the live SQLite DB to GCS. Uses SQLite's
online backup API (``Connection.backup(pages=N, progress=cb)``) with a
sleep in the progress callback so we copy at ~5-10 MiB/s rather than
saturating disk I/O. The earlier VACUUM-INTO approach pegged the disk
on the 65 GiB live DB, healthchecks failed, Render docker-stopped the
container, and the snapshot froze at 6 GiB (2026-05-10 incident).

Why ``Connection.backup`` over VACUUM INTO:
  * Designed for live databases — readers and writers can continue.
  * Incremental: copies ``pages`` per step, sleeps between steps.
  * Naturally cooperative with WAL — sees a consistent snapshot at start.

Args: DB_PATH BUCKET BLOB_NAME SNAPSHOT_PATH
Logs: stdout/stderr; the parent endpoint redirects both to a file on the
persistent disk so the run can be inspected via /admin/backup-full-log.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
import traceback


# Throttle knobs. 200 pages × 4 KiB/page = 800 KiB per step. With
# 100 ms sleep + ~50 ms step time the effective throughput is roughly
# 5-7 MiB/s — slow enough that healthchecks pass on Render Standard,
# fast enough that 65 GiB completes in 2.5-4 hours.
PAGES_PER_STEP = 200
SLEEP_BETWEEN_STEPS = 0.10
PROGRESS_LOG_EVERY_PCT = 2.0


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _reap_orphans(db_dir: str) -> None:
    """Reap _backup-*.{db,db-journal,log} older than 1h. Defense in depth so
    one killed worker doesn't wedge the disk."""
    pat = re.compile(r"^_backup-\d{8}-\d{6}\.(db|db-journal|log)$")
    cutoff = time.time() - 3600
    try:
        names = os.listdir(db_dir)
    except OSError as e:
        print(f"[{_ts()}] orphan reap listdir failed: {e}", flush=True)
        return
    for name in names:
        if not pat.match(name):
            continue
        p = os.path.join(db_dir, name)
        try:
            if os.path.getmtime(p) >= cutoff:
                continue
            sz = os.path.getsize(p)
            os.remove(p)
            print(f"[{_ts()}] reaped orphan={p} bytes={sz}", flush=True)
        except OSError:
            continue


def main() -> int:
    if len(sys.argv) != 5:
        print(
            f"usage: {sys.argv[0]} DB_PATH BUCKET BLOB_NAME SNAPSHOT_PATH",
            file=sys.stderr,
            flush=True,
        )
        return 2

    db_path, bucket_name, blob_name, snapshot_path = sys.argv[1:5]
    started = time.monotonic()
    print(
        f"[{_ts()}] backup-full(throttled) start pid={os.getpid()} "
        f"db={db_path} snapshot={snapshot_path} "
        f"blob=gs://{bucket_name}/{blob_name} "
        f"pages_per_step={PAGES_PER_STEP} sleep_between_steps={SLEEP_BETWEEN_STEPS}s",
        flush=True,
    )

    # If a stale snapshot file exists from an earlier killed worker at this
    # exact stamp (shouldn't normally happen, but be defensive), remove it
    # before we sqlite3.connect to the destination — appending to an existing
    # file would corrupt the backup.
    if os.path.exists(snapshot_path):
        try:
            os.remove(snapshot_path)
            print(f"[{_ts()}] removed pre-existing snapshot file", flush=True)
        except OSError as e:
            print(f"[{_ts()}] could not remove pre-existing snapshot: {e}", flush=True)
            return 1

    last_logged_pct = -PROGRESS_LOG_EVERY_PCT
    last_log_t = started

    def _progress(status: int, remaining: int, total: int) -> None:
        nonlocal last_logged_pct, last_log_t
        pct = (total - remaining) * 100.0 / max(total, 1)
        now = time.monotonic()
        if pct - last_logged_pct >= PROGRESS_LOG_EVERY_PCT or now - last_log_t > 60:
            elapsed = now - started
            done_pages = total - remaining
            rate_mb = (done_pages * 4096) / max(elapsed, 1) / (1024 * 1024)
            eta_s = (remaining * 4096) / max(rate_mb * 1024 * 1024, 1)
            print(
                f"[{_ts()}] copy {pct:.1f}% pages_done={done_pages}/{total} "
                f"elapsed={elapsed:.0f}s rate={rate_mb:.2f}MiB/s eta={eta_s:.0f}s",
                flush=True,
            )
            last_logged_pct = pct
            last_log_t = now
        time.sleep(SLEEP_BETWEEN_STEPS)

    try:
        # Source: read-only URI so we can never accidentally write to the live
        # DB. SQLite still reads the WAL through this connection.
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60.0)
        dst = sqlite3.connect(snapshot_path, timeout=60.0)
        try:
            src.backup(dst, pages=PAGES_PER_STEP, progress=_progress)
            dst.commit()
        finally:
            try:
                dst.close()
            except Exception:
                pass
            try:
                src.close()
            except Exception:
                pass

        size_bytes = os.path.getsize(snapshot_path)
        copy_elapsed = time.monotonic() - started
        avg_rate = size_bytes / max(copy_elapsed, 1) / (1024 * 1024)
        print(
            f"[{_ts()}] copy done bytes={size_bytes} elapsed={copy_elapsed:.1f}s "
            f"avg_rate={avg_rate:.2f}MiB/s",
            flush=True,
        )

        from google.cloud import storage as gcs

        upload_started = time.monotonic()
        print(f"[{_ts()}] gcs upload start", flush=True)
        gcs.Client().bucket(bucket_name).blob(blob_name).upload_from_filename(
            snapshot_path,
            timeout=3600,
        )
        upload_elapsed = time.monotonic() - upload_started
        upload_rate = size_bytes / max(upload_elapsed, 1) / (1024 * 1024)
        print(
            f"[{_ts()}] backup-full ok blob=gs://{bucket_name}/{blob_name} "
            f"bytes={size_bytes} upload_elapsed={upload_elapsed:.1f}s "
            f"upload_rate={upload_rate:.2f}MiB/s "
            f"total_elapsed={time.monotonic() - started:.1f}s",
            flush=True,
        )
        return 0
    except Exception as e:
        print(f"[{_ts()}] backup-full failed: {e}", flush=True)
        traceback.print_exc()
        return 1
    finally:
        try:
            if os.path.exists(snapshot_path):
                os.remove(snapshot_path)
                print(f"[{_ts()}] cleaned snapshot={snapshot_path}", flush=True)
        except OSError as e:
            print(f"[{_ts()}] cleanup failed: {e}", flush=True)
        _reap_orphans(os.path.dirname(snapshot_path) or ".")


if __name__ == "__main__":
    sys.exit(main())
