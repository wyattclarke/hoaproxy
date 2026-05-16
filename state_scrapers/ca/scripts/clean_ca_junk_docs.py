#!/usr/bin/env python3
"""CA-specific junk-doc cleanup: route ALL junk verdicts through
``/admin/clear-hoa-docs`` (preserve entity + geometry; drop docs only).

Why CA-specific: the generic ``scripts/audit/clean_junk_docs.py``
heuristic flags 70 of CA's 641 junk entries as ``delete_entity`` because
their names lack a recognized HOA suffix (Association/Owners/etc.).
Manual review shows these are all real registered CA entities — CA
maintenance corporations, mutual housing co-ops, senior-housing manors,
Davis-Stirling-style master associations whose legal name doesn't carry
the suffix — and 0 of 70 hit the ``JUNK_NAME_FRAGMENTS`` regex. Deleting
them would repeat the 2026-05-09 geometry-loss incident.

CA's content-quality problem is **document-level**, not entity-level.
Every junk verdict here means "this entity has junk docs banked"; the
entity itself is real. So we clear docs and leave the entity as a
docless stub.

Usage:
    python state_scrapers/ca/scripts/clean_ca_junk_docs.py            # dry-run
    python state_scrapers/ca/scripts/clean_ca_junk_docs.py --apply
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

GRADES = ROOT / "state_scrapers" / "ca" / "results" / "audit_2026_05_09" / "ca_grades.json"
DEFAULT_BASE = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")


def live_admin_token() -> str | None:
    # Explicit override wins; otherwise pull from settings.env.
    # Render env-vars fallback removed 2026-05-16 (Hetzner cutover).
    return os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")


def post_with_retry(url: str, *, token: str, payload: dict,
                    timeout: int = 180, retries: int = 5) -> tuple[bool, dict | None, str | None]:
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            if r.status_code == 200:
                return True, r.json(), None
            last_err = f"http {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(8 + attempt * 6)
    return False, None, last_err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grades", default=str(GRADES))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--batch-size", type=int, default=15)
    ap.add_argument("--max-process", type=int, default=1000)
    ap.add_argument("--ack-large", action="store_true")
    ap.add_argument("--out",
                    default="state_scrapers/ca/results/audit_2026_05_09/clean_outcome.json")
    args = ap.parse_args()

    data = json.loads(Path(args.grades).read_text())
    results = data.get("results", [])
    junk = [r for r in results if r.get("verdict") == "junk"
            and isinstance(r.get("hoa_id"), int)]
    print(f"[clean-ca] {len(junk)} junk verdicts (all route to /admin/clear-hoa-docs)")

    if len(junk) > args.max_process and not args.ack_large:
        print(f"refusing: {len(junk)} > --max-process={args.max_process}; "
              f"pass --ack-large to override", file=sys.stderr)
        return 2

    if not args.apply:
        print("\n=== Sample of clear_docs targets (preserve entity, drop docs) ===")
        for r in junk[:10]:
            print(f"  id={r.get('hoa_id'):>6} {(r.get('hoa') or '')[:55]}")
            print(f"      cat: {r.get('category')}  reason: {(r.get('reason') or '')[:90]}")
        print(f"\nPass --apply to clear docs on all {len(junk)} entities.")
        return 0

    token = live_admin_token()
    if not token:
        print("[clean-ca] no admin token", file=sys.stderr)
        return 2

    BATCH = max(1, args.batch_size)
    cleared_total = 0
    failures: list[dict] = []
    per_call: list[dict] = []

    for i in range(0, len(junk), BATCH):
        chunk = junk[i : i + BATCH]
        ids = [r["hoa_id"] for r in chunk]
        url = f"{args.base_url}/admin/clear-hoa-docs"
        ok, body, err = post_with_retry(url, token=token, payload={"hoa_ids": ids})
        batch_num = i // BATCH + 1
        total_batches = (len(junk) - 1) // BATCH + 1
        if ok:
            n = int((body or {}).get("cleared", 0))
            cleared_total += n
            print(f"  batch {batch_num}/{total_batches}  cleared={n}  ids={ids[:5]}{'…' if len(ids)>5 else ''}",
                  flush=True)
            per_call.append({"batch": batch_num, "ids": ids, "cleared": n, "body": body})
        else:
            failures.append({"batch": batch_num, "ids": ids, "error": err})
            print(f"  batch {batch_num}/{total_batches}  FAIL  {err}", flush=True)
        time.sleep(1.5)

    summary = {
        "junk_total": len(junk),
        "cleared": cleared_total,
        "failures": failures,
        "per_call_count": len(per_call),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: summary[k] for k in
                       ("junk_total", "cleared", "per_call_count")}, indent=2))
    print(f"summary -> {out_path}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
