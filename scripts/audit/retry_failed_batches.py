#!/usr/bin/env python3
"""Retry failed batches from a backfill_registry_stubs outcome JSON.

Reads the outcome JSON's ``failed`` array, replays just those record-ranges
through /admin/create-stub-hoas with a smaller batch size and a longer
inter-batch sleep. Idempotent: already-created rows return ``updated`` and
new rows return ``created``.
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

sys.path.insert(0, str(ROOT / "scripts" / "audit"))
from backfill_registry_stubs import REGISTRIES, normalize_lead, live_admin_token  # noqa


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outcome", required=True, help="Path to a *_backfill.json outcome file")
    ap.add_argument("--state-key", required=True,
                    help="State key in the REGISTRIES dict that produced this outcome")
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    outcome = json.load(open(args.outcome))
    # outcome is {state: {prepared, created, ..., failed: [{batch_start, ...}, ...]}}
    cfg = REGISTRIES[args.state_key]
    state_value = cfg["state"]
    body_outcome = next(iter(outcome.values()))
    failed_list = body_outcome.get("failed") or []
    if not failed_list:
        print("no failed batches in this outcome; nothing to retry")
        return 0

    # Re-run normalize over the same files
    lead_paths: list[Path] = []
    if cfg.get("leads_glob"):
        lead_paths = sorted(ROOT.glob(cfg["leads_glob"]))
        lead_paths = [p for p in lead_paths if "REGISTRY_NOTES" not in p.name]
    elif cfg.get("leads"):
        lead_paths = [cfg["leads"]] if cfg["leads"].exists() else []

    records: list[dict] = []
    seen: set[str] = set()
    for lp in lead_paths:
        with open(lp) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    lead = json.loads(line)
                except Exception:
                    continue
                rec = normalize_lead(lead, state_value, cfg["source"])
                if not rec:
                    continue
                key = rec["name"].lower()
                if key in seen:
                    continue
                seen.add(key)
                records.append(rec)
    print(f"normalized {len(records)} records, retrying {len(failed_list)} failed batches")

    BATCH_PROD = 50  # batch_start spacing the original run used
    new_batch = max(1, args.batch_size)
    if not args.apply:
        for f in failed_list[:5]:
            i = f.get("batch_start") or 0
            n = min(BATCH_PROD, len(records) - i)
            print(f"  would retry records[{i}:{i+n}]  err={f.get('error') or f.get('http')}")
        print(f"... and {max(0, len(failed_list)-5)} more")
        print("Pass --apply to execute.")
        return 0

    token = live_admin_token()
    if not token:
        print("no admin token", file=sys.stderr)
        return 2

    created = 0
    updated = 0
    disambiguated = 0
    skipped = 0
    still_failed: list[dict] = []
    for f in failed_list:
        i = f.get("batch_start") or 0
        # Original batch was BATCH_PROD records starting at i. Retry in
        # smaller sub-batches to dodge the per-call 500.
        original_chunk = records[i : i + BATCH_PROD]
        for j in range(0, len(original_chunk), new_batch):
            chunk = original_chunk[j : j + new_batch]
            r = requests.post(
                "https://hoaproxy.org/admin/create-stub-hoas",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"records": chunk, "on_collision": "disambiguate"},
                timeout=180,
            )
            if r.status_code == 200:
                body = r.json()
                created += int(body.get("created", 0))
                updated += int(body.get("updated", 0))
                disambiguated += int(body.get("disambiguated", 0))
                skipped += int(body.get("skipped", 0))
                print(f"  retry [{i}+{j}]: created={body.get('created')} updated={body.get('updated')} disambiguated={body.get('disambiguated')}", flush=True)
            else:
                still_failed.append({"batch_start": i, "sub": j, "http": r.status_code, "body": r.text[:200]})
                print(f"  retry [{i}+{j}]: FAIL {r.status_code}", flush=True)
            time.sleep(2.0)

    print()
    print(f"DONE  created={created}  updated={updated}  disambiguated={disambiguated}  skipped={skipped}  still_failed={len(still_failed)}")
    return 0 if not still_failed else 1


if __name__ == "__main__":
    sys.exit(main())
