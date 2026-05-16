#!/usr/bin/env python3
"""Backfill CT HOA locations on the live site after the prepare phase.

Strategy (per the small-state playbook §6 — public Nominatim is unreliable
above ~100 sequential requests, so ZIP centroid via zippopotam.us is the
production primary, Nominatim is a bonus when it happens to land):

  1. Pull all live CT HOAs from /hoas/summary?state=CT&limit=2000.
  2. Cross-reference each by name against the SoS leads JSONL to get
     city + ZIP + street.
  3. For HOAs with no live coordinates:
     a. (Optional) Try Nominatim on `"<HOA name>", <city>, Connecticut`.
        Stops trying after `--nominatim-cap` 429s in a row, since the
        public instance locks out for 15+ minutes once tripped.
     b. Reject any hit outside the CT bounding box.
     c. Fall back to ZIP centroid via api.zippopotam.us/us/{zip}.
     d. Final fallback: CT_CITY_CENTROIDS table.
  4. POST records to /admin/backfill-locations in batches.
  5. Demote any out-of-state coordinate to city_only to clean the map.
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

CT_BBOX = {"min_lat": 40.95, "max_lat": 42.10, "min_lon": -73.75, "max_lon": -71.78}

# CT town centroids (rough). Used as a final fallback when Nominatim and the
# ZIP lookup both fail. Lower-cased keys to match `city_only` lookups.
CT_CITY_CENTROIDS: dict[str, tuple[float, float]] = {
    # Fairfield County
    "bethel": (41.3712, -73.4140), "bridgeport": (41.1865, -73.1952),
    "brookfield": (41.4790, -73.4040), "danbury": (41.3948, -73.4540),
    "darien": (41.0790, -73.4690), "easton": (41.2520, -73.3000),
    "fairfield": (41.1408, -73.2613), "greenwich": (41.0262, -73.6282),
    "monroe": (41.3320, -73.2070), "new canaan": (41.1471, -73.4945),
    "new fairfield": (41.4670, -73.4860), "newtown": (41.4140, -73.3030),
    "norwalk": (41.1175, -73.4079), "redding": (41.3070, -73.3840),
    "ridgefield": (41.2812, -73.4979), "shelton": (41.3164, -73.0931),
    "sherman": (41.5790, -73.4960), "stamford": (41.0534, -73.5387),
    "stratford": (41.1845, -73.1331), "trumbull": (41.2429, -73.2007),
    "weston": (41.2287, -73.3801), "westport": (41.1414, -73.3579),
    "wilton": (41.1953, -73.4376),
    # Hartford County
    "avon": (41.8076, -72.8329), "berlin": (41.6217, -72.7459),
    "bloomfield": (41.8267, -72.7298), "bristol": (41.6718, -72.9493),
    "burlington": (41.7670, -72.9610), "canton": (41.8265, -72.8918),
    "east granby": (41.9376, -72.7320), "east hartford": (41.7826, -72.6121),
    "east windsor": (41.9090, -72.6090), "enfield": (41.9762, -72.5917),
    "farmington": (41.7195, -72.8323), "glastonbury": (41.7126, -72.6081),
    "granby": (41.9520, -72.7893), "hartford": (41.7658, -72.6734),
    "hartland": (42.0167, -72.9320), "manchester": (41.7759, -72.5215),
    "marlborough": (41.6320, -72.4630), "new britain": (41.6612, -72.7795),
    "newington": (41.6981, -72.7240), "plainville": (41.6745, -72.8590),
    "rocky hill": (41.6651, -72.6390), "simsbury": (41.8762, -72.8087),
    "south windsor": (41.8237, -72.5648), "southington": (41.5965, -72.8779),
    "suffield": (41.9817, -72.6520), "west hartford": (41.7621, -72.7420),
    "wethersfield": (41.7142, -72.6512), "windsor": (41.8526, -72.6437),
    "windsor locks": (41.9293, -72.6234),
    # Litchfield County
    "barkhamsted": (41.9590, -72.9590), "bethlehem": (41.6390, -73.2070),
    "bridgewater": (41.5440, -73.3690), "canaan": (41.9770, -73.2650),
    "colebrook": (42.0040, -73.0980), "cornwall": (41.8420, -73.3340),
    "goshen": (41.8290, -73.2330), "harwinton": (41.7720, -73.0560),
    "kent": (41.7240, -73.4770), "litchfield": (41.7470, -73.1890),
    "morris": (41.6810, -73.1790), "new hartford": (41.8810, -72.9760),
    "new milford": (41.5770, -73.4090), "norfolk": (41.9870, -73.1990),
    "north canaan": (42.0230, -73.3290), "plymouth": (41.6720, -73.0510),
    "roxbury": (41.5450, -73.3050), "salisbury": (41.9850, -73.4220),
    "sharon": (41.8790, -73.4760), "thomaston": (41.6740, -73.0760),
    "torrington": (41.8001, -73.1212), "warren": (41.7340, -73.3490),
    "washington": (41.6360, -73.3110), "watertown": (41.6065, -73.1182),
    "winchester": (41.9220, -73.0790), "woodbury": (41.5440, -73.2090),
    # Middlesex County
    "chester": (41.4040, -72.4520), "clinton": (41.2787, -72.5295),
    "cromwell": (41.5953, -72.6453), "deep river": (41.3810, -72.4380),
    "durham": (41.4810, -72.6810), "east haddam": (41.4570, -72.4640),
    "east hampton": (41.5760, -72.5070), "essex": (41.3520, -72.3920),
    "haddam": (41.4530, -72.5050), "killingworth": (41.3580, -72.5750),
    "middlefield": (41.5120, -72.7200), "middletown": (41.5623, -72.6506),
    "old saybrook": (41.2917, -72.3759), "portland": (41.5731, -72.6403),
    "westbrook": (41.2820, -72.4540),
    # New Haven County
    "ansonia": (41.3437, -73.0784), "beacon falls": (41.4460, -73.0640),
    "bethany": (41.4220, -72.9990), "branford": (41.2790, -72.8151),
    "cheshire": (41.4990, -72.9007), "derby": (41.3204, -73.0894),
    "east haven": (41.2762, -72.8688), "guilford": (41.2890, -72.6817),
    "hamden": (41.3962, -72.8967), "madison": (41.2792, -72.5990),
    "meriden": (41.5382, -72.8071), "middlebury": (41.5260, -73.1290),
    "milford": (41.2226, -73.0566), "naugatuck": (41.4854, -73.0507),
    "new haven": (41.3083, -72.9279), "north branford": (41.3290, -72.7700),
    "north haven": (41.3909, -72.8595), "orange": (41.2786, -73.0254),
    "oxford": (41.4340, -73.1180), "prospect": (41.5020, -72.9790),
    "seymour": (41.3964, -73.0789), "southbury": (41.4811, -73.2123),
    "wallingford": (41.4570, -72.8232), "waterbury": (41.5582, -73.0515),
    "west haven": (41.2706, -72.9469), "wolcott": (41.6010, -72.9870),
    "woodbridge": (41.3540, -72.9930),
    # New London County
    "bozrah": (41.5460, -72.1780), "colchester": (41.5751, -72.3320),
    "east lyme": (41.3740, -72.2250), "franklin": (41.5790, -72.1320),
    "griswold": (41.6080, -71.9890), "groton": (41.3501, -72.0787),
    "lebanon": (41.6390, -72.2300), "ledyard": (41.4260, -72.0140),
    "lisbon": (41.6200, -71.9970), "lyme": (41.3970, -72.3340),
    "montville": (41.4640, -72.1490), "new london": (41.3556, -72.0995),
    "north stonington": (41.4640, -71.8800), "norwich": (41.5243, -72.0759),
    "old lyme": (41.3160, -72.3160), "preston": (41.5280, -71.9850),
    "salem": (41.4910, -72.2670), "sprague": (41.6330, -72.0660),
    "stonington": (41.3357, -71.9072), "voluntown": (41.5800, -71.8740),
    "waterford": (41.3387, -72.1373),
    # Tolland County
    "andover": (41.7370, -72.3700), "bolton": (41.7700, -72.4360),
    "columbia": (41.7080, -72.3000), "coventry": (41.7700, -72.3404),
    "ellington": (41.9050, -72.4710), "hebron": (41.6540, -72.3650),
    "mansfield": (41.7850, -72.2350), "somers": (41.9870, -72.4520),
    "stafford": (41.9650, -72.3020), "tolland": (41.8723, -72.3686),
    "union": (42.0420, -72.1700), "vernon": (41.8190, -72.4690),
    "willington": (41.8800, -72.2780),
    # Windham County
    "ashford": (41.8830, -72.1860), "brooklyn": (41.7870, -71.9460),
    "canterbury": (41.7000, -71.9700), "chaplin": (41.7870, -72.1230),
    "eastford": (41.9020, -72.0820), "hampton": (41.7780, -72.0560),
    "killingly": (41.8390, -71.8730), "plainfield": (41.6770, -71.9180),
    "pomfret": (41.8990, -71.9670), "putnam": (41.9148, -71.9120),
    "scotland": (41.7050, -72.0820), "sterling": (41.6970, -71.8230),
    "thompson": (41.9520, -71.8540), "windham": (41.7110, -72.1640),
    "woodstock": (41.9550, -71.9590),
    # Common villages → use parent town centroid (approximations)
    "mystic": (41.3543, -71.9667), "pawcatuck": (41.3759, -71.8351),
    "rowayton": (41.0626, -73.4453), "cos cob": (41.0331, -73.5965),
    "old greenwich": (41.0262, -73.5670), "riverside": (41.0317, -73.5817),
    "byram": (41.0023, -73.6562), "georgetown": (41.2620, -73.4290),
    "sandy hook": (41.4150, -73.2510), "storrs": (41.8083, -72.2495),
    "storrs mansfield": (41.8083, -72.2495), "willimantic": (41.7104, -72.2081),
    "niantic": (41.3251, -72.1934), "uncasville": (41.4330, -72.1010),
    "jewett city": (41.6048, -71.9786), "moosup": (41.7090, -71.8810),
    "danielson": (41.8068, -71.8851), "dayville": (41.8643, -71.8804),
    "rockville": (41.8650, -72.4540), "stafford springs": (41.9540, -72.3030),
    "collinsville": (41.8201, -72.9192), "unionville": (41.7607, -72.8843),
    "tariffville": (41.9080, -72.7570), "plantsville": (41.5760, -72.9020),
    "forestville": (41.6710, -72.9112), "kensington": (41.6334, -72.7693),
    "moodus": (41.5040, -72.4530), "higganum": (41.4886, -72.5570),
    "centerbrook": (41.3567, -72.4093), "ivoryton": (41.3559, -72.4470),
    "gaylordsville": (41.6470, -73.4920), "lakeville": (41.9568, -73.4407),
    "bantam": (41.7232, -73.2429), "terryville": (41.6781, -73.0265),
    "noank": (41.3267, -71.9926), "gales ferry": (41.4256, -72.0573),
    "mashantucket": (41.4690, -71.9660), "broad brook": (41.9148, -72.5468),
    "amston": (41.6440, -72.3590), "baltic": (41.6195, -72.0773),
    "taftville": (41.5654, -72.0509), "yantic": (41.5440, -72.1015),
    "occum": (41.5879, -72.0506), "northford": (41.3879, -72.7748),
    "oakdale": (41.4503, -72.1589), "quaker hill": (41.3737, -72.1136),
    "south windham": (41.6740, -72.1810), "north windham": (41.7560, -72.1530),
    "oneco": (41.6915, -71.8157),
}


def in_ct(lat: float, lon: float) -> bool:
    return (CT_BBOX["min_lat"] <= lat <= CT_BBOX["max_lat"]
            and CT_BBOX["min_lon"] <= lon <= CT_BBOX["max_lon"])


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
    """Light Nominatim wrapper; raises NominatimLockedOut after `cap`
    consecutive 429s so the caller stops trying for the rest of the run."""
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
        "inc", "incorporated", "llc", "connecticut", "ct", "homes",
        "home", "estate", "estates", "village", "villas", "villa",
    } and len(t) > 2}
    best = None
    best_score = -1
    for hit in hits:
        try:
            lat = float(hit["lat"]); lon = float(hit["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not in_ct(lat, lon):
            continue
        addr = hit.get("address") or {}
        if (addr.get("state") or "").lower() not in {"connecticut", "ct"}:
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
    """Query api.zippopotam.us for a 5-digit ZIP and return (lat, lon).
    Cached and rate-limit-resilient. Returns None on miss."""
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
    if not in_ct(lat, lon):
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
    parser.add_argument("--leads", default=str(ROOT / "state_scrapers/ct/leads/ct_sos_associations.jsonl"))
    parser.add_argument("--base", default=os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-nominatim", action="store_true")
    parser.add_argument("--nominatim-cap", type=int, default=5,
                        help="Stop trying Nominatim after this many consecutive 429s")
    parser.add_argument("--zip-cache", default=str(ROOT / "state_scrapers/ct/results/zip_centroid_cache.json"))
    parser.add_argument("--output", default=str(ROOT / "state_scrapers/ct/results/location_enrichment.jsonl"))
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

    summary = requests.get(f"{args.base}/hoas/summary", params={"state": "CT", "limit": 2000}, timeout=60).json()
    live_hoas = summary.get("results", [])
    print(f"live CT HOAs: {len(live_hoas)}", file=sys.stderr)

    map_resp = requests.get(f"{args.base}/hoas/map-points", params={"state": "CT"}, timeout=30).json()
    bad_oos: list[str] = []
    if isinstance(map_resp, list):
        for p in map_resp:
            try:
                lat = float(p["latitude"]); lon = float(p["longitude"])
                if not in_ct(lat, lon):
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
        record = {"hoa": hoa_name, "state": "CT", "city": city or None, "postal_code": zip_code}

        # 1) Nominatim — opportunistic; bail after the lockout cap.
        chosen = None
        if not nominatim_locked and city:
            queries = [
                f'"{hoa_name}" {city} Connecticut',
                f'{hoa_name} {city} CT',
            ]
            for q in queries:
                try:
                    hits = nominatim_search(session, q,
                                            consecutive_429s=consecutive_429s,
                                            cap=args.nominatim_cap)
                except NominatimLockedOut:
                    nominatim_locked = True
                    print(f"[ct-loc] nominatim locked out after {consecutive_429s[0]} 429s; switching to ZIP-only", file=sys.stderr)
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
            elif city and city.lower() in CT_CITY_CENTROIDS:
                # 3) City centroid → city_only (hidden from map per playbook)
                lat, lon = CT_CITY_CENTROIDS[city.lower()]
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
