#!/usr/bin/env python3
"""Backfill WY HOA locations on the live site after the prepare phase.

Strategy (per playbook §6 — public Nominatim is unreliable above ~100
sequential requests, so ZIP centroid via zippopotam.us is the production
primary; Nominatim is a bonus when it happens to land):

  1. Pull all live WY HOAs from /hoas/summary?state=WY&limit=2000.
  2. For HOAs with no live coordinates:
     a. (Optional) Try Nominatim on `"<HOA name>", <city>, Wyoming`.
        Stops trying after `--nominatim-cap` 429s in a row.
     b. Reject any hit outside the WY bounding box.
     c. Fall back to ZIP centroid via api.zippopotam.us/us/{zip}.
     d. Final fallback: WY_CITY_CENTROIDS table.
  3. POST records to /admin/backfill-locations in batches.
  4. Demote any out-of-state coordinate to city_only.

Adapted from state_scrapers/nh/scripts/enrich_nh_locations.py.
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

NOMINATIM = "https://nominatim.openstreetmap.org/search"
ZIPPOPOTAM = "https://api.zippopotam.us/us"
USER_AGENT = "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"

WY_BBOX = {"min_lat": 40.99, "max_lat": 45.01, "min_lon": -111.06, "max_lon": -104.04}

# WY municipality / CDP centroids (lat, lon). Keys are lower-case. Focused on
# the 9 HOA-bearing counties; sparse rural counties skipped per Phase-2 scope.
WY_CITY_CENTROIDS: dict[str, tuple[float, float]] = {
    # Teton (Jackson Hole)
    "jackson": (43.4799, -110.7624), "wilson": (43.4988, -110.8693),
    "teton village": (43.5868, -110.8285), "moose": (43.6594, -110.7126),
    "kelly": (43.6444, -110.6157), "alta": (43.7740, -111.0379),
    "moran": (43.8417, -110.5152),
    # Laramie (Cheyenne)
    "cheyenne": (41.1399, -104.8202), "pine bluffs": (41.1808, -104.0664),
    "burns": (41.2025, -104.3536), "albin": (41.4189, -104.0930),
    # Natrona (Casper)
    "casper": (42.8666, -106.3131), "mills": (42.8389, -106.3675),
    "bar nunn": (42.9166, -106.3469), "evansville": (42.8722, -106.2622),
    "edgerton": (43.4175, -106.2497), "midwest": (43.4150, -106.2730),
    # Park (Cody / Yellowstone gateway)
    "cody": (44.5263, -109.0565), "powell": (44.7541, -108.7573),
    "meeteetse": (44.1564, -108.8704), "wapiti": (44.4731, -109.4307),
    # Sublette (Pinedale resort condos)
    "pinedale": (42.8666, -109.8597), "big piney": (42.5388, -110.1146),
    "marbleton": (42.5621, -110.1063), "boulder": (42.7236, -109.7194),
    "daniel": (42.8636, -110.0830), "bondurant": (43.2235, -110.4357),
    # Sheridan
    "sheridan": (44.7972, -106.9559), "story": (44.5755, -106.8961),
    "big horn": (44.6727, -107.0040), "ranchester": (44.9088, -107.1659),
    "dayton": (44.8772, -107.2645), "clearmont": (44.6444, -106.3811),
    # Albany (Laramie city)
    "laramie": (41.3114, -105.5905), "centennial": (41.2941, -106.1416),
    "tie siding": (41.0294, -105.4441), "rock river": (41.7361, -105.9694),
    # Lincoln (Star Valley resort)
    "star valley ranch": (42.9738, -110.9504), "alpine": (43.1735, -111.0354),
    "afton": (42.7263, -110.9332), "thayne": (42.9211, -111.0254),
    "etna": (43.0294, -111.0035), "bedford": (42.8946, -110.9457),
    "kemmerer": (41.7919, -110.5371), "diamondville": (41.7783, -110.5402),
    # Fremont (Wind River)
    "lander": (42.8330, -108.7307), "riverton": (43.0250, -108.3801),
    "dubois": (43.5388, -109.6378), "shoshoni": (43.2369, -108.1126),
    "hudson": (42.9069, -108.5848), "pavillion": (43.2436, -108.6962),
    # Secondary counties (likely thin but possible)
    "gillette": (44.2911, -105.5022),       # Campbell
    "rock springs": (41.5875, -109.2029),   # Sweetwater
    "green river": (41.5279, -109.4663),    # Sweetwater
    "evanston": (41.2683, -110.9632),       # Uinta
    "torrington": (42.0639, -104.1850),     # Goshen
    "wheatland": (42.0541, -104.9533),      # Platte
    "douglas": (42.7597, -105.3825),        # Converse
    "thermopolis": (43.6447, -108.2126),    # Hot Springs
    "buffalo": (44.3483, -106.6989),        # Johnson
    "worland": (44.0166, -107.9551),        # Washakie
    "newcastle": (43.8550, -104.2049),      # Weston
    "lusk": (42.7611, -104.4513),           # Niobrara
    "sundance": (44.4060, -104.3753),       # Crook
    "saratoga": (41.4555, -106.8064),       # Carbon
    "rawlins": (41.7911, -107.2387),        # Carbon
    "lyman": (41.3294, -110.2913),          # Uinta
}


def in_wy(lat: float, lon: float) -> bool:
    return (WY_BBOX["min_lat"] <= lat <= WY_BBOX["max_lat"]
            and WY_BBOX["min_lon"] <= lon <= WY_BBOX["max_lon"])


def normalize_tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


class NominatimLockedOut(Exception):
    pass


def nominatim_search(
    session: requests.Session, query: str, *,
    consecutive_429s: list[int], cap: int, retries: int = 2,
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
        "inc", "incorporated", "llc", "wyoming", "wy", "homes",
        "home", "estate", "estates", "village", "villas", "villa",
        "ranch", "ranches", "club",
    } and len(t) > 2}
    best = None
    best_score = -1
    for hit in hits:
        try:
            lat = float(hit["lat"]); lon = float(hit["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not in_wy(lat, lon):
            continue
        addr = hit.get("address") or {}
        if (addr.get("state") or "").lower() not in {"wyoming", "wy"}:
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
    if not in_wy(lat, lon):
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
    parser.add_argument("--leads", default=str(ROOT / "state_scrapers/wy/leads/wy_sos_leads.jsonl"))
    parser.add_argument("--base", default=os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org"))
    parser.add_argument("--state", default="WY")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-nominatim", action="store_true")
    parser.add_argument("--nominatim-cap", type=int, default=5)
    parser.add_argument("--zip-cache", default=str(ROOT / "state_scrapers/wy/results/zip_centroid_cache.json"))
    parser.add_argument("--output", default=str(ROOT / "state_scrapers/wy/results/location_enrichment.jsonl"))
    args = parser.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sos_by_name: dict[str, dict] = {}
    leads_path = Path(args.leads)
    if leads_path.exists():
        for line in leads_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            sos_by_name[rec["name"].lower()] = rec
    print(f"sos leads loaded: {len(sos_by_name)}", file=sys.stderr)

    summary = requests.get(f"{args.base}/hoas/summary", params={"state": args.state, "limit": 2000}, timeout=60).json()
    live_hoas = summary.get("results", []) if isinstance(summary, dict) else []
    print(f"live {args.state} HOAs: {len(live_hoas)}", file=sys.stderr)

    map_resp = requests.get(f"{args.base}/hoas/map-points", params={"state": args.state}, timeout=30).json()
    bad_oos: list[str] = []
    if isinstance(map_resp, list):
        for p in map_resp:
            try:
                lat = float(p["latitude"]); lon = float(p["longitude"])
                if not in_wy(lat, lon):
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
        sos = sos_by_name.get(hoa_name.lower())
        city = (h.get("city") or (sos and sos.get("city")) or "").strip()
        zip_code = (sos.get("postal_code") if sos else None) or h.get("postal_code")
        record = {"hoa": hoa_name, "state": args.state, "city": city or None, "postal_code": zip_code}

        chosen = None
        if not nominatim_locked and city:
            queries = [
                f'"{hoa_name}" {city} Wyoming',
                f'{hoa_name} {city} WY',
            ]
            for q in queries:
                try:
                    hits = nominatim_search(session, q,
                                            consecutive_429s=consecutive_429s,
                                            cap=args.nominatim_cap)
                except NominatimLockedOut:
                    nominatim_locked = True
                    print(f"[wy-loc] nominatim locked out after {consecutive_429s[0]} 429s; switching to ZIP-only", file=sys.stderr)
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
            zc = zip_centroid(session, zip_code or "", cache=zip_cache) if zip_code else None
            if zc:
                record["latitude"], record["longitude"] = zc
                record["location_quality"] = "zip_centroid"
            elif city and city.lower() in WY_CITY_CENTROIDS:
                lat, lon = WY_CITY_CENTROIDS[city.lower()]
                record["latitude"] = lat
                record["longitude"] = lon
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
