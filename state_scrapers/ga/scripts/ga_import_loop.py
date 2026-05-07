"""Drain ready GA bundles via /admin/ingest-ready-gcs.

Reads JWT_SECRET from settings.env. Calls the live endpoint in batches
until the prepared bucket has no remaining `ready` bundles in v1/GA/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

SETTINGS_PATH = Path(__file__).resolve().parents[3] / "settings.env"


def _load_jwt_secret() -> str:
    """Resolve the live JWT_SECRET.

    Order: HOAPROXY_ADMIN_BEARER, then Render API (RENDER_API_KEY +
    RENDER_SERVICE_ID), then local JWT_SECRET as last resort.
    """
    if os.environ.get("HOAPROXY_ADMIN_BEARER"):
        return os.environ["HOAPROXY_ADMIN_BEARER"]
    api_key = os.environ.get("RENDER_API_KEY")
    service_id = os.environ.get("RENDER_SERVICE_ID")
    if api_key and service_id:
        try:
            r = requests.get(
                f"https://api.render.com/v1/services/{service_id}/env-vars",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            r.raise_for_status()
            for env in r.json():
                e = env.get("envVar", env)
                if e.get("key") == "JWT_SECRET" and e.get("value"):
                    return e["value"]
            print("Render env-vars listing did not include JWT_SECRET", file=sys.stderr)
        except requests.RequestException as exc:
            print(f"Render API lookup failed: {exc}", file=sys.stderr)
    if "JWT_SECRET" in os.environ and os.environ["JWT_SECRET"]:
        print("Falling back to local JWT_SECRET", file=sys.stderr)
        return os.environ["JWT_SECRET"]
    raise SystemExit("Could not resolve live JWT_SECRET")


def _count_ready(state: str, bucket_name: str) -> int:
    from google.cloud import storage

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    n = 0
    for blob in bucket.list_blobs(prefix=f"v1/{state.upper()}/"):
        if not blob.name.endswith("/status.json"):
            continue
        try:
            data = json.loads(blob.download_as_bytes())
        except Exception:
            continue
        if data.get("status") == "ready":
            n += 1
    return n


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", default="GA")
    p.add_argument("--base-url", default="https://hoaproxy.org")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--max-batches", type=int, default=200)
    p.add_argument("--sleep-s", type=float, default=1.0)
    p.add_argument(
        "--bucket",
        default=os.environ.get("HOA_PREPARED_GCS_BUCKET", "hoaproxy-ingest-ready"),
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    secret = _load_jwt_secret()
    headers = {"Authorization": f"Bearer {secret}"}
    url = f"{args.base_url.rstrip('/')}/admin/ingest-ready-gcs"

    pre = _count_ready(args.state, args.bucket)
    print(f"ready bundles in v1/{args.state.upper()}/: {pre}")
    if pre == 0:
        return 0

    totals = {"imported": 0, "skipped": 0, "errors": 0}
    for batch in range(1, args.max_batches + 1):
        params = {
            "state": args.state.upper(),
            "limit": args.limit,
            "dry_run": str(args.dry_run).lower(),
        }
        try:
            r = requests.post(url, headers=headers, params=params, timeout=600)
        except requests.RequestException as exc:
            print(f"batch {batch}: request failed: {exc}", file=sys.stderr)
            totals["errors"] += 1
            time.sleep(5)
            continue
        if r.status_code != 200:
            print(f"batch {batch}: HTTP {r.status_code} — {r.text[:300]}", file=sys.stderr)
            if r.status_code in (502, 503, 504, 429):
                time.sleep(15)
                continue
            return 1
        payload = r.json()
        results = payload.get("results") or []
        if not results:
            print(f"batch {batch}: no ready bundles returned, stopping")
            break
        for entry in results:
            status = entry.get("status") or "?"
            if status == "imported":
                totals["imported"] += 1
            elif status == "skipped":
                totals["skipped"] += 1
            else:
                totals["errors"] += 1
        print(
            f"batch {batch}: imported={sum(1 for e in results if e.get('status') == 'imported')} "
            f"skipped={sum(1 for e in results if e.get('status') == 'skipped')} "
            f"other={sum(1 for e in results if e.get('status') not in ('imported','skipped'))}"
        )
        # show first error in each batch
        for entry in results:
            if entry.get("status") not in ("imported", "skipped"):
                print(f"  ! {entry.get('prefix')} :: {entry.get('status')} :: {entry.get('reason') or entry.get('error')}")
                break
        time.sleep(args.sleep_s)

    print(json.dumps({"totals": totals}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
