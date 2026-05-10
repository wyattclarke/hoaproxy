#!/usr/bin/env python3
"""Detached worker for /admin/backup-full.

VACUUM INTO + GCS upload of the live SQLite DB. The endpoint launches us
under ``nice -n 19 ionice -c 3`` so the OS deprioritizes our I/O — the
backup yields disk bandwidth to API requests instead of starving them.

Why VACUUM INTO and not Connection.backup(): the throttled
``Connection.backup(pages=N)`` approach was tried (commit 5f9102c) and
failed twice on this DB. The 2.4 GiB WAL forces frequent auto-
checkpoints, and every checkpoint mutates main-DB pages, which causes
SQLite to restart the in-progress backup step. Result: pages_done
oscillates (e.g. 2200 → 1600 → 600 → 1400 → 200) and never reaches the
end. VACUUM INTO is a single atomic SQL statement that does not have a
step-restart failure mode — SQLite handles concurrent writers
internally without losing snapshot progress.

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
        f"[{_ts()}] backup-full(VACUUM INTO) start pid={os.getpid()} "
        f"db={db_path} snapshot={snapshot_path} "
        f"blob=gs://{bucket_name}/{blob_name}",
        flush=True,
    )

    if os.path.exists(snapshot_path):
        try:
            os.remove(snapshot_path)
            print(f"[{_ts()}] removed pre-existing snapshot file", flush=True)
        except OSError as e:
            print(f"[{_ts()}] could not remove pre-existing snapshot: {e}", flush=True)
            return 1

    try:
        # Open with mode=ro so we never write to the live DB. SQLite still
        # reads the WAL through this connection. VACUUM INTO emits a
        # consistent snapshot at the moment the statement begins; concurrent
        # writes during VACUUM go to the WAL of the source and don't affect
        # the snapshot.
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=300.0)
        try:
            src.execute("VACUUM INTO ?", (snapshot_path,))
        finally:
            src.close()

        size_bytes = os.path.getsize(snapshot_path)
        copy_elapsed = time.monotonic() - started
        avg_rate = size_bytes / max(copy_elapsed, 1) / (1024 * 1024)
        print(
            f"[{_ts()}] vacuum done bytes={size_bytes} elapsed={copy_elapsed:.1f}s "
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
