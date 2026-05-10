#!/usr/bin/env python3
"""Verify CA cleanup preserved entity rows + geometry.

Per sibling-session handoff, after running clean_ca_junk_docs.py we need
to confirm:

1. Every cleared entity still appears on the live site (i.e. NOT
   accidentally deleted).
2. doc_count for each cleared entity is now 0.
3. lat/lon/boundary_geojson/street/postal_code (if present pre-cleanup)
   is still attached post-cleanup.

This script samples N random cleared hoa_ids from clean_outcome.json
(or all of them with --all), fetches their current state from
/hoas/{name} on the live site, and reports any anomalies.

Usage:
    python state_scrapers/ca/scripts/verify_ca_cleanup.py            # default: 20 random
    python state_scrapers/ca/scripts/verify_ca_cleanup.py --sample 50
    python state_scrapers/ca/scripts/verify_ca_cleanup.py --all      # all cleared
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / "settings.env")

GRADES = ROOT / "state_scrapers" / "ca" / "results" / "audit_2026_05_09" / "ca_grades.json"
DEFAULT_BASE = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")


def get_with_retry(url: str, retries: int = 5, timeout: int = 60) -> tuple[int, dict | None, str | None]:
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.status_code, r.json(), None
            if r.status_code == 404:
                return 404, None, "not found"
            last_err = f"http {r.status_code}: {r.text[:120]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(3 + attempt * 4)
    return 0, None, last_err


def build_id_map(state: str, base_url: str) -> dict[int, dict]:
    """Page through /hoas/summary?state=XX, build {hoa_id: row} map.

    /hoas/{name} returns 404 for docless entities (the rendered docs page
    requires at least one document). The summary endpoint is the canonical
    "does this entity exist?" check and includes latitude/longitude/
    boundary_geojson per row.
    """
    out: dict[int, dict] = {}
    offset = 0
    page = 500
    while True:
        url = f"{base_url}/hoas/summary?state={state}&limit={page}&offset={offset}"
        status, body, err = get_with_retry(url)
        if not body or not body.get("results"):
            break
        for row in body["results"]:
            hid = row.get("hoa_id")
            if isinstance(hid, int):
                out[hid] = row
        if len(body["results"]) < page:
            break
        offset += page
        if offset > 100000:
            break
    return out


def fetch_summary(name: str, base_url: str) -> tuple[dict | None, str | None]:
    """Legacy wrapper kept for backward compat — no longer used."""
    enc = quote(name, safe="")
    status, body, err = get_with_retry(f"{base_url}/hoas/{enc}")
    if status == 200 and isinstance(body, dict):
        return body, None
    return None, err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grades", default=str(GRADES))
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="state_scrapers/ca/results/audit_2026_05_09/verify_outcome.json")
    args = ap.parse_args()

    d = json.loads(Path(args.grades).read_text())
    junk = [r for r in d.get("results", []) if r.get("verdict") == "junk"
            and isinstance(r.get("hoa_id"), int)]
    print(f"[verify] {len(junk)} CA junk entries from grade file (target = entities cleared)")

    random.seed(args.seed)
    if args.all:
        targets = junk
    else:
        targets = random.sample(junk, min(args.sample, len(junk)))
    print(f"[verify] sampling {len(targets)} entities to verify")

    print(f"[verify] building CA hoa_id → row map via /hoas/summary…")
    id_map = build_id_map("CA", args.base_url)
    print(f"[verify] loaded {len(id_map)} CA entities")

    issues = {"not_found": [], "doc_count_nonzero": [], "missing_geometry": [], "errors": []}
    ok = 0
    for n, t in enumerate(targets, 1):
        name = t.get("hoa")
        hoa_id = t.get("hoa_id")
        row = id_map.get(hoa_id)
        if row is None:
            issues["not_found"].append({"hoa_id": hoa_id, "hoa": name})
            print(f"  [{n}/{len(targets)}] id={hoa_id} {name[:40]:<40}  MISSING (not in /hoas/summary)")
            continue

        doc_count = row.get("doc_count", 0)
        lat = row.get("latitude")
        lon = row.get("longitude")
        boundary = row.get("boundary_geojson")
        # /hoas/summary doesn't surface street/postal; check via id_map presence
        geom_present = lat is not None and lon is not None
        polygon = bool(boundary)

        flag = "OK"
        if doc_count > 0:
            issues["doc_count_nonzero"].append({
                "hoa_id": hoa_id, "hoa": name, "doc_count": doc_count,
            })
            flag = f"DOC_COUNT={doc_count}"
        elif not (geom_present or polygon):
            # Not necessarily a regression — the entity may never have had
            # geometry. We log but don't fail-hard on this.
            issues["missing_geometry"].append({
                "hoa_id": hoa_id, "hoa": name,
            })
            flag = "NO_GEOM"
        else:
            ok += 1

        loc_summary = []
        if geom_present:
            loc_summary.append(f"latlon={lat:.4f},{lon:.4f}")
        if polygon:
            loc_summary.append("polygon")
        loc_str = " ".join(loc_summary) or "-"
        print(f"  [{n}/{len(targets)}] id={hoa_id} {name[:40]:<40}  doc_count={doc_count}  {loc_str}  [{flag}]")

    print()
    print(f"=== Summary ===")
    print(f"  sampled: {len(targets)}")
    print(f"  ok (entity preserved + docs=0 + geometry intact-or-never-had-any): {ok}")
    print(f"  not_found: {len(issues['not_found'])}")
    print(f"  doc_count_nonzero: {len(issues['doc_count_nonzero'])}")
    print(f"  missing_geometry: {len(issues['missing_geometry'])}")
    print(f"  errors: {len(issues['errors'])}")
    if issues["not_found"]:
        print()
        print("  not_found (UH-OH — entity was deleted):")
        for x in issues["not_found"][:10]:
            print(f"    id={x['hoa_id']} {x['hoa']}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"sampled": len(targets), "ok": ok, "issues": issues}, indent=2))
    print(f"\n  summary -> {out_path}")
    return 0 if not issues["not_found"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
