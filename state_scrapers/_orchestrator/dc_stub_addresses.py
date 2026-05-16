#!/usr/bin/env python3
"""Backfill street addresses + ZIPs onto existing DC stub HOAs.

For each live DC HOA without a street address:
  1. Parse `<num> <street>` from the CAMA-style name when possible
     ("1130 Columbia Rd Nw Condo" -> "1130 Columbia Rd NW").
  2. Forward-geocode that string via OSM Nominatim ("<street>,
     Washington, DC, USA") — usually returns a perfect canonical address
     with ZIP. Verify the result is in the DC bbox.
  3. If parsing fails, reverse-geocode the existing polygon centroid via
     Nominatim — picks up at least the city/ZIP for named buildings
     ("The Watergate") even when their tax lot polygon centroid lands on
     a trail or side street.
  4. Update via /admin/create-stub-hoas (idempotent upsert).

Nominatim public instance is rate-limited to 1 req/s; respect that with
a 1.2s inter-request delay.
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
DC_BBOX = {"min_lat": 38.79, "max_lat": 39.00, "min_lon": -77.12, "max_lon": -76.91}
NOMINATIM_FWD = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REV = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def live_admin_token() -> str | None:
    # Explicit override wins; otherwise pull from settings.env.
    # Render env-vars fallback removed 2026-05-16 (Hetzner cutover).
    return os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")


# Match: optional '#', digit-string, then street, then condo-suffix
NAME_STREET_RE = re.compile(
    r"^[#]?\s*(\d+(?:[-\s]\d+)?)\s+"             # number (or range like "1110 - 1112")
    r"([A-Za-z][A-Za-z'.&\- ]+?)"                # street tokens
    r"\s+(Condo|Condominium|Condominiums|Condos|Coop|Co-?op|Cooperative|Apartments?)\b",
    re.IGNORECASE,
)
NAME_INCLUDES_QUAD_RE = re.compile(r"\b(NW|NE|SW|SE|Northwest|Northeast|Southwest|Southeast)\b", re.IGNORECASE)


def parse_street_from_name(name: str) -> str | None:
    m = NAME_STREET_RE.match(name.strip())
    if not m:
        return None
    num = m.group(1).split()[0]  # take first number from "1110 - 1112"
    street = m.group(2).strip()
    # Normalize spacing + uppercase quadrant
    street = re.sub(r"\s+", " ", street)
    return f"{num} {street}"


def in_dc_bbox(lat: float, lon: float) -> bool:
    return (DC_BBOX["min_lat"] <= lat <= DC_BBOX["max_lat"]
            and DC_BBOX["min_lon"] <= lon <= DC_BBOX["max_lon"])


def nominatim_forward(query: str, session: requests.Session) -> dict | None:
    try:
        r = session.get(NOMINATIM_FWD, params={
            "q": query, "format": "json", "addressdetails": 1, "limit": 1,
            "countrycodes": "us",
        }, timeout=15)
        if r.status_code != 200:
            return None
        results = r.json()
        if not results:
            return None
        return results[0]
    except Exception:
        return None


def nominatim_reverse(lat: float, lon: float, session: requests.Session) -> dict | None:
    try:
        r = session.get(NOMINATIM_REV, params={
            "lat": lat, "lon": lon, "format": "json",
            "zoom": 18, "addressdetails": 1,
        }, timeout=15)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def extract_addr_components(nom_obj: dict) -> dict:
    addr = nom_obj.get("address") or {}
    house_num = addr.get("house_number")
    road = addr.get("road")
    street_parts = [p for p in (house_num, road) if p]
    return {
        "street": " ".join(street_parts) if street_parts else None,
        "city": addr.get("city") or addr.get("town") or addr.get("hamlet") or "Washington",
        "state": "DC",
        "postal_code": addr.get("postcode"),
    }


def fetch_dc_hoas_needing_address(base_url: str) -> list[dict]:
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
    parser.add_argument("--ledger", default=str(ROOT / f"state_scrapers/dc/results/dc_stub_addresses_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl"))
    parser.add_argument("--limit", type=int, default=0, help="0 = all")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delay", type=float, default=1.2, help="Nominatim inter-request delay (seconds)")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--skip-with-street", action="store_true", default=True,
                        help="Skip HOAs that already have a street")
    args = parser.parse_args()

    token = live_admin_token()
    if not token:
        print("FATAL: no admin token", file=sys.stderr)
        return 2

    ledger_path = Path(args.ledger)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    rows = fetch_dc_hoas_needing_address(args.base)
    print(f"Fetched {len(rows)} DC HOAs", file=sys.stderr)

    needing = []
    for r in rows:
        # The summary endpoint returns 'street' if location_quality has it set.
        # Prefer to backfill all entries that have polygon-quality coords but
        # no street_num + road combo set in their location.
        # We can't tell from /hoas/summary directly (street isn't returned).
        # Use: HOAs with coords (polygon-mapped) get backfilled; city-only
        # ones get an attempt via parsed name.
        needing.append(r)
    if args.limit:
        needing = needing[: args.limit]
    print(f"Will attempt address backfill on {len(needing)} HOAs", file=sys.stderr)

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    pending: list[dict] = []
    counts = {
        "parsed_name": 0, "name_geocode_hit": 0, "name_geocode_miss": 0,
        "reverse_hit": 0, "no_coords": 0,
        "updated": 0, "errors": 0,
    }

    t0 = time.time()
    last_report = t0
    for i, r in enumerate(needing):
        name = (r.get("hoa") or "").strip()
        if not name:
            continue
        lat = r.get("latitude")
        lon = r.get("longitude")
        record: dict[str, Any] | None = None
        decision = ""

        # 1. Parse street from name
        parsed = parse_street_from_name(name)
        if parsed:
            counts["parsed_name"] += 1
            q = f"{parsed}, Washington, DC, USA"
            nom = nominatim_forward(q, session)
            time.sleep(args.delay)
            if nom:
                try:
                    nlat = float(nom["lat"])
                    nlon = float(nom["lon"])
                except Exception:
                    nlat = nlon = None
                if nlat is not None and in_dc_bbox(nlat, nlon):
                    comps = extract_addr_components(nom)
                    record = {
                        "name": name,
                        "metadata_type": (r.get("metadata_type") or "condo"),
                        "city": comps.get("city") or "Washington",
                        "state": "DC",
                        "street": comps.get("street") or parsed,
                        "postal_code": comps.get("postal_code"),
                        "latitude": nlat,
                        "longitude": nlon,
                        "location_quality": "address",
                        "source": "dc-mar-via-osm-nominatim",
                    }
                    counts["name_geocode_hit"] += 1
                    decision = "name_geocode_hit"
                else:
                    counts["name_geocode_miss"] += 1
            else:
                counts["name_geocode_miss"] += 1

        # 2. Fall back to reverse-geocode of existing polygon centroid (for
        #    HOAs where parsing failed OR forward-geocode didn't find a hit)
        if record is None and lat is not None and lon is not None:
            nom = nominatim_reverse(lat, lon, session)
            time.sleep(args.delay)
            if nom:
                comps = extract_addr_components(nom)
                # Keep existing centroid; only attach street + ZIP from reverse
                record = {
                    "name": name,
                    "metadata_type": (r.get("metadata_type") or "condo"),
                    "city": comps.get("city") or "Washington",
                    "state": "DC",
                    "street": comps.get("street"),
                    "postal_code": comps.get("postal_code"),
                    # Don't overwrite lat/lon — they're polygon centroid which is correct
                    "source": "dc-gis-cama-condo-regime+osm-nominatim-reverse",
                }
                counts["reverse_hit"] += 1
                decision = "reverse_hit"
        elif record is None:
            counts["no_coords"] += 1

        if record:
            pending.append(record)

        if len(pending) >= args.batch_size:
            if args.apply:
                result = post_update(pending, args.base, token)
                with ledger_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"event": "batch", "size": len(pending), "result": result}, sort_keys=True) + "\n")
                if result.get("status") != 200:
                    counts["errors"] += 1
                else:
                    counts["updated"] += result.get("body", {}).get("updated", 0)
            else:
                with ledger_path.open("a", encoding="utf-8") as f:
                    for rec in pending:
                        f.write(json.dumps({"dry_run": rec, "decision": decision}, sort_keys=True) + "\n")
            pending = []

        now = time.time()
        if now - last_report >= 30:
            last_report = now
            rate = (i + 1) / max(0.001, now - t0)
            eta_min = (len(needing) - i - 1) / max(0.001, rate) / 60
            print(
                f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                f"{i+1}/{len(needing)} ({rate:.2f}/s, ETA {eta_min:.1f}m) "
                f"name_hit={counts['name_geocode_hit']} reverse_hit={counts['reverse_hit']} "
                f"miss={counts['name_geocode_miss']} updated={counts['updated']} err={counts['errors']}",
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
