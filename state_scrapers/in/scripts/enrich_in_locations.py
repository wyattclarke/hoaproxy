#!/usr/bin/env python3
"""Backfill IN HOA locations on the live site after the prepare phase.

Strategy mirrors CT (per playbook §6 — public Nominatim is unreliable above
~100 sequential requests, so ZIP centroid via zippopotam.us is the production
primary, Nominatim is a bonus when it happens to land):

  1. Pull all live IN HOAs from /hoas/summary?state=IN&limit=5000.
  2. (Optional) Try Nominatim on `"<HOA name>", <city>, Indiana`.
     Stops trying after `--nominatim-cap` 429s in a row.
  3. Reject any hit outside the IN bounding box.
  4. Fall back to ZIP centroid via api.zippopotam.us/us/{zip}.
  5. Final fallback: IN_CITY_CENTROIDS table from in_geo.
  6. POST records to /admin/backfill-locations in batches.
  7. Demote any out-of-state coordinate to city_only to clean the map.

Key difference from RI/CT runs: leads come from the bank manifests
(probe-driven discovery) rather than a pre-scraped SoS JSONL. We pull
city/postal-code per-HOA from /hoas/summary directly.
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
sys.path.insert(0, str(ROOT))
# `in` is a Python reserved word so we can't `from state_scrapers.in.scripts...`.
# Add the scripts dir directly and import the bare module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(ROOT / "settings.env", override=False)

from in_geo import IN_BBOX, CITY_COUNTY, CITY_CENTROIDS  # noqa: E402

NOMINATIM = "https://nominatim.openstreetmap.org/search"
ZIPPOPOTAM = "https://api.zippopotam.us/us"
USER_AGENT = "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"


def in_indiana(lat: float, lon: float) -> bool:
    return (IN_BBOX["min_lat"] <= lat <= IN_BBOX["max_lat"]
            and IN_BBOX["min_lon"] <= lon <= IN_BBOX["max_lon"])


def normalize_tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


class NominatimLockedOut(Exception):
    pass


def nominatim_search(
    session: requests.Session,
    query: str,
    *,
    consecutive_429s: list[int],
    cap: int,
    retries: int = 2,
) -> list[dict]:
    if consecutive_429s[0] >= cap:
        raise NominatimLockedOut()
    delay = 1.2
    for _ in range(retries):
        time.sleep(delay)
        try:
            r = session.get(
                NOMINATIM,
                params={
                    "q": query, "format": "jsonv2", "polygon_geojson": 1,
                    "addressdetails": 1, "limit": 5, "countrycodes": "us",
                },
                timeout=30,
            )
        except requests.RequestException:
            delay *= 2
            continue
        if r.status_code == 429:
            consecutive_429s[0] += 1
            if consecutive_429s[0] >= cap:
                raise NominatimLockedOut()
            delay = min(delay * 2, 30)
            continue
        if r.status_code >= 400:
            return []
        try:
            consecutive_429s[0] = 0
            return r.json() or []
        except Exception:
            return []
    return []


def best_nominatim_hit(hits: list[dict], name: str, city: str | None) -> dict | None:
    name_tokens = normalize_tokens(name)
    name_specific = {t for t in name_tokens if t not in {
        "the", "and", "of", "association", "associations", "condominium",
        "condominiums", "homeowners", "homeowner", "owners", "owner",
        "inc", "incorporated", "llc", "indiana", "in", "homes",
        "home", "estate", "estates", "village", "villas", "villa",
    } and len(t) > 2}
    best = None
    best_score = -1
    for hit in hits:
        try:
            lat = float(hit["lat"]); lon = float(hit["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not in_indiana(lat, lon):
            continue
        addr = hit.get("address") or {}
        if (addr.get("state") or "").lower() not in {"indiana", "in"}:
            continue
        display = (hit.get("display_name") or "").lower()
        score = 0
        for tok in name_specific:
            if tok in display:
                score += 3
        if city and city.lower() in display:
            score += 2
        if hit.get("type") in {"residential", "neighbourhood", "suburb", "village"}:
            score += 2
        if hit.get("geojson"):
            score += 1
        if score > best_score:
            best_score = score
            best = hit
    if best is None or best_score < 2:
        return None
    return best


def zip_centroid(session: requests.Session, zipcode: str, *, cache: dict[str, tuple[float, float] | None]) -> tuple[float, float] | None:
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
        cache[z] = None
        return None
    if r.status_code != 200:
        cache[z] = None
        return None
    try:
        data = r.json() or {}
    except Exception:
        cache[z] = None
        return None
    places = data.get("places") or []
    if not places:
        cache[z] = None
        return None
    try:
        lat = float(places[0]["latitude"]); lon = float(places[0]["longitude"])
    except (KeyError, TypeError, ValueError):
        cache[z] = None
        return None
    if not in_indiana(lat, lon):
        cache[z] = None
        return None
    cache[z] = (lat, lon)
    return (lat, lon)


def _live_admin_token() -> str | None:
    # Explicit override wins; otherwise pull from settings.env.
    # Render env-vars fallback removed 2026-05-16 (Hetzner cutover).
    return os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-nominatim", action="store_true")
    parser.add_argument("--nominatim-cap", type=int, default=5)
    parser.add_argument("--zip-cache", default=str(ROOT / "state_scrapers/in/results/zip_centroid_cache.json"))
    parser.add_argument("--output", default=str(ROOT / "state_scrapers/in/results/location_enrichment.jsonl"))
    args = parser.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    summary = requests.get(f"{args.base}/hoas/summary", params={"state": "IN", "limit": 5000}, timeout=60).json()
    live_hoas = summary.get("results", [])
    print(f"live IN HOAs: {len(live_hoas)}", file=sys.stderr)

    map_resp = requests.get(f"{args.base}/hoas/map-points", params={"state": "IN"}, timeout=60).json()
    bad_oos: list[str] = []
    if isinstance(map_resp, list):
        for p in map_resp:
            try:
                lat = float(p["latitude"]); lon = float(p["longitude"])
                if not in_indiana(lat, lon):
                    bad_oos.append(p["hoa"])
            except (KeyError, TypeError, ValueError):
                continue
    print(f"out-of-state map points to demote: {len(bad_oos)}", file=sys.stderr)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    zip_cache: dict[str, tuple[float, float] | None] = {}
    zip_cache_path = Path(args.zip_cache)
    if zip_cache_path.exists():
        try:
            raw_cache = json.loads(zip_cache_path.read_text(encoding="utf-8"))
            for k, v in raw_cache.items():
                zip_cache[k] = tuple(v) if v else None
        except Exception:
            zip_cache = {}

    records: list[dict] = []
    by_quality: dict[str, int] = {}
    nom_calls = 0
    n_with_existing = 0
    consecutive_429s = [0]
    nominatim_locked = args.skip_nominatim

    for h in (live_hoas[: args.limit] if args.limit else live_hoas):
        hoa_name = h.get("hoa") or ""
        if h.get("latitude") is not None and hoa_name not in bad_oos:
            n_with_existing += 1
            continue
        city = (h.get("city") or "").strip()
        zip_code = h.get("postal_code") or h.get("zip") or h.get("zip_code")
        record = {"hoa": hoa_name, "state": "IN", "city": city or None, "postal_code": zip_code}

        # 1) Nominatim — opportunistic; bail after lockout cap.
        chosen = None
        if not nominatim_locked and city:
            queries = [
                f'"{hoa_name}" {city} Indiana',
                f'{hoa_name} {city} IN',
            ]
            for q in queries:
                try:
                    hits = nominatim_search(session, q,
                                            consecutive_429s=consecutive_429s,
                                            cap=args.nominatim_cap)
                except NominatimLockedOut:
                    nominatim_locked = True
                    print(f"[in-loc] nominatim locked out after {consecutive_429s[0]} 429s; switching to ZIP-only", file=sys.stderr)
                    break
                nom_calls += 1
                pick = best_nominatim_hit(hits, hoa_name, city)
                if pick:
                    chosen = pick
                    break
        if chosen:
            lat = float(chosen["lat"]); lon = float(chosen["lon"])
            record["latitude"] = lat
            record["longitude"] = lon
            if chosen.get("geojson"):
                record["boundary_geojson"] = chosen["geojson"]
                record["location_quality"] = "polygon"
            else:
                record["location_quality"] = "address"
        else:
            # 2) ZIP centroid via zippopotam.us
            zc = zip_centroid(session, zip_code or "", cache=zip_cache) if zip_code else None
            if zc:
                record["latitude"], record["longitude"] = zc
                record["location_quality"] = "zip_centroid"
            elif city and city.lower() in CITY_CENTROIDS:
                lat, lon = CITY_CENTROIDS[city.lower()]
                record["latitude"] = lat
                record["longitude"] = lon
                # Note: small-state playbook says city_only is hidden from map
                # to avoid stacked pins. Keep that behavior.
                record["location_quality"] = "city_only"
            else:
                record["location_quality"] = "city_only"

        if hoa_name in bad_oos and record.get("location_quality") not in ("polygon", "address"):
            record["location_quality"] = "city_only"

        records.append(record)
        by_quality[record.get("location_quality", "unknown")] = by_quality.get(record.get("location_quality", "unknown"), 0) + 1

    out_path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in records), encoding="utf-8")
    zip_cache_path.parent.mkdir(parents=True, exist_ok=True)
    zip_cache_path.write_text(
        json.dumps({k: list(v) if v else None for k, v in zip_cache.items()}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    summary_out = {
        "live_hoa_count": len(live_hoas),
        "with_existing_location": n_with_existing,
        "out_of_state_demoted": len(bad_oos),
        "nominatim_calls": nom_calls,
        "nominatim_locked": nominatim_locked,
        "zip_cache_entries": len(zip_cache),
        "records": len(records),
        "by_quality": by_quality,
    }
    print(json.dumps(summary_out, indent=2))

    if args.apply and records:
        token = _live_admin_token()
        assert token, "no admin token available"
        for i in range(0, len(records), 100):
            chunk = records[i: i + 100]
            r = requests.post(
                f"{args.base}/admin/backfill-locations",
                json={"records": chunk},
                headers={"Authorization": f"Bearer {token}"},
                timeout=300,
            )
            print(f"batch[{i}:{i+len(chunk)}] status={r.status_code} body={r.text[:200]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
