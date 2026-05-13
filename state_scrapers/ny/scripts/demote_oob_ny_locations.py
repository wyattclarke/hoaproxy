#!/usr/bin/env python3
"""Demote NY HOA lat/lon to city_only when coords land outside the NY state bbox.

ZIP-centroid backfill (``backfill_ny_zip_centroids.py``) maps every HOA's
postal_code to its ZIP centroid. But the registered postal_code is often the
*mailing address* of a registered agent or sponsor — not the property. So a
NY-incorporated condo can get a Miami / Lakewood NJ / San Francisco lat/lon
because that's where its sponsor LLC is registered.

This pass identifies any NY HOA whose lat/lon falls outside the NY state bbox
and clears the coordinates, demoting location_quality to ``city_only`` so it
doesn't show on the map.

Usage:
    .venv/bin/python state_scrapers/ny/scripts/demote_oob_ny_locations.py [--apply]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / "settings.env", override=False)

BASE_URL = "https://hoaproxy.org"
NY_BBOX = {"min_lat": 40.49, "max_lat": 45.02, "min_lon": -79.77, "max_lon": -71.78}
BATCH = 25


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")
    if not token:
        raise SystemExit("no admin token")

    print("Fetching NY rows...", file=sys.stderr)
    r = requests.post(
        f"{BASE_URL}/admin/list-corruption-targets",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"sources": [
            "ny-dos-active-corporations",
            "ny-acris-decl-2026-05",
            "gcs_prepared_ingest",
        ]},
        timeout=300,
    )
    r.raise_for_status()
    rows = [
        x for x in r.json().get("rows", [])
        if (x.get("state") or "").upper() == "NY" and x.get("latitude") is not None
    ]
    oob = [
        x for x in rows
        if not (NY_BBOX["min_lat"] <= x["latitude"] <= NY_BBOX["max_lat"]
                and NY_BBOX["min_lon"] <= x["longitude"] <= NY_BBOX["max_lon"])
    ]
    print(f"NY with_lat: {len(rows)} | OOB: {len(oob)}", file=sys.stderr)

    if not args.apply:
        for x in oob[:10]:
            print(f"  DRY: {x['hoa'][:50]:50s} | zip={x.get('postal_code')} "
                  f"lat={x['latitude']:.3f} lon={x['longitude']:.3f}", file=sys.stderr)
        return 0

    records = [
        {"hoa": x["hoa"], "location_quality": "city_only", "clear_coordinates": True}
        for x in oob
    ]
    matched = not_found = bad = 0
    for i in range(0, len(records), BATCH):
        chunk = records[i:i + BATCH]
        rr = requests.post(
            f"{BASE_URL}/admin/backfill-locations",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"records": chunk},
            timeout=120,
        )
        if rr.status_code == 200:
            b = rr.json()
            matched += b.get("matched", 0)
            not_found += b.get("not_found", 0)
            bad += b.get("bad_quality", 0)
            print(
                f"  batch {i // BATCH}: matched={b.get('matched')} "
                f"not_found={b.get('not_found')} bad={b.get('bad_quality')}",
                file=sys.stderr,
            )
        else:
            print(f"  batch {i // BATCH}: HTTP {rr.status_code} {rr.text[:200]}",
                  file=sys.stderr)
        time.sleep(0.5)
    print(f"TOTAL: matched={matched} not_found={not_found} bad={bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
