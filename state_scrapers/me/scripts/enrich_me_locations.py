#!/usr/bin/env python3
"""Backfill Maine imported HOA locations with conservative ZIP centroids.

This scraper run uses curated public documents with city-level evidence but no
street-level geocoding. Public Nominatim was already rate-limiting during
prepare, so use zippopotam.us ZIP centroids for map-safe approximate points.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


KNOWN_ZIPS: dict[str, tuple[str, str]] = {
    "Beachwood Bay Estates Condominium Association": ("York", "03909"),
    "Breeze Lane Condominium Association": ("Wells", "04090"),
    "Cider Hill Condominium Association": ("North Yarmouth", "04097"),
    "Cumberland Foreside Condominium Association": ("Falmouth", "04105"),
    "Eastman Block Condominium Association": ("Portland", "04101"),
    "Fairway View Village Condominium Association": ("Wells", "04090"),
    "Misty Harbor and Barefoot Beach Resort Condominium Association": ("Wells", "04090"),
    "Munjoy Heights Condominium Association": ("Portland", "04101"),
    "Ocean Mist Villages Condominium Association": ("Wells", "04090"),
    "OneJoy Condominium Owners Association": ("Portland", "04101"),
    "Park-Danforth Condominium Association": ("Portland", "04103"),
    "Pine and Winter Street Condominium Association": ("Portland", "04102"),
    "Regency Woods Condominium Association": ("Kittery", "03904"),
    "Ridgewood Condominium Association": ("Falmouth", "04105"),
    "Sunfield Condominium Association": ("Brunswick", "04011"),
    "Woodbury Shores Cottages Condominium Owners Association": ("Litchfield", "04350"),
    "Yarmouth Bluffs Condominium Association": ("Yarmouth", "04096"),
}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def live_admin_token() -> str | None:
    if os.environ.get("HOAPROXY_ADMIN_BEARER"):
        return os.environ["HOAPROXY_ADMIN_BEARER"]
    api_key = os.environ.get("RENDER_API_KEY")
    service_id = os.environ.get("RENDER_SERVICE_ID")
    if api_key and service_id:
        try:
            r = requests.get(
                f"https://api.render.com/v1/services/{service_id}/env-vars",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            r.raise_for_status()
            for env in r.json():
                e = env.get("envVar", env)
                if e.get("key") == "JWT_SECRET" and e.get("value"):
                    return e["value"]
        except Exception:
            pass
    return os.environ.get("JWT_SECRET")


def zip_centroid(session: requests.Session, zip_code: str, cache: dict[str, Any]) -> tuple[float, float] | None:
    if zip_code in cache:
        value = cache[zip_code]
        return tuple(value) if value else None
    response = session.get(f"https://api.zippopotam.us/us/{zip_code}", timeout=20)
    if response.status_code != 200:
        cache[zip_code] = None
        return None
    places = response.json().get("places") or []
    if not places:
        cache[zip_code] = None
        return None
    lat = float(places[0]["latitude"])
    lon = float(places[0]["longitude"])
    cache[zip_code] = [lat, lon]
    return lat, lon


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org"))
    parser.add_argument("--zip-cache", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--state", default="ME")
    parser.add_argument("--skip-nominatim", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    session = requests.Session()
    cache_path = Path(args.zip_cache)
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    summary = session.get(f"{args.base}/hoas/summary", params={"state": args.state}, timeout=60)
    summary.raise_for_status()
    rows = summary.json().get("results") or []

    records: list[dict[str, Any]] = []
    for row in rows:
        hoa = row.get("hoa")
        if hoa not in KNOWN_ZIPS:
            continue
        city, zip_code = KNOWN_ZIPS[hoa]
        point = zip_centroid(session, zip_code, cache)
        if not point:
            continue
        records.append(
            {
                "hoa": hoa,
                "city": city,
                "state": args.state,
                "postal_code": zip_code,
                "latitude": point[0],
                "longitude": point[1],
                "location_quality": "zip_centroid",
                "source": "me_zip_centroid_backfill",
            }
        )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    write_json(cache_path, cache)

    result: dict[str, Any] = {"records": len(records), "applied": False}
    if args.apply and records:
        token = live_admin_token()
        if not token:
            raise RuntimeError("missing admin token")
        response = session.post(
            f"{args.base}/admin/backfill-locations",
            headers={"Authorization": f"Bearer {token}"},
            json={"records": records},
            timeout=60,
        )
        result["status_code"] = response.status_code
        result["body"] = response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text
        response.raise_for_status()
        result["applied"] = True

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
