#!/usr/bin/env python3
"""Backfill NY HOA lat/lon from postal_code via zippopotam.us.

NY's 12,000+ HOAs were stub-backfilled with city + postal_code but ZERO
latitude/longitude — the original stub backfill skipped geo-enrichment.
This script fills in ZIP-centroid coordinates for every NY HOA with a
postal_code, lifting map coverage from 0% to ~95%.

Pattern from CT retrospective: zippopotam.us is the right post-import
geocoder (Nominatim rate-limits hard at ~100 sequential).

Usage:
    .venv/bin/python state_scrapers/ny/scripts/backfill_ny_zip_centroids.py [--apply]
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
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)

BASE_URL = "https://hoaproxy.org"
ZIP_API = "https://api.zippopotam.us/us/{zip}"
CACHE_PATH = ROOT / "state_scrapers/ny/results/ny_20260510_213944Z_claude/zip_centroid_cache.json"
BATCH = 25
SLEEP_S = 0.3


def fetch_ny_rows(token: str) -> list[dict]:
    r = requests.post(
        f"{BASE_URL}/admin/list-corruption-targets",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"sources": [
            "ny-dos-active-corporations",
            "ny-acris-decl-2026-05",
            "gcs_prepared_ingest",
        ]},
        timeout=120,
    )
    r.raise_for_status()
    return [
        r2 for r2 in r.json().get("rows", [])
        if (r2.get("state") or "").upper() == "NY"
    ]


def load_cache() -> dict[str, list[float] | None]:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def zip_centroid(session: requests.Session, zip_code: str, cache: dict) -> tuple[float, float] | None:
    if zip_code in cache:
        v = cache[zip_code]
        return tuple(v) if v else None
    try:
        r = session.get(ZIP_API.format(zip=zip_code), timeout=15)
    except requests.exceptions.RequestException:
        cache[zip_code] = None
        return None
    if r.status_code != 200:
        cache[zip_code] = None
        return None
    places = r.json().get("places") or []
    if not places:
        cache[zip_code] = None
        return None
    lat = float(places[0]["latitude"])
    lon = float(places[0]["longitude"])
    cache[zip_code] = [lat, lon]
    return lat, lon


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    args = ap.parse_args()

    token = os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")
    if not token:
        raise SystemExit("no admin token")

    print("Fetching NY HOAs...", file=sys.stderr)
    rows = fetch_ny_rows(token)
    print(f"NY total in sources: {len(rows)}", file=sys.stderr)

    # Filter to rows with postal_code AND no lat
    need = [r for r in rows if (r.get("postal_code") or "").strip() and not r.get("latitude")]
    print(f"Need ZIP centroid: {len(need)}", file=sys.stderr)
    if args.limit:
        need = need[:args.limit]

    # Build unique ZIP set
    unique_zips = sorted({(r.get("postal_code") or "").strip()[:5] for r in need if (r.get("postal_code") or "").strip()})
    unique_zips = [z for z in unique_zips if len(z) == 5 and z.isdigit()]
    print(f"Unique ZIPs to geocode: {len(unique_zips)}", file=sys.stderr)

    cache = load_cache()
    print(f"Cache pre-load: {len(cache)} entries", file=sys.stderr)

    # Fetch each ZIP
    session = requests.Session()
    session.headers.update({"User-Agent": "hoaproxy/1.0 (admin@hoaproxy.org)"})
    fetched = 0
    misses = 0
    for i, z in enumerate(unique_zips):
        if z in cache:
            continue
        result = zip_centroid(session, z, cache)
        if result is None:
            misses += 1
        fetched += 1
        if fetched % 100 == 0:
            print(f"  zip fetch {fetched}/{len(unique_zips)-len(cache)+fetched} (misses={misses})", file=sys.stderr)
            save_cache(cache)
        time.sleep(SLEEP_S)
    save_cache(cache)
    print(f"Cache final: {len(cache)} | fetched this run: {fetched} | misses: {misses}", file=sys.stderr)

    # Build backfill records
    records: list[dict] = []
    skipped = 0
    for r in need:
        zip5 = (r.get("postal_code") or "").strip()[:5]
        if not (len(zip5) == 5 and zip5.isdigit()):
            skipped += 1
            continue
        coords = cache.get(zip5)
        if not coords:
            skipped += 1
            continue
        records.append({
            "hoa": r["hoa"],
            "latitude": coords[0],
            "longitude": coords[1],
            "location_quality": "zip_centroid",
        })

    print(f"Backfill records prepared: {len(records)} | skipped: {skipped}", file=sys.stderr)
    if not args.apply:
        for rec in records[:5]:
            print(f"  DRY: {rec}", file=sys.stderr)
        return 0

    # Apply in batches
    matched = not_found = bad_quality = 0
    for i in range(0, len(records), BATCH):
        chunk = records[i:i+BATCH]
        try:
            rr = requests.post(
                f"{BASE_URL}/admin/backfill-locations",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"records": chunk},
                timeout=300,
            )
        except requests.exceptions.RequestException as e:
            print(f"  batch {i//BATCH} request error: {e}", file=sys.stderr)
            time.sleep(2.0)
            continue
        if rr.status_code != 200:
            print(f"  batch {i//BATCH} HTTP {rr.status_code}: {rr.text[:200]}", file=sys.stderr)
            time.sleep(2.0)
            continue
        body = rr.json()
        m = int(body.get("matched", 0))
        nf = int(body.get("not_found", 0))
        bq = int(body.get("bad_quality", 0))
        matched += m
        not_found += nf
        bad_quality += bq
        print(f"  batch {i//BATCH}: matched={m} not_found={nf} bad_quality={bq}", file=sys.stderr)
        time.sleep(0.5)

    print(json.dumps({
        "summary": "ny_zip_centroid_backfill",
        "candidates": len(records),
        "matched": matched,
        "not_found": not_found,
        "bad_quality": bad_quality,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
