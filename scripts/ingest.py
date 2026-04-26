#!/usr/bin/env python3
"""
Universal HOA ingest script.

Processes HOAs from data/ingest_queue/pending/. Each JSON file describes
one HOA with metadata and paths to PDFs. Supports two modes:

  --mode api     Upload via the site's /upload endpoint (slow, safe)
  --mode local   Run ingestion locally against a local DB + Qdrant (fast)

Any scraper can feed this queue by dropping a JSON file into pending/.
See scripts/queue_hoa.py for a helper.

Usage:
    python scripts/ingest.py --mode api [--api-url URL] [--delay 5] [--dry-run]
    python scripts/ingest.py --mode local [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent.parent
QUEUE_DIR = Path(os.environ.get("INGEST_QUEUE_DIR", str(ROOT / "data" / "ingest_queue")))
PENDING = QUEUE_DIR / "pending"
DONE = QUEUE_DIR / "done"
FAILED = QUEUE_DIR / "failed"

DEFAULT_API_URL = "https://hoaproxy.org"

# Throttling (API mode)
DELAY_BETWEEN_UPLOADS = 5
BACKOFF_DELAY = 30
MAX_RETRIES = 5
INGEST_POLL_INTERVAL = 10
INGEST_TIMEOUT = 600
MAX_BATCH_BYTES = 20 * 1024 * 1024
COOLDOWN_AFTER_INGEST = 10
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # per-file limit


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def load_jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET")
    if secret:
        return secret
    env_file = ROOT / "settings.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("JWT_SECRET="):
                return line.split("=", 1)[1].strip().strip("'\"")
    print("ERROR: JWT_SECRET not found in environment or settings.env", file=sys.stderr)
    sys.exit(1)


def register_or_login(api_url: str, email: str, password: str) -> str:
    payload = {"email": email, "password": password, "display_name": "Bulk Importer"}
    resp = requests.post(f"{api_url}/auth/register", json=payload, timeout=120)
    if resp.status_code == 200:
        return resp.json()["token"]
    if resp.status_code != 409:
        raise RuntimeError(f"register failed: {resp.status_code} {resp.text}")
    resp = requests.post(f"{api_url}/auth/login",
                         json={"email": email, "password": password}, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"login failed: {resp.status_code} {resp.text}")
    return resp.json()["token"]


def check_health(api_url: str) -> bool:
    try:
        return requests.get(f"{api_url}/healthz", timeout=10).status_code == 200
    except Exception:
        return False


def wait_for_healthy(api_url: str) -> None:
    while not check_health(api_url):
        print(f"    Server unhealthy, waiting {BACKOFF_DELAY}s...", flush=True)
        time.sleep(BACKOFF_DELAY)


def wait_for_ingestion(api_url: str, hoa_name: str) -> bool:
    endpoint = f"{api_url}/hoas/{quote(hoa_name, safe='')}/documents"
    elapsed = 0
    last_count = -1
    stable_polls = 0
    while elapsed < INGEST_TIMEOUT:
        try:
            resp = requests.get(endpoint, timeout=15)
            if resp.status_code == 200:
                count = len(resp.json())
                if count == last_count and count > 0:
                    stable_polls += 1
                    if stable_polls >= 2:
                        return True
                else:
                    stable_polls = 0
                last_count = count
        except Exception:
            pass
        time.sleep(INGEST_POLL_INTERVAL)
        elapsed += INGEST_POLL_INTERVAL
    return False


def move_entry(entry_path: Path, dest_dir: Path, extra: dict | None = None) -> None:
    """Move a queue entry to done/ or failed/, optionally updating it."""
    if extra:
        data = json.loads(entry_path.read_text())
        data.update(extra)
        entry_path.write_text(json.dumps(data, indent=2))
    shutil.move(str(entry_path), str(dest_dir / entry_path.name))


# ---------------------------------------------------------------------------
# API mode
# ---------------------------------------------------------------------------

def upload_one_api(entry: dict, entry_path: Path, api_url: str,
                   token: str, jwt_secret: str, delay: int, dry_run: bool) -> bool:
    """Upload one HOA via the site API. Returns True on success."""
    name = entry["name"]
    files = [Path(p) for p in entry.get("files", []) if Path(p).exists()]
    files = [f for f in files if f.stat().st_size <= MAX_UPLOAD_BYTES]

    if not files:
        print(f"  SKIP {name}: no valid files", flush=True)
        move_entry(entry_path, FAILED, {"error": "no_valid_files",
                                         "finished_at": _now()})
        return False

    # Metadata import
    headers_admin = {"Authorization": f"Bearer {jwt_secret}",
                     "Content-Type": "application/json"}
    metadata = {
        "source": entry.get("source", "unknown"),
        "records": [{
            "name": name,
            "state": entry.get("state", ""),
            "city": entry.get("city", ""),
            "postal_code": entry.get("postal_code", ""),
            "metadata_type": entry.get("metadata_type", "hoa"),
            "website_url": entry.get("website_url", ""),
        }],
    }

    if not dry_run:
        resp = requests.post(f"{api_url}/admin/bulk-import",
                             json=metadata, headers=headers_admin, timeout=120)
        if resp.status_code != 200:
            print(f"  METADATA FAIL {name}: {resp.status_code}", flush=True)

    if dry_run:
        print(f"  [dry-run] {name}: {len(files)} files", flush=True)
        move_entry(entry_path, DONE, {"status": "dry_run", "finished_at": _now()})
        return True

    # Upload files in size-based batches
    data_template = {"hoa": name, "state": entry.get("state", "")}
    for key in ["metadata_type", "website_url", "city", "postal_code"]:
        val = entry.get(key)
        if val and str(val).strip():
            data_template[key] = str(val)

    batches = _batch_by_size(files)
    headers_upload = {"Authorization": f"Bearer {token}"}

    for batch_idx, batch_paths in enumerate(batches):
        wait_for_healthy(api_url)

        success = False
        for attempt in range(MAX_RETRIES):
            opened = []
            multipart = []
            try:
                for p in batch_paths:
                    h = p.open("rb")
                    opened.append(h)
                    multipart.append(("files", (p.name, h, "application/pdf")))
                resp = requests.post(f"{api_url}/upload", headers=headers_upload,
                                     data=data_template.copy(), files=multipart, timeout=600)
            except Exception as exc:
                for h in opened:
                    h.close()
                print(f"    Connection error (attempt {attempt+1}): {exc}", flush=True)
                wait_for_healthy(api_url)
                time.sleep(BACKOFF_DELAY)
                continue
            finally:
                for h in opened:
                    h.close()

            if resp.status_code == 200:
                success = True
                break
            elif resp.status_code >= 500:
                print(f"    Server error ({resp.status_code}, attempt {attempt+1})", flush=True)
                wait_for_healthy(api_url)
                time.sleep(BACKOFF_DELAY)
            else:
                print(f"  UPLOAD FAIL {name}: {resp.status_code} {resp.text[:200]}", flush=True)
                break

        if not success:
            move_entry(entry_path, FAILED, {"error": f"upload_failed",
                                             "finished_at": _now()})
            return False

        # Wait for ingestion
        batch_label = f" batch {batch_idx+1}/{len(batches)}" if len(batches) > 1 else ""
        print(f"  {name}{batch_label}: {len(batch_paths)} files sent, waiting for ingestion...", flush=True)
        if wait_for_ingestion(api_url, name):
            print(f"    Ingestion complete.", flush=True)
        else:
            print(f"    Ingestion timed out (continuing).", flush=True)
        time.sleep(COOLDOWN_AFTER_INGEST)

    move_entry(entry_path, DONE, {"status": "ok", "files_uploaded": len(files),
                                   "finished_at": _now()})
    time.sleep(delay)
    return True


def _batch_by_size(files: list[Path]) -> list[list[Path]]:
    batches = []
    current, size = [], 0
    for f in files:
        fsize = f.stat().st_size
        if current and size + fsize > MAX_BATCH_BYTES:
            batches.append(current)
            current, size = [f], fsize
        else:
            current.append(f)
            size += fsize
    if current:
        batches.append(current)
    return batches


# ---------------------------------------------------------------------------
# Local mode
# ---------------------------------------------------------------------------

def ingest_one_local(entry: dict, entry_path: Path, dry_run: bool) -> bool:
    """Ingest one HOA locally using the hoaware pipeline."""
    # Import here to avoid requiring all deps in API mode
    sys.path.insert(0, str(ROOT))
    from hoaware.config import load_settings
    from hoaware.ingest import ingest_pdf_paths

    name = entry["name"]
    files = [Path(p) for p in entry.get("files", []) if Path(p).exists()]

    if not files:
        print(f"  SKIP {name}: no valid files", flush=True)
        move_entry(entry_path, FAILED, {"error": "no_valid_files",
                                         "finished_at": _now()})
        return False

    if dry_run:
        print(f"  [dry-run] {name}: {len(files)} files", flush=True)
        move_entry(entry_path, DONE, {"status": "dry_run", "finished_at": _now()})
        return True

    settings = load_settings()

    # Copy files into docs_root/{hoa_name}/ if not already there
    hoa_dir = settings.docs_root / name
    hoa_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    for f in files:
        dest = hoa_dir / f.name
        if not dest.exists():
            shutil.copy2(f, dest)
        staged.append(dest)

    try:
        stats = ingest_pdf_paths(name, staged, settings=settings, show_progress=False)
        print(f"  {name}: indexed={stats.indexed}, skipped={stats.skipped}, failed={stats.failed}", flush=True)
        if entry_path.exists():
            move_entry(entry_path, DONE, {"status": "ok", "indexed": stats.indexed,
                                           "finished_at": _now()})
        return True
    except Exception as exc:
        print(f"  ERROR {name}: {exc}", flush=True)
        if entry_path.exists():
            move_entry(entry_path, FAILED, {"error": str(exc), "finished_at": _now()})
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description="Universal HOA ingest from queue")
    parser.add_argument("--mode", choices=["api", "local"], required=True)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--delay", type=int, default=DELAY_BETWEEN_UPLOADS)
    parser.add_argument("--limit", type=int, default=0, help="Process at most N entries (0=all)")
    parser.add_argument("--source", default=None, help="Only process entries from this source")
    parser.add_argument("--workers", type=int, default=1,
                        help="Parallel workers for local mode (default: 1, ignored in api mode)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Ensure queue dirs exist
    for d in (PENDING, DONE, FAILED):
        d.mkdir(parents=True, exist_ok=True)

    # Load pending entries, sorted by filename for deterministic order
    entries = sorted(PENDING.glob("*.json"))
    if args.source:
        filtered = []
        for p in entries:
            data = json.loads(p.read_text())
            if data.get("source") == args.source:
                filtered.append(p)
        entries = filtered

    if args.limit > 0:
        entries = entries[:args.limit]

    print(f"Queue: {len(entries)} pending entries", flush=True)
    if not entries:
        return

    ok = 0
    fail = 0

    if args.mode == "api":
        api_url = args.api_url.rstrip("/")
        jwt_secret = load_jwt_secret()

        print(f"Checking server health...", end=" ", flush=True)
        if check_health(api_url):
            print("OK")
        else:
            print("waiting...")
            wait_for_healthy(api_url)

        email = f"ingest-{secrets.token_hex(6)}@example.com"
        password = secrets.token_urlsafe(18)
        token = register_or_login(api_url, email, password) if not args.dry_run else "dry-run"

        for i, entry_path in enumerate(entries):
            entry = json.loads(entry_path.read_text())
            print(f"[{i+1}/{len(entries)}] {entry['name']}", flush=True)
            if upload_one_api(entry, entry_path, api_url, token, jwt_secret,
                              args.delay, args.dry_run):
                ok += 1
            else:
                fail += 1

    elif args.mode == "local":
        # Use a dedicated Qdrant path for builds, unless already set
        # (workers get per-worker paths via env from the parent)
        build_qdrant = ROOT / "data" / "qdrant_local_build"
        if "HOA_QDRANT_LOCAL_PATH" not in os.environ:
            os.environ["HOA_QDRANT_LOCAL_PATH"] = str(build_qdrant)

        workers = max(1, args.workers)
        if workers == 1:
            for i, entry_path in enumerate(entries):
                entry = json.loads(entry_path.read_text())
                print(f"[{i+1}/{len(entries)}] {entry['name']}", flush=True)
                if ingest_one_local(entry, entry_path, args.dry_run):
                    ok += 1
                else:
                    fail += 1
        else:
            # Parallel: split entries into per-worker subdirs, each gets
            # its own Qdrant build path to avoid file locking conflicts.
            import subprocess
            print(f"Launching {workers} parallel workers for {len(entries)} entries", flush=True)

            chunks = [[] for _ in range(workers)]
            for i, entry_path in enumerate(entries):
                chunks[i % workers].append(entry_path)

            # Move entries into per-worker staging dirs
            worker_dirs = []
            for w_idx, chunk in enumerate(chunks):
                if not chunk:
                    continue
                w_pending = QUEUE_DIR / f".worker_{w_idx}" / "pending"
                w_pending.mkdir(parents=True, exist_ok=True)
                for p in chunk:
                    shutil.move(str(p), str(w_pending / p.name))
                worker_dirs.append((w_idx, w_pending.parent, len(chunk)))

            # Launch each worker as a subprocess with its own Qdrant path
            procs = []
            log_files = []
            for w_idx, w_dir, count in worker_dirs:
                log_path = Path(f"/tmp/ingest_worker_{w_idx}.log")
                log_f = log_path.open("w")
                log_files.append(log_f)
                env = os.environ.copy()
                env["HOA_QDRANT_LOCAL_PATH"] = str(build_qdrant / f"worker_{w_idx}")
                env["INGEST_QUEUE_DIR"] = str(w_dir)
                cmd = [
                    sys.executable, "-u", str(ROOT / "scripts" / "ingest.py"),
                    "--mode", "local", "--workers", "1",
                ]
                if args.dry_run:
                    cmd += ["--dry-run"]
                proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT, env=env)
                procs.append((w_idx, proc, log_path, w_dir, count))
                print(f"  Worker {w_idx}: PID {proc.pid}, {count} entries", flush=True)

            # Wait for all workers
            for w_idx, proc, log_path, w_dir, count in procs:
                proc.wait()
                print(f"  Worker {w_idx}: exit={proc.returncode}", flush=True)
                # Move done/failed back to main queue dirs
                w_done = w_dir / "done"
                w_failed = w_dir / "failed"
                for src_dir, dst_dir in [(w_done, DONE), (w_failed, FAILED)]:
                    if src_dir.exists():
                        for f in src_dir.glob("*.json"):
                            shutil.move(str(f), str(dst_dir / f.name))
                # Move any remaining pending back
                w_pending = w_dir / "pending"
                if w_pending.exists():
                    for f in w_pending.glob("*.json"):
                        shutil.move(str(f), str(PENDING / f.name))
                # Cleanup worker dir
                shutil.rmtree(str(w_dir), ignore_errors=True)

            for f in log_files:
                f.close()

            # Summary
            ok = len(list(DONE.glob("*.json")))
            fail = len(list(FAILED.glob("*.json")))
            print(f"  Results: {ok} done, {fail} failed", flush=True)

    print(f"\nDone: {ok} succeeded, {fail} failed")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(1)
