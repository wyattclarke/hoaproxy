#!/usr/bin/env python3
"""Delete HOAs from prod whose content is junk per the LLM grader.

Reads a grading JSON (from grade_hoa_text_quality.py) and deletes every
HOA where verdict == "junk". Skips "real", "no_docs", and "error".

Always runs as dry-run unless --apply is set. Always confirms before
deleting more than --max-delete entities.
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

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / "settings.env")

DEFAULT_BASE_URL = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")


def live_admin_token() -> str | None:
    if os.environ.get("HOAPROXY_ADMIN_BEARER"):
        return os.environ["HOAPROXY_ADMIN_BEARER"]
    api_key = os.environ.get("RENDER_API_KEY")
    service_id = os.environ.get("RENDER_SERVICE_ID")
    if api_key and service_id:
        try:
            r = requests.get(
                f"https://api.render.com/v1/services/{service_id}/env-vars",
                headers={"Authorization": f"Bearer {api_key}"}, timeout=30,
            )
            r.raise_for_status()
            for env in r.json():
                e = env.get("envVar", env)
                if e.get("key") == "JWT_SECRET" and e.get("value"):
                    return e["value"]
        except Exception:
            pass
    return os.environ.get("JWT_SECRET")


def delete_hoas_batch(hoa_ids: list[int], *, base_url: str, token: str) -> dict:
    last_err = None
    for attempt in range(5):
        try:
            r = requests.post(
                f"{base_url}/admin/delete-hoa",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"hoa_ids": hoa_ids},
                timeout=180,
            )
            if r.status_code == 200:
                return {"ok": True, "body": r.json()}
            last_err = f"http {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(8 + attempt * 8)
    return {"ok": False, "error": last_err}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grades", required=True, help="JSON file from grade_hoa_text_quality.py")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--max-delete", type=int, default=2000,
                    help="Refuse to delete more than this without explicit ack")
    ap.add_argument("--ack-large", action="store_true",
                    help="Acknowledge deletes exceeding --max-delete")
    ap.add_argument("--out", default=None, help="Write outcome JSON here")
    args = ap.parse_args()

    data = json.loads(Path(args.grades).read_text())
    results = data.get("results") or []
    junk = [r for r in results if r.get("verdict") == "junk"]
    print(f"[delete] {len(junk)} junk HOAs in grading file (state={data.get('state')})")
    if not junk:
        return 0

    if len(junk) > args.max_delete and not args.ack_large:
        print(f"[delete] refusing: {len(junk)} > --max-delete={args.max_delete}; pass --ack-large to override")
        return 2

    if not args.apply:
        for r in junk[:20]:
            print(f"  would delete: id={r.get('hoa_id'):>6}  {r.get('hoa')}  ({r.get('category')})")
        if len(junk) > 20:
            print(f"  …and {len(junk) - 20} more")
        return 0

    token = live_admin_token()
    if not token:
        print("[delete] no admin token (HOAPROXY_ADMIN_BEARER / JWT_SECRET via Render API)")
        return 2

    valid = [r for r in junk if isinstance(r.get("hoa_id"), int)]
    deleted: list[dict] = []
    failed: list[dict] = []
    BATCH = 15
    total_batches = (len(valid) + BATCH - 1) // BATCH
    for bi in range(total_batches):
        chunk = valid[bi * BATCH : (bi + 1) * BATCH]
        ids = [r["hoa_id"] for r in chunk]
        out = delete_hoas_batch(ids, base_url=args.base_url, token=token)
        if out.get("ok"):
            body = out.get("body") or {}
            for entry in body.get("results", []) or body.get("deleted", []) or []:
                # Endpoint shape: {"results":[{"hoa_id":..., "status":"deleted"|"not_found", ...}]}
                if entry.get("status") in ("deleted", "ok") or entry.get("deleted"):
                    deleted.append(entry)
            print(f"[delete] batch {bi+1}/{total_batches} ok  ids={ids[:5]}{'...' if len(ids)>5 else ''}", flush=True)
        else:
            for r in chunk:
                failed.append({"hoa_id": r["hoa_id"], "hoa": r.get("hoa"), "error": out.get("error")})
            print(f"[delete] batch {bi+1}/{total_batches} FAIL  {out.get('error')}", flush=True)
        time.sleep(2.0)  # gentle pacing between batches

    summary = {
        "state": data.get("state"),
        "attempted": len(junk),
        "deleted": len(deleted),
        "failed": len(failed),
        "failures": failed[:50],
    }
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: summary[k] for k in ("state", "attempted", "deleted", "failed")}, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
