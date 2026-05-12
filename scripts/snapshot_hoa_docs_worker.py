#!/usr/bin/env python3
"""Detached worker for /admin/snapshot-hoa-docs-to-gcs.

Mirrors HOA_DOCS_ROOT into gs://{BUCKET}/hoa_docs/. The endpoint launches
us under ``nice -n 19 ionice -c 3`` so we yield to API requests instead
of competing for disk + CPU.

Why detached: a 1 TB upload from Render's 1 CPU plan takes hours, and
Render's HTTP frontend disconnects sync requests after ~5 minutes.
Daemon threads die with worker recycles, deploys, and OOMs. A separate
process started with start_new_session=True survives parent restarts and
writes its own log file on the persistent disk.

Args: DOCS_ROOT BUCKET LOG_PATH [PREFIX]
- DOCS_ROOT: absolute path to walk
- BUCKET: gs:// bucket name (no prefix)
- LOG_PATH: where to write progress
- PREFIX: optional, relative to DOCS_ROOT (e.g. "Foo HOA/" to scope a re-run)

Behavior:
- Lists existing blobs once at start to dedup by size; skip if size matches.
- Walks DOCS_ROOT and uploads any file not already present at the matching size.
- Heartbeat every 500 files OR every 30 seconds with running totals.
- Exits 0 on completion, 1 on fatal error.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main() -> int:
    if len(sys.argv) < 4 or len(sys.argv) > 5:
        print(
            f"usage: {sys.argv[0]} DOCS_ROOT BUCKET LOG_PATH [PREFIX]",
            file=sys.stderr,
            flush=True,
        )
        return 2

    docs_root_str, bucket_name, _log_path = sys.argv[1:4]
    prefix = sys.argv[4] if len(sys.argv) == 5 else ""
    docs_root = Path(docs_root_str).resolve()
    started = time.monotonic()

    print(
        f"[{_ts()}] snapshot-hoa-docs start pid={os.getpid()} "
        f"root={docs_root} bucket=gs://{bucket_name} prefix={prefix!r}",
        flush=True,
    )

    try:
        from google.cloud import storage as gcs

        client = gcs.Client()
        bucket = client.bucket(bucket_name)

        # 1. List existing blobs to build size-keyed dedup map.
        blob_prefix = f"hoa_docs/{prefix}" if prefix else "hoa_docs/"
        print(f"[{_ts()}] listing existing blobs at {blob_prefix} ...", flush=True)
        existing: dict[str, int] = {}
        for blob in client.list_blobs(bucket, prefix=blob_prefix):
            existing[blob.name] = blob.size or 0
            if len(existing) % 50000 == 0:
                print(f"[{_ts()}]   listed {len(existing):,} existing blobs", flush=True)
        print(f"[{_ts()}] existing blob count: {len(existing):,}", flush=True)

        # 2. Walk + upload.
        walk_root = docs_root / prefix if prefix else docs_root
        if not walk_root.exists():
            print(f"[{_ts()}] walk root {walk_root} does not exist; nothing to do", flush=True)
            return 0

        uploaded = 0
        skipped = 0
        errors = 0
        total_bytes = 0
        last_beat = time.monotonic()
        files_since_beat = 0

        for path in walk_root.rglob("*"):
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(docs_root).as_posix()
                blob_name = f"hoa_docs/{rel}"
                size = path.stat().st_size

                if existing.get(blob_name, -1) == size:
                    skipped += 1
                else:
                    bucket.blob(blob_name).upload_from_filename(str(path), timeout=600)
                    uploaded += 1
                    total_bytes += size

                files_since_beat += 1
                now = time.monotonic()
                if files_since_beat >= 500 or (now - last_beat) >= 30:
                    elapsed = now - started
                    rate_mibs = (total_bytes / max(elapsed, 1)) / (1024 * 1024)
                    print(
                        f"[{_ts()}] heartbeat uploaded={uploaded:,} skipped={skipped:,} "
                        f"errors={errors} bytes={total_bytes:,} "
                        f"elapsed={elapsed:.0f}s rate={rate_mibs:.2f}MiB/s",
                        flush=True,
                    )
                    last_beat = now
                    files_since_beat = 0
            except Exception as exc:
                errors += 1
                print(f"[{_ts()}] upload-error path={path} err={exc}", flush=True)
                if errors > 100:
                    print(f"[{_ts()}] too many errors; aborting", flush=True)
                    return 1

        elapsed = time.monotonic() - started
        print(
            f"[{_ts()}] snapshot-hoa-docs DONE uploaded={uploaded:,} skipped={skipped:,} "
            f"errors={errors} bytes={total_bytes:,} elapsed={elapsed:.0f}s",
            flush=True,
        )
        return 0
    except Exception as exc:
        print(f"[{_ts()}] snapshot-hoa-docs FAILED: {exc}", flush=True)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
