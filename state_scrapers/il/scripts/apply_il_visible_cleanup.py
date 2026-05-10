"""Apply heuristic-only Phase 10 cleanup on IL visible HOAs (no LLM).

The full LLM-driven cleanup is too slow for the IL summary backlog (1300+
records, ~30s/decision). This is a fast pass that only touches the 422
records that are *visible on the public site* (have geo). Two stages:

1. Rename — strip `[PDF] ` prefix from ~9 records whose remainder is a
   clean HOA name. Uses /admin/rename-hoa with merge-on-collision.
2. Delete — hard-delete ~86 records whose names are clearly doc fragments
   ([PDF] declaration..., Notice of, Pursuant, Whereas, etc.). Uses
   /admin/delete-hoa.

Inputs (pre-built by an inline classifier, not this script):
  /tmp/il_to_rename.json   — list of {hoa_id, hoa, new_name, why}
  /tmp/il_to_delete.json   — list of {hoa_id, hoa, why}

Concurrency: 6 parallel workers. Render is fragile under heavy concurrent
load, but rename/delete are cheap (no embedding) so 6 is safe.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests


def admin_rename(token: str, base_url: str, hoa_id: int, new_name: str, allow_merge: bool = True) -> dict:
    r = requests.post(
        f"{base_url}/admin/rename-hoa",
        json={"hoa_id": hoa_id, "new_name": new_name, "allow_merge": allow_merge},
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    return {"status": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text}


def admin_delete(token: str, base_url: str, hoa_id: int) -> dict:
    r = requests.post(
        f"{base_url}/admin/delete-hoa",
        json={"hoa_id": hoa_id},
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
    return {"status": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="https://hoaproxy.org")
    p.add_argument("--rename-file", default="/tmp/il_to_rename.json")
    p.add_argument("--delete-file", default="/tmp/il_to_delete.json")
    p.add_argument("--out-dir", default="state_scrapers/il/results/cleanup_visible_round4")
    p.add_argument("--apply", action="store_true", help="Actually call admin endpoints")
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args()

    token = os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")
    if not token:
        print("HOAPROXY_ADMIN_BEARER not set", file=sys.stderr)
        return 1

    rename_list = json.load(open(args.rename_file))
    delete_list = json.load(open(args.delete_file))
    print(f"to_rename: {len(rename_list)}, to_delete: {len(delete_list)}", file=sys.stderr)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "apply_log.jsonl"
    log_f = log_path.open("w")

    if not args.apply:
        print("dry-run; pass --apply to execute", file=sys.stderr)
        return 0

    rename_ok = 0
    rename_err = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(admin_rename, token, args.base_url, r["hoa_id"], r["new_name"]): r
            for r in rename_list
        }
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                resp = fut.result()
            except Exception as exc:
                resp = {"status": 0, "error": f"{type(exc).__name__}: {exc}"}
            entry = {"action": "rename", **r, "response": resp}
            log_f.write(json.dumps(entry, default=str) + "\n")
            log_f.flush()
            if resp.get("status") == 200:
                rename_ok += 1
            else:
                rename_err += 1
                print(f"  rename ERR {r['hoa_id']} -> {r['new_name']!r}: {resp}", file=sys.stderr)
    print(f"rename: ok={rename_ok} err={rename_err}", file=sys.stderr)

    delete_ok = 0
    delete_err = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(admin_delete, token, args.base_url, r["hoa_id"]): r
            for r in delete_list
        }
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                resp = fut.result()
            except Exception as exc:
                resp = {"status": 0, "error": f"{type(exc).__name__}: {exc}"}
            entry = {"action": "delete", **r, "response": resp}
            log_f.write(json.dumps(entry, default=str) + "\n")
            log_f.flush()
            if resp.get("status") == 200:
                delete_ok += 1
            else:
                delete_err += 1
                print(f"  delete ERR {r['hoa_id']} ({r['hoa']!r}): {resp}", file=sys.stderr)
    print(f"delete: ok={delete_ok} err={delete_err}", file=sys.stderr)

    log_f.close()
    summary = {
        "rename_ok": rename_ok, "rename_err": rename_err,
        "delete_ok": delete_ok, "delete_err": delete_err,
        "log": str(log_path),
    }
    print(json.dumps(summary, indent=2))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
