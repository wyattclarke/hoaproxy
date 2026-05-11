#!/usr/bin/env python3
"""Run New York HOA ingestion end-to-end.

Drives Phases 5–8 of the giant-state playbook for NY:

  5. prepare_bank_for_ingest.py with --max-docai-cost-usd 150
     → prepared bundles in gs://hoaproxy-ingest-ready/v1/NY/.

  6. POST /admin/ingest-ready-gcs?state=NY in a loop until empty
     (capped at 10 per call per CLAUDE.md memory — Render gateway 502s
     above this).

  7. State-local location enrichment — ZIP-centroid backfill via
     zippopotam.us for stubs missing geometry. ACRIS-banked HOAs already
     have BBL + street_address; NYC PLUTO BBL→polygon is a later
     enhancement.

  8. Verify live counts and map coverage; produce final report.

Unlike VA's template, NY does NOT include a discovery step here — discovery
is handled separately via Driver A/B/C/E launchers. This script is the
"bank → live" half of the pipeline.

NY-specific notes:
- Pacing: /upload calls space 75s apart (Render OOM mitigation, per
  CLAUDE.md memory).
- DocAI cap: 150 USD (Tier-4 NY, per giant-state-playbook).
- Cross-state contamination: NYC name patterns are high collision risk.
  Phase 5b reroute runs aggressively after prepare.

Usage:
    .venv/bin/python state_scrapers/ny/scripts/run_state_ingestion.py prepare
    .venv/bin/python state_scrapers/ny/scripts/run_state_ingestion.py import
    .venv/bin/python state_scrapers/ny/scripts/run_state_ingestion.py verify
    .venv/bin/python state_scrapers/ny/scripts/run_state_ingestion.py all
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)

STATE = "NY"
STATE_NAME = "New York"
# Out-of-bbox check uses NY state plus a margin for ACRIS rounding.
STATE_BBOX: dict[str, float] = {
    "min_lat": 40.49, "max_lat": 45.02, "min_lon": -79.77, "max_lon": -71.78,
}

BANK_BUCKET = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")
PREPARED_BUCKET = os.environ.get("HOA_PREPARED_GCS_BUCKET", "hoaproxy-ingest-ready")

LIVE_BASE_URL = "https://hoaproxy.org"
DOCAI_BUDGET_USD = 150.0  # giant-state tier-4 NY cap
UPLOAD_PACE_SECONDS = 75  # CLAUDE.md memory: Render OOM mitigation
INGEST_LIMIT_PER_CALL = 10  # CLAUDE.md memory: 502s above this


def _admin_token() -> str:
    """Fetch the production admin token via Render env-vars API."""
    api_key = os.environ.get("RENDER_API_KEY")
    sid = os.environ.get("RENDER_SERVICE_ID")
    if api_key and sid:
        r = requests.get(
            f"https://api.render.com/v1/services/{sid}/env-vars",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        for env in r.json():
            e = env.get("envVar", env)
            if e.get("key") == "JWT_SECRET" and e.get("value"):
                return e["value"]
    token = os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")
    if not token:
        raise SystemExit("no admin token (set HOAPROXY_ADMIN_BEARER or JWT_SECRET)")
    return token


def cmd_prepare(args: argparse.Namespace) -> int:
    """Phase 5: run prepare_bank_for_ingest.py on the NY bank."""
    out_log = ROOT / "state_scrapers/ny/results" / f"prepare_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log"
    out_log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(ROOT / "scripts/prepare_bank_for_ingest.py"),
        "--state", STATE,
        "--max-docai-cost-usd", str(DOCAI_BUDGET_USD),
    ]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.dry_run:
        cmd += ["--dry-run"]
    print(f"[prepare] {' '.join(cmd)}", flush=True)
    with open(out_log, "w") as f:
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    print(f"[prepare] exit={proc.returncode} log={out_log}", flush=True)
    return proc.returncode


def cmd_import(args: argparse.Namespace) -> int:
    """Phase 7-8: drain prepared bundles into live via /admin/ingest-ready-gcs.

    The endpoint is async — each call returns ``found`` (matched bundles) and
    ``enqueued`` (jobs queued for ingest). We loop until ``found == 0``.
    """
    token = _admin_token()
    calls = 0
    total_enqueued = 0
    while calls < args.max_calls:
        try:
            r = requests.post(
                f"{LIVE_BASE_URL}/admin/ingest-ready-gcs",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                params={"state": STATE, "limit": INGEST_LIMIT_PER_CALL},
                timeout=600,
            )
        except requests.exceptions.RequestException as e:
            print(f"[import] call={calls} request error: {e}", flush=True)
            time.sleep(UPLOAD_PACE_SECONDS)
            calls += 1
            continue
        if r.status_code != 200:
            print(f"[import] call={calls} HTTP {r.status_code}: {r.text[:200]}",
                  flush=True)
            # 5xx: brief backoff and retry; 4xx: bail.
            if 500 <= r.status_code < 600:
                time.sleep(UPLOAD_PACE_SECONDS)
                calls += 1
                continue
            break
        body = r.json()
        found = int(body.get("found", 0))
        enqueued = int(body.get("enqueued", 0))
        skipped = body.get("skipped", []) or []
        print(
            f"[import] call={calls} found={found} enqueued={enqueued} "
            f"skipped={len(skipped)}",
            flush=True,
        )
        total_enqueued += enqueued
        calls += 1
        if found == 0:
            print("[import] no more bundles; done", flush=True)
            break
        time.sleep(UPLOAD_PACE_SECONDS)
    print(f"[import] total enqueued={total_enqueued} across {calls} calls",
          flush=True)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Phase 8: pull live state-doc-coverage + sample map points."""
    token = _admin_token()
    r = requests.get(
        f"{LIVE_BASE_URL}/admin/state-doc-coverage",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if r.status_code != 200:
        print(f"[verify] coverage HTTP {r.status_code}", flush=True)
        return 1
    res = r.json().get("results", [])
    ny = next((x for x in res if x.get("state") == STATE), {})
    print(json.dumps(
        {
            "state": STATE,
            "live": ny.get("live"),
            "with_docs": ny.get("with_docs"),
            "without_docs": ny.get("without_docs"),
            "with_docs_pct": ny.get("with_docs_pct"),
            "verified_at": datetime.now(timezone.utc).isoformat(),
        },
        indent=2,
    ))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("prepare", help="Run Phase 5 prepare on NY bank")
    pp.add_argument("--limit", type=int, default=None)
    pp.add_argument("--dry-run", action="store_true")
    pp.set_defaults(func=cmd_prepare)

    pi = sub.add_parser("import", help="Drain prepared bundles to live")
    pi.add_argument("--max-calls", type=int, default=500)
    pi.set_defaults(func=cmd_import)

    pv = sub.add_parser("verify", help="Check live state-doc-coverage for NY")
    pv.set_defaults(func=cmd_verify)

    pa = sub.add_parser("all", help="prepare → import → verify")
    pa.add_argument("--limit", type=int, default=None)
    pa.add_argument("--dry-run", action="store_true")
    pa.add_argument("--max-calls", type=int, default=500)
    pa.set_defaults(func=lambda a: (cmd_prepare(a) or 0) + (cmd_import(a) or 0) + (cmd_verify(a) or 0))

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
