#!/usr/bin/env python3
"""Backfill NH HOA locations on the live site after the prepare phase.

Strategy (per playbook §6 — public Nominatim is unreliable above ~100
sequential requests, so ZIP centroid via zippopotam.us is the production
primary; Nominatim is a bonus when it happens to land):

  1. Pull all live NH HOAs from /hoas/summary?state=NH&limit=2000.
  2. (Optional) Cross-reference each by name against an SoS leads JSONL
     for city + ZIP — kept here for future SoS-first runs; ignored if
     the file is absent (keyword-Serper run).
  3. For HOAs with no live coordinates:
     a. (Optional) Try Nominatim on `"<HOA name>", <city>, New Hampshire`.
        Stops trying after `--nominatim-cap` 429s in a row.
     b. Reject any hit outside the NH bounding box.
     c. Fall back to ZIP centroid via api.zippopotam.us/us/{zip}.
     d. Final fallback: NH_CITY_CENTROIDS table.
  4. POST records to /admin/backfill-locations in batches.
  5. Demote any out-of-state coordinate to city_only.
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

NH_BBOX = {"min_lat": 42.69, "max_lat": 45.32, "min_lon": -72.58, "max_lon": -70.60}

# NH municipality centroids (lat, lon) — best-effort. Used as a final
# fallback when Nominatim and the ZIP lookup fail. Keys are lower-case.
NH_CITY_CENTROIDS: dict[str, tuple[float, float]] = {
    # Hillsborough
    "manchester": (42.9956, -71.4548), "nashua": (42.7654, -71.4676),
    "merrimack": (42.8651, -71.4934), "bedford": (42.9462, -71.5236),
    "hudson": (42.7648, -71.4391), "goffstown": (43.0223, -71.6056),
    "milford": (42.8348, -71.6493), "amherst": (42.8601, -71.6051),
    "hollis": (42.7445, -71.5901), "brookline": (42.7537, -71.6648),
    "litchfield": (42.8461, -71.4720), "pelham": (42.7345, -71.3242),
    "wilton": (42.8434, -71.7370), "lyndeborough": (42.9001, -71.7376),
    "mont vernon": (42.9006, -71.6779), "new boston": (42.9709, -71.6928),
    "weare": (43.0826, -71.7239), "francestown": (42.9851, -71.8095),
    "greenfield": (42.9462, -71.8740), "bennington": (43.0001, -71.9176),
    "antrim": (43.0301, -71.9398), "hancock": (43.0148, -71.9817),
    "peterborough": (42.8779, -71.9509), "sharon": (42.8240, -71.9554),
    "temple": (42.8362, -71.8479), "mason": (42.7434, -71.7800),
    "new ipswich": (42.7515, -71.8617), "greenville": (42.7682, -71.8095),
    # Rockingham
    "salem": (42.7884, -71.2009), "derry": (42.8806, -71.3273),
    "londonderry": (42.8651, -71.3739), "portsmouth": (43.0718, -70.7626),
    "exeter": (42.9814, -70.9478), "hampton": (42.9376, -70.8284),
    "windham": (42.8001, -71.3026), "stratham": (43.0334, -70.9009),
    "newington": (43.0826, -70.8237), "greenland": (43.0376, -70.8298),
    "rye": (43.0084, -70.7706), "north hampton": (42.9700, -70.8316),
    "atkinson": (42.8390, -71.1473), "auburn": (43.0001, -71.3473),
    "brentwood": (42.9759, -71.0628), "candia": (43.0626, -71.2898),
    "chester": (42.9598, -71.2570), "danville": (42.9134, -71.1232),
    "deerfield": (43.1390, -71.2517), "east kingston": (42.9251, -71.0245),
    "epping": (43.0381, -71.0734), "fremont": (42.9900, -71.1334),
    "hampstead": (42.8770, -71.1798), "hampton falls": (42.9148, -70.8593),
    "kensington": (42.9251, -70.9520), "kingston": (42.9376, -71.0573),
    "new castle": (43.0668, -70.7195), "newfields": (43.0376, -70.9512),
    "newmarket": (43.0826, -70.9362), "newton": (42.8751, -71.0359),
    "northwood": (43.2001, -71.1726), "nottingham": (43.1057, -71.1009),
    "plaistow": (42.8362, -71.0951), "raymond": (43.0334, -71.1934),
    "sandown": (42.9209, -71.1862), "seabrook": (42.8945, -70.8717),
    "south hampton": (42.8862, -70.9700),
    # Belknap
    "laconia": (43.5279, -71.4703), "gilford": (43.5468, -71.4131),
    "meredith": (43.6593, -71.5006), "belmont": (43.4451, -71.4787),
    "tilton": (43.4445, -71.5895), "alton": (43.4560, -71.2273),
    "barnstead": (43.3498, -71.2926), "center harbor": (43.7048, -71.4720),
    "new hampton": (43.6017, -71.6448), "sanbornton": (43.5234, -71.5942),
    "gilmanton": (43.4259, -71.4112),
    # Carroll
    "conway": (43.9789, -71.1228), "north conway": (44.0529, -71.1284),
    "wolfeboro": (43.5832, -71.2065), "ossipee": (43.6859, -71.1129),
    "tamworth": (43.8534, -71.2753), "bartlett": (44.0834, -71.2842),
    "jackson": (44.1492, -71.1842), "tuftonboro": (43.6889, -71.2540),
    "madison": (43.9217, -71.1409), "sandwich": (43.7884, -71.4156),
    "albany": (43.9701, -71.2776), "chatham": (44.1390, -71.0220),
    "eaton": (43.9151, -71.0790), "effingham": (43.7628, -71.0093),
    "freedom": (43.8101, -71.0568), "hart's location": (44.1390, -71.3406),
    "harts location": (44.1390, -71.3406), "hales location": (44.0584, -71.1568),
    # Merrimack
    "concord": (43.2081, -71.5376), "bow": (43.1426, -71.5440),
    "hooksett": (43.0873, -71.4515), "pembroke": (43.1426, -71.4576),
    "boscawen": (43.3198, -71.6076), "henniker": (43.1762, -71.8198),
    "hopkinton": (43.1862, -71.6898), "new london": (43.4126, -71.9842),
    "allenstown": (43.1390, -71.4234), "bradford": (43.2540, -71.9551),
    "canterbury": (43.3334, -71.5587), "chichester": (43.2273, -71.4045),
    "danbury": (43.5217, -71.8717), "dunbarton": (43.1126, -71.6173),
    "epsom": (43.2137, -71.3320), "franklin": (43.4445, -71.6479),
    "hill": (43.5384, -71.7029), "loudon": (43.2937, -71.4587),
    "newbury": (43.3201, -71.9701), "northfield": (43.4262, -71.5723),
    "pittsfield": (43.3045, -71.3334), "salisbury": (43.4201, -71.7268),
    "sutton": (43.3262, -71.9351), "warner": (43.2773, -71.8195),
    "webster": (43.3290, -71.7187), "wilmot": (43.4498, -71.9112),
    "andover": (43.4279, -71.8128),
    # Strafford
    "dover": (43.1979, -70.8737), "rochester": (43.3045, -70.9759),
    "somersworth": (43.2618, -70.8762), "durham": (43.1340, -70.9265),
    "barrington": (43.2126, -71.0432), "farmington": (43.3909, -71.0670),
    "lee": (43.1209, -71.0140), "madbury": (43.1862, -70.9329),
    "milton": (43.4262, -71.0173), "new durham": (43.4459, -71.1551),
    "middleton": (43.4517, -71.0853), "rollinsford": (43.2351, -70.8254),
    "strafford": (43.2862, -71.1726),
    # Grafton
    "lebanon": (43.6420, -72.2517), "hanover": (43.7026, -72.2887),
    "plymouth": (43.7570, -71.6884), "littleton": (44.3084, -71.7706),
    "bristol": (43.5917, -71.7373), "enfield": (43.6473, -72.1414),
    "lincoln": (44.0426, -71.6701), "woodstock": (44.0273, -71.6890),
    "bath": (44.1648, -71.9701), "bridgewater": (43.6679, -71.7406),
    "campton": (43.8584, -71.6606), "canaan": (43.6584, -72.0162),
    "dorchester": (43.7634, -71.9356), "easton": (44.1259, -71.7762),
    "franconia": (44.1903, -71.7459), "grafton": (43.5662, -71.9637),
    "groton": (43.7434, -71.8800), "haverhill": (44.0334, -72.0626),
    "hebron": (43.7034, -71.8053), "holderness": (43.7390, -71.5942),
    "landaff": (44.1334, -71.8870), "lisbon": (44.2117, -71.9087),
    "lyman": (44.2362, -71.9870), "lyme": (43.8087, -72.1517),
    "monroe": (44.2779, -72.0273), "orange": (43.6917, -71.9362),
    "orford": (43.9056, -72.1620), "piermont": (43.9760, -72.0815),
    "rumney": (43.8001, -71.8217), "sugar hill": (44.2126, -71.7770),
    "thornton": (43.8901, -71.6573), "warren": (43.9217, -71.8920),
    "waterville valley": (43.9512, -71.5009), "wentworth": (43.8629, -71.9007),
    # Cheshire
    "keene": (42.9341, -72.2782), "swanzey": (42.8762, -72.2770),
    "jaffrey": (42.8131, -72.0254), "walpole": (43.0762, -72.4262),
    "winchester": (42.7726, -72.3856), "rindge": (42.7567, -72.0026),
    "hinsdale": (42.7898, -72.4862), "alstead": (43.1473, -72.3551),
    "chesterfield": (42.8862, -72.4862), "dublin": (42.9009, -72.0676),
    "fitzwilliam": (42.7762, -72.1420), "gilsum": (43.0509, -72.2706),
    "harrisville": (42.9351, -72.0773), "marlborough": (42.9026, -72.2098),
    "marlow": (43.1290, -72.2032), "nelson": (42.9870, -72.1320),
    "richmond": (42.7548, -72.2570), "roxbury": (42.9598, -72.1820),
    "stoddard": (43.0834, -72.1109), "sullivan": (43.0334, -72.2095),
    "surry": (43.0240, -72.3251), "troy": (42.8243, -72.1801),
    "westmoreland": (42.9762, -72.4334),
    # Sullivan
    "claremont": (43.3742, -72.3464), "newport": (43.3651, -72.1734),
    "sunapee": (43.3909, -72.0876), "charlestown": (43.2390, -72.4262),
    "grantham": (43.4965, -72.1470), "cornish": (43.4779, -72.3434),
    "croydon": (43.4665, -72.1843), "goshen": (43.3068, -72.1370),
    "langdon": (43.1762, -72.4351), "lempster": (43.2570, -72.1862),
    "plainfield": (43.5326, -72.3206), "springfield": (43.5009, -72.0273),
    "unity": (43.3162, -72.2945), "washington": (43.1773, -72.0793),
    # Coos
    "berlin": (44.4687, -71.1850), "gorham": (44.3865, -71.1734),
    "lancaster": (44.4845, -71.5670), "whitefield": (44.3712, -71.6098),
    "colebrook": (44.8945, -71.4951), "jefferson": (44.4173, -71.4854),
    "carroll": (44.2667, -71.5234),  # CDP within Coos County
    "pittsburg": (45.0526, -71.3690), "errol": (44.7798, -71.1409),
    "stratford": (44.6579, -71.5759), "northumberland": (44.5651, -71.5670),
    "stewartstown": (44.9162, -71.4598), "stark": (44.5806, -71.4006),
    "randolph": (44.3692, -71.2887), "milan": (44.5859, -71.1820),
    "dummer": (44.6398, -71.2401), "dixville": (44.8709, -71.2987),
    "columbia": (44.8351, -71.4854), "clarksville": (45.0095, -71.3690),
    "cambridge": (44.7351, -71.2087),
}


def in_nh(lat: float, lon: float) -> bool:
    return (NH_BBOX["min_lat"] <= lat <= NH_BBOX["max_lat"]
            and NH_BBOX["min_lon"] <= lon <= NH_BBOX["max_lon"])


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
        "inc", "incorporated", "llc", "new", "hampshire", "nh", "homes",
        "home", "estate", "estates", "village", "villas", "villa",
    } and len(t) > 2}
    best = None
    best_score = -1
    for hit in hits:
        try:
            lat = float(hit["lat"]); lon = float(hit["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not in_nh(lat, lon):
            continue
        addr = hit.get("address") or {}
        if (addr.get("state") or "").lower() not in {"new hampshire", "nh"}:
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
    if not in_nh(lat, lon):
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
    parser.add_argument("--leads", default=str(ROOT / "state_scrapers/nh/leads/nh_sos_leads.jsonl"))
    parser.add_argument("--base", default=os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org"))
    parser.add_argument("--state", default="NH")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-nominatim", action="store_true")
    parser.add_argument("--nominatim-cap", type=int, default=5)
    parser.add_argument("--zip-cache", default=str(ROOT / "state_scrapers/nh/results/zip_centroid_cache.json"))
    parser.add_argument("--output", default=str(ROOT / "state_scrapers/nh/results/location_enrichment.jsonl"))
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
                if not in_nh(lat, lon):
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
                f'"{hoa_name}" {city} New Hampshire',
                f'{hoa_name} {city} NH',
            ]
            for q in queries:
                try:
                    hits = nominatim_search(session, q,
                                            consecutive_429s=consecutive_429s,
                                            cap=args.nominatim_cap)
                except NominatimLockedOut:
                    nominatim_locked = True
                    print(f"[nh-loc] nominatim locked out after {consecutive_429s[0]} 429s; switching to ZIP-only", file=sys.stderr)
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
            elif city and city.lower() in NH_CITY_CENTROIDS:
                lat, lon = NH_CITY_CENTROIDS[city.lower()]
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
