#!/usr/bin/env python3
"""Mapbox-based address backfill for DC stub HOAs.

Uses Mapbox Geocoding API (https://api.mapbox.com/geocoding/v5/mapbox.places/...)
which is faster and higher-quality than OSM Nominatim for US addresses,
and free up to 100k/month under the temporary-geocode tier.

Strategy per HOA:
  1. If the CAMA name parses to '<num> <street>' (e.g., "218 Vista Condo"
     -> "218 Vista"), forward-geocode '<parsed>, Washington, DC' with
     types=address,poi.
  2. Otherwise (named buildings like "The Watergate"), forward-geocode
     '<full name>, Washington, DC' with types=poi,address.
  3. If both fail and the HOA already has a polygon centroid, reverse-
     geocode the centroid for a fallback street + ZIP.
  4. Validate result is in DC bbox.
  5. Update via /admin/create-stub-hoas.

Requires MAPBOX_ACCESS_TOKEN in settings.env.

Mapbox free tier: 100k temporary geocodes/month — well above our 3,289
DC condo budget. Permanent storage of results requires the (paid)
permanent geocoding tier; we set permanent=false (the default for the
v5 endpoint) so we stay free. The results are stored in our own DB
under the name registry-stub source — Mapbox's terms allow that
provided it isn't redistributed back to the user as a Mapbox-branded
geocode response.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DC_BBOX_STR = "-77.12,38.79,-76.91,39.00"  # min_lon, min_lat, max_lon, max_lat
DC_BBOX = {"min_lat": 38.79, "max_lat": 39.00, "min_lon": -77.12, "max_lon": -76.91}
MAPBOX_FORWARD = "https://api.mapbox.com/geocoding/v5/mapbox.places/{q}.json"
MAPBOX_REVERSE = "https://api.mapbox.com/geocoding/v5/mapbox.places/{lon},{lat}.json"

NAME_STREET_RE = re.compile(
    r"^[#]?\s*(\d+(?:[-\s]\d+)?)\s+"
    r"([A-Za-z][A-Za-z'.&\- ]+?)"
    r"\s+(Condo|Condominium|Condominiums|Condos|Coop|Co-?op|Cooperative|Apartments?)\b",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def live_admin_token() -> str | None:
    # Explicit override wins; otherwise pull from settings.env.
    # Render env-vars fallback removed 2026-05-16 (Hetzner cutover).
    return os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")


def parse_street_from_name(name: str) -> str | None:
    m = NAME_STREET_RE.match(name.strip())
    if not m:
        return None
    num = m.group(1).split()[0]
    street = re.sub(r"\s+", " ", m.group(2).strip())
    return f"{num} {street}"


def in_dc_bbox(lat: float, lon: float) -> bool:
    return (DC_BBOX["min_lat"] <= lat <= DC_BBOX["max_lat"]
            and DC_BBOX["min_lon"] <= lon <= DC_BBOX["max_lon"])


def mapbox_forward(query: str, mapbox_token: str, *, types: str = "address,poi", session: requests.Session) -> dict | None:
    try:
        url = MAPBOX_FORWARD.format(q=quote(query))
        r = session.get(url, params={
            "access_token": mapbox_token,
            "country": "us",
            "limit": 1,
            "types": types,
            "bbox": DC_BBOX_STR,
            "autocomplete": "false",
        }, timeout=15)
        if r.status_code != 200:
            return None
        feats = r.json().get("features") or []
        return feats[0] if feats else None
    except Exception:
        return None


def mapbox_reverse(lon: float, lat: float, mapbox_token: str, session: requests.Session) -> dict | None:
    try:
        url = MAPBOX_REVERSE.format(lon=lon, lat=lat)
        r = session.get(url, params={
            "access_token": mapbox_token, "limit": 1, "types": "address",
        }, timeout=15)
        if r.status_code != 200:
            return None
        feats = r.json().get("features") or []
        return feats[0] if feats else None
    except Exception:
        return None


def extract_address_from_feature(feat: dict) -> dict:
    """Pull street + city + postal from a Mapbox feature."""
    place_name = feat.get("place_name") or ""
    addr_num = feat.get("address")  # street number
    text = feat.get("text") or ""   # street name
    street = None
    if addr_num and text:
        street = f"{addr_num} {text}"
    elif text:
        street = text
    city = None
    postal = None
    for ctx in feat.get("context") or []:
        cid = ctx.get("id", "")
        if cid.startswith("postcode."):
            postal = ctx.get("text")
        elif cid.startswith("place."):
            city = ctx.get("text")
    coords = feat.get("center") or [None, None]
    return {
        "street": street,
        "city": city or "Washington",
        "state": "DC",
        "postal_code": postal,
        "longitude": coords[0],
        "latitude": coords[1],
        "place_name": place_name,
    }


def fetch_dc_hoas(base_url: str) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        r = requests.get(f"{base_url}/hoas/summary", params={
            "state": "DC", "limit": 500, "offset": offset,
        }, timeout=60)
        if r.status_code != 200:
            break
        body = r.json()
        results = body.get("results") or []
        if not results:
            break
        out.extend(results)
        if len(results) < 500:
            break
        offset += 500
        if offset > 10000:
            break
    return out


def post_update(records: list[dict], base_url: str, token: str) -> dict:
    try:
        r = requests.post(
            f"{base_url}/admin/create-stub-hoas",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"records": records},
            timeout=300,
        )
        return {"status": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:500]}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org"))
    parser.add_argument("--ledger", default=str(ROOT / f"state_scrapers/dc/results/dc_stub_addresses_mapbox_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delay", type=float, default=0.05, help="Per-request delay (Mapbox allows ~600 req/min, default is conservative)")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--only-no-street", action="store_true",
                        help="Skip HOAs whose existing /hoas/summary indicates an address-quality location_quality")
    args = parser.parse_args()

    mapbox_token = os.environ.get("MAPBOX_ACCESS_TOKEN")
    if not mapbox_token:
        print("FATAL: MAPBOX_ACCESS_TOKEN missing in settings.env", file=sys.stderr)
        return 2
    token = live_admin_token()
    if not token:
        print("FATAL: no admin token", file=sys.stderr)
        return 2

    ledger_path = Path(args.ledger)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    rows = fetch_dc_hoas(args.base)
    print(f"Fetched {len(rows)} DC HOAs", file=sys.stderr)
    if args.limit:
        rows = rows[: args.limit]

    session = requests.Session()
    session.headers["User-Agent"] = "HOAproxy stub-address backfill (+https://hoaproxy.org)"

    pending: list[dict] = []
    counts = {
        "name_parsed_hit": 0, "name_parsed_miss": 0,
        "name_full_hit": 0, "name_full_miss": 0,
        "reverse_hit": 0, "reverse_miss": 0,
        "no_match": 0, "updated": 0, "errors": 0,
    }

    t0 = time.time()
    last_report = t0
    for i, r in enumerate(rows):
        name = (r.get("hoa") or "").strip()
        if not name:
            continue
        lat = r.get("latitude")
        lon = r.get("longitude")
        decision = "no_match"
        record: dict[str, Any] | None = None

        # Strategy 1: parsed-street forward geocode
        parsed = parse_street_from_name(name)
        if parsed:
            feat = mapbox_forward(f"{parsed}, Washington, DC", mapbox_token, types="address", session=session)
            time.sleep(args.delay)
            if feat:
                comps = extract_address_from_feature(feat)
                clat = comps.get("latitude")
                clon = comps.get("longitude")
                if clat is not None and in_dc_bbox(clat, clon):
                    record = {
                        "name": name,
                        "metadata_type": (r.get("metadata_type") or "condo"),
                        "city": comps.get("city") or "Washington",
                        "state": "DC",
                        "street": comps.get("street") or parsed,
                        "postal_code": comps.get("postal_code"),
                        "latitude": clat,
                        "longitude": clon,
                        "location_quality": "address",
                        "source": "mapbox-geocoding+cama-name-parsed",
                    }
                    counts["name_parsed_hit"] += 1
                    decision = "name_parsed_hit"
                else:
                    counts["name_parsed_miss"] += 1
            else:
                counts["name_parsed_miss"] += 1

        # Strategy 2: full-name forward geocode (POI)
        if record is None:
            feat = mapbox_forward(f"{name}, Washington, DC", mapbox_token, types="poi,address", session=session)
            time.sleep(args.delay)
            if feat:
                comps = extract_address_from_feature(feat)
                clat = comps.get("latitude")
                clon = comps.get("longitude")
                if clat is not None and in_dc_bbox(clat, clon):
                    record = {
                        "name": name,
                        "metadata_type": (r.get("metadata_type") or "condo"),
                        "city": comps.get("city") or "Washington",
                        "state": "DC",
                        "street": comps.get("street"),
                        "postal_code": comps.get("postal_code"),
                        "latitude": clat,
                        "longitude": clon,
                        "location_quality": "address" if comps.get("street") else "place_centroid",
                        "source": "mapbox-geocoding+full-name",
                    }
                    counts["name_full_hit"] += 1
                    decision = "name_full_hit"
                else:
                    counts["name_full_miss"] += 1
            else:
                counts["name_full_miss"] += 1

        # Strategy 3: reverse-geocode existing polygon centroid
        if record is None and lat is not None and lon is not None:
            feat = mapbox_reverse(lon, lat, mapbox_token, session)
            time.sleep(args.delay)
            if feat:
                comps = extract_address_from_feature(feat)
                # Keep the polygon centroid coords; only attach street + ZIP
                record = {
                    "name": name,
                    "metadata_type": (r.get("metadata_type") or "condo"),
                    "city": comps.get("city") or "Washington",
                    "state": "DC",
                    "street": comps.get("street"),
                    "postal_code": comps.get("postal_code"),
                    "source": "mapbox-geocoding+centroid-reverse",
                }
                counts["reverse_hit"] += 1
                decision = "reverse_hit"
            else:
                counts["reverse_miss"] += 1
                counts["no_match"] += 1
        elif record is None:
            counts["no_match"] += 1

        if record:
            pending.append(record)
            with ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"name": name, "decision": decision, "record": record}, sort_keys=True) + "\n")
        else:
            with ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"name": name, "decision": decision}, sort_keys=True) + "\n")

        if len(pending) >= args.batch_size:
            if args.apply:
                result = post_update(pending, args.base, token)
                with ledger_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"event": "batch", "size": len(pending), "result": result}, sort_keys=True) + "\n")
                if result.get("status") != 200:
                    counts["errors"] += 1
                else:
                    counts["updated"] += result.get("body", {}).get("updated", 0)
            pending = []

        now = time.time()
        if now - last_report >= 30:
            last_report = now
            rate = (i + 1) / max(0.001, now - t0)
            eta_min = (len(rows) - i - 1) / max(0.001, rate) / 60
            print(
                f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                f"{i+1}/{len(rows)} ({rate:.2f}/s ETA {eta_min:.1f}m) "
                f"parsed_hit={counts['name_parsed_hit']} full_hit={counts['name_full_hit']} "
                f"reverse_hit={counts['reverse_hit']} no_match={counts['no_match']} "
                f"updated={counts['updated']}",
                file=sys.stderr, flush=True,
            )

    if pending and args.apply:
        result = post_update(pending, args.base, token)
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "batch_final", "size": len(pending), "result": result}, sort_keys=True) + "\n")
        counts["updated"] += result.get("body", {}).get("updated", 0)

    summary = {
        "started_at": now_iso(),
        "wall_seconds": round(time.time() - t0, 1),
        "counts": counts,
        "applied": args.apply,
        "ledger": str(ledger_path),
    }
    with ledger_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "summary", **summary}, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
