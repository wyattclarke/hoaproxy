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

        # 2. Walk + upload in parallel.
        walk_root = docs_root / prefix if prefix else docs_root
        if not walk_root.exists():
            print(f"[{_ts()}] walk root {walk_root} does not exist; nothing to do", flush=True)
            return 0

        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        counters = {"uploaded": 0, "skipped": 0, "errors": 0, "total_bytes": 0}
        counters_lock = threading.Lock()

        # MAX_WORKERS: 16 threads gives us ~50–80 MiB/s on Render's 1 CPU plan.
        # Higher hits diminishing returns (GIL + per-thread socket cost).
        max_workers = int(os.environ.get("SNAPSHOT_PARALLEL", "16"))

        def _upload_one(path: Path) -> tuple[str, int]:
            rel = path.relative_to(docs_root).as_posix()
            blob_name = f"hoa_docs/{rel}"
            size = path.stat().st_size
            if existing.get(blob_name, -1) == size:
                return ("skipped", size)
            bucket.blob(blob_name).upload_from_filename(str(path), timeout=600)
            return ("uploaded", size)

        last_beat = time.monotonic()

        def _beat() -> None:
            nonlocal last_beat
            elapsed = time.monotonic() - started
            with counters_lock:
                up, sk, er, tb = (counters["uploaded"], counters["skipped"],
                                  counters["errors"], counters["total_bytes"])
            rate_mibs = (tb / max(elapsed, 1)) / (1024 * 1024)
            print(
                f"[{_ts()}] heartbeat uploaded={up:,} skipped={sk:,} errors={er} "
                f"bytes={tb:,} elapsed={elapsed:.0f}s rate={rate_mibs:.2f}MiB/s "
                f"workers={max_workers}",
                flush=True,
            )
            last_beat = time.monotonic()

        files_iter = (p for p in walk_root.rglob("*") if p.is_file())

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            in_flight: dict = {}
            target_qd = max_workers * 4  # keep ~64 ops queued

            def _drain_completed(timeout: float | None = None) -> None:
                done = []
                for fut in list(in_flight.keys()):
                    if fut.done():
                        done.append(fut)
                if not done and timeout is not None:
                    for fut in as_completed(list(in_flight.keys()), timeout=timeout):
                        done.append(fut)
                        break
                for fut in done:
                    path = in_flight.pop(fut)
                    try:
                        action, size = fut.result()
                        with counters_lock:
                            counters[action] += 1
                            if action == "uploaded":
                                counters["total_bytes"] += size
                    except Exception as exc:
                        with counters_lock:
                            counters["errors"] += 1
                        print(f"[{_ts()}] upload-error path={path} err={exc}", flush=True)

            for path in files_iter:
                fut = pool.submit(_upload_one, path)
                in_flight[fut] = path
                # Bounded queue: don't outpace workers
                if len(in_flight) >= target_qd:
                    _drain_completed(timeout=5.0)
                if (time.monotonic() - last_beat) >= 30:
                    _beat()
                with counters_lock:
                    if counters["errors"] > 100:
                        print(f"[{_ts()}] too many errors; aborting", flush=True)
                        return 1

            # Drain remaining
            while in_flight:
                _drain_completed(timeout=5.0)
                if (time.monotonic() - last_beat) >= 30:
                    _beat()

        elapsed = time.monotonic() - started
        with counters_lock:
            up, sk, er, tb = (counters["uploaded"], counters["skipped"],
                              counters["errors"], counters["total_bytes"])
        print(
            f"[{_ts()}] snapshot-hoa-docs DONE uploaded={up:,} skipped={sk:,} "
            f"errors={er} bytes={tb:,} elapsed={elapsed:.0f}s",
            flush=True,
        )
        return 0
    except Exception as exc:
        print(f"[{_ts()}] snapshot-hoa-docs FAILED: {exc}", flush=True)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
