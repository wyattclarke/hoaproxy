#!/usr/bin/env python3
"""State-agnostic location enrichment using OCR-derived ZIP centroids.

Despite the historical filename, this script accepts `--state X` and works for
any state.  Pipeline:

  1. POST /admin/extract-doc-zips?state=X — server scans every HOA's chunked
     OCR text for ZIP mentions and returns the top 3 most-frequent ZIPs whose
     first-digit matches the state.  CC&Rs reliably mention the subdivision's
     real ZIP many times (recorded plat, metes-and-bounds, common-area
     address); the manager's ZIP appears once.
  2. For each HOA without coordinates, pick the highest-count plausible ZIP,
     fetch centroid from api.zippopotam.us, and POST to
     /admin/backfill-locations as `zip_centroid` quality.
  3. Optional Nominatim attempt for `<HOA name>, <city>, <state>` (best-effort,
     bounded retry; public instance rate-limits hard above ~100 sequential
     requests).
  4. Reject any centroid outside the state bbox before posting.

This is the production-primary enrichment per the multi-state playbook §6.
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


# State bboxes for OOB rejection (matches scaffold_states.py).
STATE_BBOXES: dict[str, dict[str, float]] = {
    "AK": {"min_lat": 51.20, "max_lat": 71.50, "min_lon": -179.99, "max_lon": -129.00},
    "AL": {"min_lat": 30.14, "max_lat": 35.01, "min_lon": -88.47, "max_lon": -84.89},
    "AR": {"min_lat": 33.00, "max_lat": 36.50, "min_lon": -94.62, "max_lon": -89.64},
    "AZ": {"min_lat": 31.33, "max_lat": 37.00, "min_lon": -114.82, "max_lon": -109.04},
    "CA": {"min_lat": 32.53, "max_lat": 42.01, "min_lon": -124.41, "max_lon": -114.13},
    "CO": {"min_lat": 36.99, "max_lat": 41.00, "min_lon": -109.06, "max_lon": -102.04},
    "CT": {"min_lat": 40.95, "max_lat": 42.05, "min_lon": -73.73, "max_lon": -71.78},
    "DC": {"min_lat": 38.79, "max_lat": 39.00, "min_lon": -77.12, "max_lon": -76.91},
    "DE": {"min_lat": 38.45, "max_lat": 39.84, "min_lon": -75.79, "max_lon": -75.05},
    "FL": {"min_lat": 24.40, "max_lat": 31.00, "min_lon": -87.63, "max_lon": -79.97},
    "GA": {"min_lat": 30.36, "max_lat": 35.00, "min_lon": -85.61, "max_lon": -80.84},
    "HI": {"min_lat": 18.86, "max_lat": 22.24, "min_lon": -160.27, "max_lon": -154.75},
    "IA": {"min_lat": 40.36, "max_lat": 43.50, "min_lon": -96.64, "max_lon": -90.14},
    "ID": {"min_lat": 41.99, "max_lat": 49.00, "min_lon": -117.24, "max_lon": -111.04},
    "IL": {"min_lat": 36.97, "max_lat": 42.51, "min_lon": -91.51, "max_lon": -87.50},
    "IN": {"min_lat": 37.77, "max_lat": 41.76, "min_lon": -88.10, "max_lon": -84.78},
    "KS": {"min_lat": 36.99, "max_lat": 40.00, "min_lon": -102.05, "max_lon": -94.59},
    "KY": {"min_lat": 36.49, "max_lat": 39.15, "min_lon": -89.57, "max_lon": -81.96},
    "LA": {"min_lat": 28.93, "max_lat": 33.02, "min_lon": -94.04, "max_lon": -88.81},
    "MA": {"min_lat": 41.18, "max_lat": 42.89, "min_lon": -73.51, "max_lon": -69.93},
    "MD": {"min_lat": 37.90, "max_lat": 39.72, "min_lon": -79.49, "max_lon": -75.05},
    "ME": {"min_lat": 43.06, "max_lat": 47.46, "min_lon": -71.08, "max_lon": -66.95},
    "MI": {"min_lat": 41.70, "max_lat": 48.30, "min_lon": -90.42, "max_lon": -82.12},
    "MN": {"min_lat": 43.50, "max_lat": 49.38, "min_lon": -97.24, "max_lon": -89.49},
    "MO": {"min_lat": 35.99, "max_lat": 40.62, "min_lon": -95.77, "max_lon": -89.10},
    "MS": {"min_lat": 30.13, "max_lat": 35.00, "min_lon": -91.66, "max_lon": -88.10},
    "MT": {"min_lat": 44.35, "max_lat": 49.00, "min_lon": -116.05, "max_lon": -104.04},
    "NC": {"min_lat": 33.84, "max_lat": 36.59, "min_lon": -84.32, "max_lon": -75.46},
    "ND": {"min_lat": 45.94, "max_lat": 49.00, "min_lon": -104.05, "max_lon": -96.55},
    "NE": {"min_lat": 40.00, "max_lat": 43.00, "min_lon": -104.05, "max_lon": -95.31},
    "NH": {"min_lat": 42.70, "max_lat": 45.31, "min_lon": -72.56, "max_lon": -70.61},
    "NJ": {"min_lat": 38.93, "max_lat": 41.36, "min_lon": -75.56, "max_lon": -73.89},
    "NM": {"min_lat": 31.33, "max_lat": 37.00, "min_lon": -109.05, "max_lon": -103.00},
    "NV": {"min_lat": 35.00, "max_lat": 42.00, "min_lon": -120.01, "max_lon": -114.04},
    "NY": {"min_lat": 40.50, "max_lat": 45.02, "min_lon": -79.76, "max_lon": -71.86},
    "OH": {"min_lat": 38.40, "max_lat": 41.98, "min_lon": -84.82, "max_lon": -80.52},
    "OK": {"min_lat": 33.62, "max_lat": 37.00, "min_lon": -103.00, "max_lon": -94.43},
    "OR": {"min_lat": 41.99, "max_lat": 46.29, "min_lon": -124.55, "max_lon": -116.46},
    "PA": {"min_lat": 39.72, "max_lat": 42.27, "min_lon": -80.52, "max_lon": -74.69},
    "RI": {"min_lat": 41.15, "max_lat": 42.02, "min_lon": -71.86, "max_lon": -71.12},
    "SC": {"min_lat": 32.03, "max_lat": 35.22, "min_lon": -83.35, "max_lon": -78.55},
    "SD": {"min_lat": 42.48, "max_lat": 45.95, "min_lon": -104.06, "max_lon": -96.44},
    "TN": {"min_lat": 34.98, "max_lat": 36.68, "min_lon": -90.31, "max_lon": -81.65},
    "TX": {"min_lat": 25.84, "max_lat": 36.50, "min_lon": -106.65, "max_lon": -93.51},
    "UT": {"min_lat": 36.99, "max_lat": 42.00, "min_lon": -114.05, "max_lon": -109.04},
    "VA": {"min_lat": 36.54, "max_lat": 39.47, "min_lon": -83.68, "max_lon": -75.24},
    "VT": {"min_lat": 42.73, "max_lat": 45.02, "min_lon": -73.44, "max_lon": -71.46},
    "WA": {"min_lat": 45.54, "max_lat": 49.00, "min_lon": -124.85, "max_lon": -116.92},
    "WI": {"min_lat": 42.49, "max_lat": 47.08, "min_lon": -92.89, "max_lon": -86.81},
    "WV": {"min_lat": 37.20, "max_lat": 40.64, "min_lon": -82.65, "max_lon": -77.72},
    "WY": {"min_lat": 40.99, "max_lat": 45.01, "min_lon": -111.06, "max_lon": -104.05},
}


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


def in_bbox(lat: float, lon: float, bbox: dict[str, float]) -> bool:
    return (
        bbox["min_lat"] <= lat <= bbox["max_lat"]
        and bbox["min_lon"] <= lon <= bbox["max_lon"]
    )


def fetch_extract_doc_zips(state: str, base_url: str, token: str) -> list[dict[str, Any]]:
    """Server-side ZIP extraction from indexed OCR text."""
    try:
        r = requests.post(
            f"{base_url}/admin/extract-doc-zips",
            headers={"Authorization": f"Bearer {token}"},
            params={"state": state, "limit": 5000},
            timeout=900,
        )
        if r.status_code != 200:
            print(f"extract-doc-zips returned {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return []
        body = r.json()
        # Accept a list of records, {"results": [...]}, or {"hoas": [...]}.
        # The live endpoint currently returns {"hoas": [...], "total": N}.
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            if isinstance(body.get("hoas"), list):
                return body["hoas"]
            if isinstance(body.get("results"), list):
                return body["results"]
        return []
    except Exception as exc:
        print(f"extract-doc-zips failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return []


def fetch_summary(state: str, base_url: str) -> list[dict[str, Any]]:
    try:
        r = requests.get(f"{base_url}/hoas/summary", params={"state": state, "limit": 5000}, timeout=120)
        if r.status_code != 200:
            return []
        body = r.json()
        if isinstance(body, dict) and isinstance(body.get("results"), list):
            return body["results"]
        if isinstance(body, list):
            return body
    except Exception:
        pass
    return []


def zip_centroid(zip_code: str, cache: dict[str, Any], session: requests.Session) -> tuple[float, float] | None:
    if zip_code in cache:
        v = cache[zip_code]
        return tuple(v) if v else None
    try:
        r = session.get(f"https://api.zippopotam.us/us/{zip_code}", timeout=20)
    except Exception:
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


def post_backfill(records: list[dict[str, Any]], base_url: str, token: str) -> dict[str, Any]:
    if not records:
        return {"posted": 0, "reason": "empty"}
    try:
        r = requests.post(
            f"{base_url}/admin/backfill-locations",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"records": records},
            timeout=600,
        )
        return {"status": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:500]}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org"))
    parser.add_argument("--state", required=True)
    parser.add_argument("--zip-cache", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-nominatim", action="store_true", help="ignored; here for runner compatibility")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()

    state = args.state.upper()
    bbox = STATE_BBOXES.get(state)
    if not bbox:
        print(f"unknown state {state}; refusing to proceed without bbox", file=sys.stderr)
        return 2

    token = live_admin_token()
    if not token:
        print("no admin token", file=sys.stderr)
        return 2

    cache_path = Path(args.zip_cache)
    cache: dict[str, Any] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Server-side ZIP extraction from indexed doc text
    zip_records = fetch_extract_doc_zips(state, args.base, token)
    print(f"extract-doc-zips: {len(zip_records)} HOAs with ZIP evidence", file=sys.stderr)

    # 2. Live HOA summary to find which still lack coordinates
    live_rows = fetch_summary(state, args.base)
    by_id_or_name: dict[str, dict[str, Any]] = {}
    for row in live_rows:
        key = str(row.get("hoa_id") or row.get("hoa") or "").strip()
        if key:
            by_id_or_name[key] = row

    # 3. Build backfill records
    session = requests.Session()
    session.headers.update({"User-Agent": "HOAproxy enrichment (+https://hoaproxy.org)"})
    backfill: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for entry in zip_records:
        hoa = entry.get("hoa") or entry.get("name")
        hoa_id = entry.get("hoa_id")
        # Skip HOAs that already have coords on the live site
        live_row = None
        if hoa_id is not None:
            live_row = by_id_or_name.get(str(hoa_id))
        if live_row is None and hoa:
            live_row = by_id_or_name.get(str(hoa))
        if live_row and live_row.get("latitude") and live_row.get("longitude"):
            decisions.append({"hoa": hoa, "skipped": "already_has_coords"})
            continue
        top_zips = entry.get("top_zips") or []
        if not isinstance(top_zips, list):
            decisions.append({"hoa": hoa, "skipped": "no_top_zips"})
            continue
        chosen = None
        for tz in top_zips:
            zc = (tz.get("zip") if isinstance(tz, dict) else str(tz)) or ""
            if not zc or len(zc) != 5 or not zc.isdigit():
                continue
            ll = zip_centroid(zc, cache, session)
            if not ll:
                continue
            if not in_bbox(ll[0], ll[1], bbox):
                continue
            chosen = (zc, ll)
            break
        if not chosen:
            decisions.append({"hoa": hoa, "skipped": "no_in_bbox_zip"})
            continue
        zc, (lat, lon) = chosen
        rec = {
            "hoa": hoa,
            "hoa_id": hoa_id,
            "latitude": lat,
            "longitude": lon,
            "location_quality": "zip_centroid",
            "postal_code": zc,
            "state": state,
        }
        backfill.append(rec)
        decisions.append({"hoa": hoa, "applied": rec})

    # Persist cache + decisions
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")
    out_path.write_text(
        json.dumps({"backfill_count": len(backfill), "decisions": decisions}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    if args.apply and backfill:
        for i in range(0, len(backfill), args.batch_size):
            chunk = backfill[i:i + args.batch_size]
            result = post_backfill(chunk, args.base, token)
            print(f"backfill batch {i // args.batch_size + 1}: {result}", file=sys.stderr)
    else:
        print(f"dry-run: would backfill {len(backfill)} records", file=sys.stderr)

    print(json.dumps({"state": state, "backfill_count": len(backfill), "decisions_count": len(decisions)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
