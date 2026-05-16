#!/usr/bin/env python3
"""Retry the failed /admin/clear-hoa-docs batches from the main CA cleanup.

Reads state_scrapers/ca/results/audit_2026_05_09/clean_outcome.json, pulls
the 75 ids from the 5 failed batches (Render 502s during the main run),
and retries them with smaller batch size (5 instead of 15) and longer
gaps between batches (5s instead of 1.5s) to reduce Render load.

Usage:
    python state_scrapers/ca/scripts/retry_ca_cleanup_failures.py            # dry-run
    python state_scrapers/ca/scripts/retry_ca_cleanup_failures.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / "settings.env")

CLEAN_OUTCOME = ROOT / "state_scrapers" / "ca" / "results" / "audit_2026_05_09" / "clean_outcome.json"
DEFAULT_BASE = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")


def live_admin_token() -> str | None:
    # Explicit override wins; otherwise pull from settings.env.
    # Render env-vars fallback removed 2026-05-16 (Hetzner cutover).
    return os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")


def post_clear(url: str, *, token: str, ids: list[int]) -> tuple[bool, dict | None, str | None]:
    last_err = None
    for attempt in range(6):
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json={"hoa_ids": ids},
                timeout=300,
            )
            if r.status_code == 200:
                return True, r.json(), None
            last_err = f"http {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(10 + attempt * 8)
    return False, None, last_err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outcome", default=str(CLEAN_OUTCOME))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--batch-size", type=int, default=5)
    ap.add_argument("--gap", type=float, default=5.0,
                    help="Seconds between successful batches")
    ap.add_argument("--out",
                    default="state_scrapers/ca/results/audit_2026_05_09/retry_outcome.json")
    args = ap.parse_args()

    d = json.loads(Path(args.outcome).read_text())
    failed_ids: list[int] = []
    for f in d.get("failures", []):
        failed_ids.extend(f.get("ids") or [])

    print(f"[retry] {len(failed_ids)} ids to retry (from {len(d.get('failures', []))} failed batches)")

    if not args.apply:
        print(f"first 10: {failed_ids[:10]}")
        print("Pass --apply to execute.")
        return 0

    token = live_admin_token()
    if not token:
        print("[retry] no admin token", file=sys.stderr)
        return 2

    BATCH = args.batch_size
    cleared_total = 0
    failures: list[dict] = []

    for i in range(0, len(failed_ids), BATCH):
        chunk = failed_ids[i : i + BATCH]
        url = f"{args.base_url}/admin/clear-hoa-docs"
        batch_n = i // BATCH + 1
        total_batches = (len(failed_ids) - 1) // BATCH + 1
        ok, body, err = post_clear(url, token=token, ids=chunk)
        if ok:
            n = int((body or {}).get("cleared", 0))
            cleared_total += n
            print(f"  retry-batch {batch_n}/{total_batches}  cleared={n}  ids={chunk}",
                  flush=True)
        else:
            failures.append({"batch": batch_n, "ids": chunk, "error": err})
            print(f"  retry-batch {batch_n}/{total_batches}  FAIL  {err}", flush=True)
        time.sleep(args.gap)

    summary = {
        "retry_total": len(failed_ids),
        "cleared": cleared_total,
        "failures": failures,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: summary[k] for k in ("retry_total", "cleared")}, indent=2))
    print(f"summary -> {out}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
