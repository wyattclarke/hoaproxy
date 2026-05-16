#!/usr/bin/env python3
"""Use /admin/extract-doc-zips output to backfill WY HOA coordinates.

Pipeline:
  1. POST /admin/extract-doc-zips?state=WY → list of {hoa, top_zips: [{zip, count}, ...]}
  2. For each HOA, take the top ZIP and geocode via zippopotam.us.
  3. Reject any centroid outside WY bbox.
  4. POST batched records to /admin/backfill-locations with location_quality=zip_centroid.

The base enrich_wy_locations.py only had access to live `postal_code` / SoS
leads (neither populated for keyword-Serper-discovered WY HOAs); doc-derived
ZIPs are the production fallback.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / "settings.env", override=False)

ZIPPOPOTAM = "https://api.zippopotam.us/us"
USER_AGENT = "HOAproxy public-document discovery (+https://hoaproxy.org)"
WY_BBOX = {"min_lat": 40.99, "max_lat": 45.01, "min_lon": -111.06, "max_lon": -104.04}


def in_wy(lat: float, lon: float) -> bool:
    return (WY_BBOX["min_lat"] <= lat <= WY_BBOX["max_lat"]
            and WY_BBOX["min_lon"] <= lon <= WY_BBOX["max_lon"])


def _live_admin_token() -> str | None:
    # Explicit override wins; otherwise pull from settings.env.
    # Render env-vars fallback removed 2026-05-16 (Hetzner cutover).
    return os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")


def zip_centroid(session, zipcode, *, cache):
    z = (zipcode or "").strip()
    m = re.match(r"^(\d{5})", z)
    if not m:
        return None
    z = m.group(1)
    if z in cache:
        return cache[z]
    try:
        r = session.get(f"{ZIPPOPOTAM}/{z}", timeout=15)
    except requests.RequestException:
        cache[z] = None; return None
    if r.status_code != 200:
        cache[z] = None; return None
    try:
        data = r.json() or {}
    except Exception:
        cache[z] = None; return None
    places = data.get("places") or []
    if not places:
        cache[z] = None; return None
    try:
        lat = float(places[0]["latitude"]); lon = float(places[0]["longitude"])
    except (KeyError, TypeError, ValueError):
        cache[z] = None; return None
    if not in_wy(lat, lon):
        cache[z] = None; return None
    cache[z] = (lat, lon)
    return (lat, lon)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org"))
    parser.add_argument("--state", default="WY")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--zip-cache", default=str(ROOT / "state_scrapers/wy/results/zip_centroid_cache.json"))
    parser.add_argument("--output", default=str(ROOT / "state_scrapers/wy/results/doc_zip_enrichment.jsonl"))
    args = parser.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    token = _live_admin_token()
    assert token, "no admin token available"

    extracted = requests.post(
        f"{args.base}/admin/extract-doc-zips",
        params={"state": args.state, "limit": 5000},
        headers={"Authorization": f"Bearer {token}"}, timeout=600,
    ).json()
    hoas = extracted.get("hoas") or []
    print(f"extracted ZIPs for {len(hoas)} HOAs", file=sys.stderr)

    zip_cache = {}
    zip_cache_path = Path(args.zip_cache)
    if zip_cache_path.exists():
        try:
            raw = json.loads(zip_cache_path.read_text())
            for k, v in raw.items():
                zip_cache[k] = tuple(v) if v else None
        except Exception:
            pass

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    records = []
    by_quality = {"zip_centroid": 0, "no_zip": 0, "bad_zip": 0}
    for h in hoas:
        name = h.get("hoa")
        zips = h.get("top_zips") or []
        if not zips:
            by_quality["no_zip"] += 1
            continue
        zc = None
        zused = None
        for z in zips:
            zused = z.get("zip")
            zc = zip_centroid(session, zused, cache=zip_cache)
            if zc:
                break
        if not zc:
            by_quality["bad_zip"] += 1
            continue
        records.append({
            "hoa": name, "state": args.state,
            "postal_code": zused,
            "latitude": zc[0], "longitude": zc[1],
            "location_quality": "zip_centroid",
        })
        by_quality["zip_centroid"] += 1

    out_path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in records))
    zip_cache_path.write_text(json.dumps({k: list(v) if v else None for k, v in zip_cache.items()}, indent=2, sort_keys=True))

    summary = {
        "extracted_hoas": len(hoas),
        "records_to_backfill": len(records),
        "by_quality": by_quality,
        "zip_cache_size": len(zip_cache),
    }
    print(json.dumps(summary, indent=2))

    if args.apply and records:
        for i in range(0, len(records), 100):
            chunk = records[i:i+100]
            r = requests.post(
                f"{args.base}/admin/backfill-locations",
                json={"records": chunk},
                headers={"Authorization": f"Bearer {token}"}, timeout=300,
            )
            print(f"batch[{i}:{i+len(chunk)}] status={r.status_code} body={r.text[:200]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
