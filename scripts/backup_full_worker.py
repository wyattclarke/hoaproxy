#!/usr/bin/env python3
"""Detached worker for /admin/backup-full.

VACUUM INTO + GCS upload of the live SQLite DB. Runs as its own process
(launched via subprocess.Popen with start_new_session=True) so uvicorn worker
recycles, graceful-shutdown SIGTERMs, and OOM kills of the request worker
don't take the in-flight backup with them — the failure mode that left the
prior daemon-thread implementation silent.

Args: DB_PATH BUCKET BLOB_NAME SNAPSHOT_PATH
Logs: stdout/stderr; the parent endpoint redirects both to a file on the
persistent disk so the run can be inspected via /admin/backup-full-log.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import traceback


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
        f"[{_ts()}] backup-full start pid={os.getpid()} db={db_path} "
        f"snapshot={snapshot_path} blob=gs://{bucket_name}/{blob_name}",
        flush=True,
    )

    try:
        src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            src.execute("VACUUM INTO ?", (snapshot_path,))
        finally:
            src.close()
        size_bytes = os.path.getsize(snapshot_path)
        print(
            f"[{_ts()}] vacuum done bytes={size_bytes} "
            f"elapsed={time.monotonic() - started:.1f}s",
            flush=True,
        )

        from google.cloud import storage as gcs

        blob = gcs.Client().bucket(bucket_name).blob(blob_name)
        blob.upload_from_filename(snapshot_path, timeout=1800)
        print(
            f"[{_ts()}] backup-full ok blob=gs://{bucket_name}/{blob_name} "
            f"bytes={size_bytes} total_elapsed={time.monotonic() - started:.1f}s",
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


if __name__ == "__main__":
    sys.exit(main())
