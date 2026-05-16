#!/usr/bin/env python3
"""HERE Geocoding-based address backfill for DC stub HOAs.

Uses the HERE Geocoding & Search API (v7), free up to 250k transactions/
month — well above our 3,289 DC condo budget.

  Forward: https://geocode.search.hereapi.com/v1/geocode
  Reverse: https://revgeocode.search.hereapi.com/v1/revgeocode

Strategy per HOA:
  1. If the CAMA name parses to '<num> <street>' (e.g., "218 Vista Condo"
     -> "218 Vista"), forward-geocode '<parsed>, Washington, DC' with
     a DC bbox filter.
  2. Otherwise (named building like "The Watergate"), forward-geocode
     '<full name>, Washington, DC'.
  3. If both fail and the HOA has a polygon centroid, reverse-geocode
     the centroid for a fallback street + ZIP.
  4. Validate result is in DC bbox.
  5. Update via /admin/create-stub-hoas.

Requires HERE_API_KEY in settings.env.
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

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
HERE_FORWARD = "https://geocode.search.hereapi.com/v1/geocode"
HERE_REVERSE = "https://revgeocode.search.hereapi.com/v1/revgeocode"
DC_BBOX = {"min_lat": 38.79, "max_lat": 39.00, "min_lon": -77.12, "max_lon": -76.91}
# HERE 'in' parameter format: bbox:minLng,minLat,maxLng,maxLat
DC_BBOX_HERE = "bbox:-77.12,38.79,-76.91,39.00"

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


def here_forward(query: str, here_key: str, session: requests.Session) -> dict | None:
    try:
        r = session.get(HERE_FORWARD, params={
            "q": query,
            "apiKey": here_key,
            "in": "countryCode:USA",
            "limit": 1,
            # Bias toward DC bbox via 'in' alongside countryCode (HERE supports a single 'in' param,
            # but accepts 'at' for proximity bias)
            "at": "38.9072,-77.0369",  # Washington DC center for proximity
        }, timeout=15)
        if r.status_code != 200:
            return None
        items = r.json().get("items") or []
        return items[0] if items else None
    except Exception:
        return None


def here_reverse(lon: float, lat: float, here_key: str, session: requests.Session) -> dict | None:
    try:
        r = session.get(HERE_REVERSE, params={
            "at": f"{lat},{lon}",
            "apiKey": here_key,
            "limit": 1,
        }, timeout=15)
        if r.status_code != 200:
            return None
        items = r.json().get("items") or []
        return items[0] if items else None
    except Exception:
        return None


def extract_address_from_here(item: dict) -> dict:
    addr = item.get("address") or {}
    pos = item.get("position") or {}
    house = addr.get("houseNumber")
    street = addr.get("street")
    full_street = f"{house} {street}" if house and street else (street or None)
    postal = addr.get("postalCode")
    if postal and "-" in postal:
        postal = postal.split("-")[0]  # 5-digit ZIP, drop +4
    return {
        "street": full_street,
        "city": addr.get("city") or "Washington",
        "state": addr.get("stateCode") or "DC",
        "postal_code": postal,
        "latitude": pos.get("lat"),
        "longitude": pos.get("lng"),
        "result_type": item.get("resultType"),
        "scoring": (item.get("scoring") or {}).get("queryScore"),
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
    parser.add_argument("--ledger", default=str(ROOT / f"state_scrapers/dc/results/dc_stub_addresses_here_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--min-score", type=float, default=0.6,
                        help="Reject HERE results with queryScore below this threshold")
    args = parser.parse_args()

    here_key = os.environ.get("HERE_API_KEY")
    if not here_key:
        print("FATAL: HERE_API_KEY missing in settings.env", file=sys.stderr)
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
        "parsed_hit": 0, "parsed_miss": 0,
        "fullname_hit": 0, "fullname_miss": 0,
        "reverse_hit": 0, "reverse_miss": 0,
        "no_match": 0, "low_score": 0,
        "updated": 0, "errors": 0,
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
            item = here_forward(f"{parsed}, Washington, DC, USA", here_key, session)
            time.sleep(args.delay)
            if item:
                comps = extract_address_from_here(item)
                clat = comps.get("latitude")
                clon = comps.get("longitude")
                score = comps.get("scoring") or 0
                if clat is not None and in_dc_bbox(clat, clon) and score >= args.min_score:
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
                        "source": "here-geocoding+cama-name-parsed",
                    }
                    counts["parsed_hit"] += 1
                    decision = "parsed_hit"
                else:
                    counts["parsed_miss"] += 1
                    if score < args.min_score:
                        counts["low_score"] += 1
            else:
                counts["parsed_miss"] += 1

        # Strategy 2: full-name forward geocode (catches named buildings)
        if record is None:
            item = here_forward(f"{name}, Washington, DC, USA", here_key, session)
            time.sleep(args.delay)
            if item:
                comps = extract_address_from_here(item)
                clat = comps.get("latitude")
                clon = comps.get("longitude")
                score = comps.get("scoring") or 0
                if clat is not None and in_dc_bbox(clat, clon) and score >= args.min_score:
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
                        "source": "here-geocoding+full-name",
                    }
                    counts["fullname_hit"] += 1
                    decision = "fullname_hit"
                else:
                    counts["fullname_miss"] += 1
            else:
                counts["fullname_miss"] += 1

        # Strategy 3: reverse-geocode existing polygon centroid
        if record is None and lat is not None and lon is not None:
            item = here_reverse(lon, lat, here_key, session)
            time.sleep(args.delay)
            if item:
                comps = extract_address_from_here(item)
                # Keep existing polygon centroid; only add street + ZIP
                record = {
                    "name": name,
                    "metadata_type": (r.get("metadata_type") or "condo"),
                    "city": comps.get("city") or "Washington",
                    "state": "DC",
                    "street": comps.get("street"),
                    "postal_code": comps.get("postal_code"),
                    "source": "here-geocoding+centroid-reverse",
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
                f"parsed_hit={counts['parsed_hit']} fullname_hit={counts['fullname_hit']} "
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
