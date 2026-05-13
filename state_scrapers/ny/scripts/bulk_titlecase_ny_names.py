#!/usr/bin/env python3
"""Bulk-titlecase NY HOA names that are mostly uppercase.

The default ``clean_dirty_hoa_names.py`` pipeline (`is_dirty()` + LLM rename)
is expensive and only catches names flagged by the `shouting_prefix` rule.
For NY's 4,000+ all-caps names that don't trip that specific heuristic
(e.g. `"100 WEST 33RD STREET CORP."`, which starts with digits so doesn't
match the leading-caps rule), this script applies the deterministic
``smart_titlecase`` directly without any LLM call.

Heuristic: alphabetic-uppercase ratio > 70% AND name has at least 8
alphabetic characters. Skips names that already mix case.

Usage:
    .venv/bin/python state_scrapers/ny/scripts/bulk_titlecase_ny_names.py [--apply]

Always dry-run unless --apply. Batches 50 renames per /admin/rename-hoa call.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)

from hoaware.name_utils import smart_titlecase  # noqa: E402

BASE_URL = "https://hoaproxy.org"
BATCH = 50
SLEEP_S = 0.5


def alpha_upper_ratio(name: str) -> float:
    alpha = [c for c in name if c.isalpha()]
    if len(alpha) < 8:
        return 0.0
    upper = sum(1 for c in alpha if c.isupper())
    return upper / len(alpha)


def fetch_all_ny(token: str) -> list[dict]:
    """Pull all NY HOAs via /admin/list-corruption-targets which has no rate
    limit and lets us filter by hoa_locations.source. Uses the two known NY
    source strings (DOS registry stubs + ACRIS-derived bank manifests).
    """
    r = requests.post(
        f"{BASE_URL}/admin/list-corruption-targets",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"sources": [
            "ny-dos-active-corporations",
            "ny-acris-decl-2026-05",
            "gcs_prepared_ingest",  # legacy/import-time source
        ]},
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    rows = data.get("rows", [])
    print(f"  fetched via /admin/list-corruption-targets: {len(rows)} NY rows",
          file=sys.stderr)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-upper-ratio", type=float, default=0.70)
    ap.add_argument(
        "--out",
        default="state_scrapers/ny/results/ny_20260510_213944Z_claude/titlecase_renames.jsonl",
    )
    args = ap.parse_args()

    token = os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")
    if not token:
        raise SystemExit("no admin token")

    print("Fetching all NY HOAs...", file=sys.stderr)
    rows = fetch_all_ny(token)
    rows = [r for r in rows if (r.get("state") or "").upper() == "NY"]
    print(f"Total NY: {len(rows)}", file=sys.stderr)

    candidates: list[dict[str, Any]] = []
    for row in rows:
        name = row.get("hoa") or ""
        ratio = alpha_upper_ratio(name)
        if ratio < args.min_upper_ratio:
            continue
        new_name = smart_titlecase(name)
        if new_name == name or not new_name:
            continue
        candidates.append({
            "hoa_id": row["hoa_id"],
            "old_name": name,
            "new_name": new_name,
            "upper_ratio": round(ratio, 2),
        })

    print(f"Candidates: {len(candidates)}", file=sys.stderr)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        for c in candidates:
            f.write(json.dumps(c) + "\n")
    print(f"Wrote candidates to {out}", file=sys.stderr)

    if not args.apply:
        print("DRY-RUN. Sample:", file=sys.stderr)
        for c in candidates[:10]:
            print(f"  {c['old_name'][:55]:55s} -> {c['new_name']}", file=sys.stderr)
        return 0

    # Apply in batches.
    renamed = merged = noop = errors = 0
    for i in range(0, len(candidates), BATCH):
        chunk = candidates[i:i + BATCH]
        items = [{"hoa_id": c["hoa_id"], "new_name": c["new_name"]} for c in chunk]
        try:
            r = requests.post(
                f"{BASE_URL}/admin/rename-hoa",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"renames": items, "dry_run": False},
                timeout=120,
            )
        except requests.exceptions.RequestException as e:
            print(f"  batch {i//BATCH} request error: {e}", file=sys.stderr)
            time.sleep(SLEEP_S)
            continue
        if r.status_code != 200:
            print(f"  batch {i//BATCH} HTTP {r.status_code}: {r.text[:200]}",
                  file=sys.stderr)
            time.sleep(SLEEP_S)
            continue
        body = r.json()
        renamed += int(body.get("renamed", 0))
        merged += int(body.get("merged", 0))
        noop += int(body.get("noop", 0))
        errors += int(body.get("errors", 0))
        print(
            f"  batch {i//BATCH}: renamed={body.get('renamed',0)} "
            f"merged={body.get('merged',0)} noop={body.get('noop',0)} "
            f"errors={body.get('errors',0)}",
            file=sys.stderr,
        )
        time.sleep(SLEEP_S)

    print(json.dumps({
        "summary": "ny_bulk_titlecase",
        "candidates": len(candidates),
        "renamed": renamed,
        "merged": merged,
        "noop": noop,
        "errors": errors,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
