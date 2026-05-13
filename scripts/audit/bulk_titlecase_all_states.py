#!/usr/bin/env python3
"""Bulk-titlecase ALL-CAPS HOA names across every state.

Generalizes ``state_scrapers/ny/scripts/bulk_titlecase_ny_names.py`` to the
full 50-state corpus. Pulls every hoa_locations row via
``/admin/list-corruption-targets`` (uses the same ``KNOWN_SOURCES`` list as
``scripts/audit/repair_oob_locations.py``), filters to names whose
alphabetic-uppercase ratio crosses ``--min-upper-ratio`` (default 0.70) and
whose ``smart_titlecase`` output differs from the input, then POSTs renames
to ``/admin/rename-hoa`` in 50-record batches.

Idempotent: re-running after a successful pass produces 0 candidates.

Per-state ledger lands at
``state_scrapers/_orchestrator/titlecase_{date}/all_state_renames.jsonl``
and a summary at the same directory.

Usage::

    # dry-run, write candidates only
    .venv/bin/python scripts/audit/bulk_titlecase_all_states.py

    # apply renames
    .venv/bin/python scripts/audit/bulk_titlecase_all_states.py --apply
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hoaware.name_utils import smart_titlecase  # noqa: E402

# Reuse the curated source list from the OOB repair script (117 entries).
REPAIR_PATH = ROOT / "scripts" / "audit" / "repair_oob_locations.py"
_spec = importlib.util.spec_from_file_location("_repair", REPAIR_PATH)
_repair = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_repair)  # type: ignore[union-attr]
KNOWN_SOURCES: tuple[str, ...] = _repair.KNOWN_SOURCES

DEFAULT_BASE_URL = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")
BATCH = 25
SLEEP_S = 1.0
MAX_RETRIES = 4         # per batch, exponential backoff: 2s, 4s, 8s, 16s
CIRCUIT_BREAK = 5       # abort if this many consecutive batches all fail


def live_admin_token() -> str | None:
    return os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")


def alpha_upper_ratio(name: str) -> float:
    alpha = [c for c in name if c.isalpha()]
    if len(alpha) < 8:
        return 0.0
    upper = sum(1 for c in alpha if c.isupper())
    return upper / len(alpha)


def fetch_all_rows(base_url: str, token: str) -> list[dict]:
    r = requests.post(
        f"{base_url}/admin/list-corruption-targets",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"sources": list(KNOWN_SOURCES), "require_lat": False},
        timeout=600,
    )
    r.raise_for_status()
    rows = r.json().get("rows", [])
    print(f"Fetched {len(rows)} rows across {len(KNOWN_SOURCES)} sources",
          file=sys.stderr)
    return rows


def build_candidates(
    rows: list[dict], min_ratio: float
) -> list[dict[str, Any]]:
    seen_ids: set[int] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        hid = row.get("hoa_id")
        if not isinstance(hid, int) or hid in seen_ids:
            continue
        seen_ids.add(hid)
        name = row.get("hoa") or ""
        if not name:
            continue
        ratio = alpha_upper_ratio(name)
        if ratio < min_ratio:
            continue
        new = smart_titlecase(name)
        if not new or new == name:
            continue
        out.append({
            "hoa_id": hid,
            "old_name": name,
            "new_name": new,
            "state": (row.get("state") or "").upper(),
            "upper_ratio": round(ratio, 2),
            "source": row.get("source"),
        })
    return out


def _post_with_backoff(
    base_url: str, headers: dict, items: list[dict]
) -> tuple[int, dict | None, str | None]:
    """Single batch POST with exponential backoff on 5xx / network errors.

    Returns (status_code, parsed_body_or_None, error_text_or_None).
    On final failure, status_code is the last seen code or 0 for network.
    """
    delay = 2.0
    last_status = 0
    last_err = "no attempt"
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.post(
                f"{base_url}/admin/rename-hoa",
                headers=headers,
                json={"renames": items, "dry_run": False},
                timeout=180,
            )
        except requests.RequestException as e:
            last_err = f"network: {e}"
            last_status = 0
        else:
            if r.status_code == 200:
                try:
                    return 200, r.json(), None
                except ValueError:
                    return 200, None, "200 but invalid json"
            last_status = r.status_code
            last_err = r.text[:200]
            # 4xx is a client error — don't retry, fail fast
            if 400 <= r.status_code < 500:
                return last_status, None, last_err
        if attempt < MAX_RETRIES:
            time.sleep(delay)
            delay *= 2
    return last_status, None, last_err


def load_done_ids(ledger_path: Path) -> set[int]:
    if not ledger_path.exists():
        return set()
    done: set[int] = set()
    with ledger_path.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            hid = r.get("hoa_id")
            if isinstance(hid, int):
                done.add(hid)
    return done


def apply_renames(
    candidates: list[dict[str, Any]],
    base_url: str,
    token: str,
    out_path: Path,
) -> dict[str, int]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    totals = {"renamed": 0, "merged": 0, "noop": 0, "errors": 0, "batches": 0,
              "failed_batches": 0}

    done = load_done_ids(out_path)
    if done:
        before = len(candidates)
        candidates = [c for c in candidates if c["hoa_id"] not in done]
        print(f"  resume: {len(done)} hoa_ids already in ledger; "
              f"{before - len(candidates)} skipped, {len(candidates)} to go",
              file=sys.stderr)

    consecutive_failures = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as out_f:
        for i in range(0, len(candidates), BATCH):
            chunk = candidates[i:i + BATCH]
            items = [{"hoa_id": c["hoa_id"], "new_name": c["new_name"]} for c in chunk]
            status, body, err = _post_with_backoff(base_url, headers, items)
            if status != 200 or body is None:
                consecutive_failures += 1
                totals["errors"] += len(chunk)
                totals["failed_batches"] += 1
                print(
                    f"  batch {i//BATCH} FAILED after retries: HTTP {status} {err}",
                    file=sys.stderr,
                )
                if consecutive_failures >= CIRCUIT_BREAK:
                    print(
                        f"  circuit-break: {CIRCUIT_BREAK} consecutive batch "
                        f"failures — aborting. Re-run to resume from ledger.",
                        file=sys.stderr,
                    )
                    return totals
                time.sleep(SLEEP_S * 4)
                continue
            consecutive_failures = 0
            totals["renamed"] += int(body.get("renamed", 0))
            totals["merged"] += int(body.get("merged", 0))
            totals["noop"] += int(body.get("noop", 0))
            totals["errors"] += int(body.get("errors", 0))
            totals["batches"] += 1
            results = body.get("results") or []
            ts = datetime.now(timezone.utc).isoformat()
            for cand, res in zip(chunk, results):
                out_f.write(json.dumps({
                    "ts": ts,
                    "hoa_id": cand["hoa_id"],
                    "old_name": cand["old_name"],
                    "new_name": cand["new_name"],
                    "state": cand["state"],
                    "outcome": res.get("status") or res.get("outcome"),
                    "merged_into_id": res.get("merged_into_id"),
                }, ensure_ascii=False) + "\n")
                out_f.flush()
            if i % (BATCH * 20) == 0:
                print(
                    f"  [{i}/{len(candidates)}] "
                    f"renamed={totals['renamed']} merged={totals['merged']} "
                    f"errors={totals['errors']}",
                    file=sys.stderr,
                )
            time.sleep(SLEEP_S)
    return totals


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-upper-ratio", type=float, default=0.70)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument(
        "--run-id",
        default=f"titlecase_{datetime.now(timezone.utc).strftime('%Y_%m_%d')}",
    )
    ap.add_argument(
        "--only-states",
        default="",
        help="Comma-separated state codes (default: all)",
    )
    args = ap.parse_args()

    token = live_admin_token()
    if not token:
        print("FATAL: no admin token", file=sys.stderr)
        return 2

    run_dir = ROOT / "state_scrapers" / "_orchestrator" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    rows = fetch_all_rows(args.base_url, token)
    if args.only_states:
        only = {s.strip().upper() for s in args.only_states.split(",") if s.strip()}
        rows = [r for r in rows if (r.get("state") or "").upper() in only]
        print(f"After --only-states filter: {len(rows)} rows", file=sys.stderr)

    candidates = build_candidates(rows, args.min_upper_ratio)
    by_state: dict[str, int] = {}
    for c in candidates:
        by_state[c["state"]] = by_state.get(c["state"], 0) + 1
    print(f"\nCandidates: {len(candidates)} across {len(by_state)} states",
          file=sys.stderr)
    for st in sorted(by_state, key=lambda s: -by_state[s])[:15]:
        print(f"  {st}: {by_state[st]}", file=sys.stderr)

    cand_path = run_dir / "titlecase_candidates.jsonl"
    with cand_path.open("w", encoding="utf-8") as f:
        for c in candidates:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"Wrote candidates to {cand_path}", file=sys.stderr)

    if not args.apply:
        print("\nDRY-RUN. Sample (20):", file=sys.stderr)
        for c in candidates[:20]:
            print(
                f"  {c['state']} | {c['old_name'][:55]:55s} -> {c['new_name']}",
                file=sys.stderr,
            )
        return 0

    out_path = run_dir / "all_state_renames.jsonl"
    print(f"\nApplying {len(candidates)} renames to {args.base_url} ...",
          file=sys.stderr)
    t0 = time.monotonic()
    totals = apply_renames(candidates, args.base_url, token, out_path)
    elapsed = time.monotonic() - t0
    summary = {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(elapsed, 1),
        "candidates": len(candidates),
        "totals": totals,
        "per_state_counts": by_state,
        "min_upper_ratio": args.min_upper_ratio,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
