#!/usr/bin/env python3
"""Backfill missing map locations using OCR-extracted addresses + HERE Geocoder.

Designed to run after Phase 9 verification reveals a low map rate. For each
live HOA in state X with no map coordinate (or `location_quality` in
{city_only, unknown}), this script:

  1. Pulls the HOA's indexed OCR text via /hoas/{name}/documents/searchable.
  2. Extracts location candidates from that text:
       a. Street addresses ("100 Main St, Anytown, ST 12345")
       b. City + state + ZIP combinations
       c. Subdivision-name + city anchors (when the manifest has city/county)
  3. Queries HERE Geocoder (https://geocode.search.hereapi.com/v1/geocode)
     with each candidate, picks the best in-state match.
  4. POSTs to /admin/backfill-locations with location_quality=address (street-
     level) or place_centroid (city-only) per the HERE result type.

Cost: HERE free tier is 30k requests/month; this script paces at 4 req/sec
(default) and caches responses locally to survive re-runs. A typical state
run: 200-400 candidates × ~3 HERE queries each = 600-1200 calls.

State-agnostic — pass --state and the script discovers the state's bbox
from `state_scrapers/{state}/scripts/run_state_ingestion.py::STATE_BBOX`,
or accept --bbox-json explicitly.

Usage:
    set -a; source settings.env; set +a
    .venv/bin/python scripts/enrich_locations_from_ocr_here.py \\
        --state OH --apply

Idempotent: skips entries already at location_quality in {polygon, address}
unless --reupgrade is passed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_BASE_URL = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")
HERE_ENDPOINT = "https://geocode.search.hereapi.com/v1/geocode"

# Two-letter state code → full state name. Used for query construction and
# HERE result filtering. (HERE returns full names like "Ohio" in its
# `state` field, so we match either the abbreviation or the full name.)
STATE_FULL = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
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
                headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
            r.raise_for_status()
            for env in r.json():
                e = env.get("envVar", env)
                if e.get("key") == "JWT_SECRET" and e.get("value"):
                    return e["value"]
        except Exception:
            pass
    return os.environ.get("JWT_SECRET")


def load_state_bbox(state: str) -> dict[str, float] | None:
    """Read STATE_BBOX from state_scrapers/{state}/scripts/run_state_ingestion.py."""
    runner_path = ROOT / f"state_scrapers/{state.lower()}/scripts/run_state_ingestion.py"
    if not runner_path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_state_runner", runner_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return getattr(mod, "STATE_BBOX", None)
    except Exception:
        return None


def fetch_unmapped_hoas(state: str, base_url: str, reupgrade: bool) -> list[dict[str, Any]]:
    """Return live HOAs for state that lack a usable map coordinate.

    "Unmapped" includes both:
      a. Entries with lat/lon = null in /hoas/summary
      b. Entries with lat/lon set but location_quality in {city_only, unknown}
         — these have coordinates but are hidden from /hoas/map-points

    Compute (b) by diffing /hoas/summary against /hoas/map-points: anything
    in summary that isn't in map-points has either no coordinates or a
    map-hidden quality, both of which warrant a HERE upgrade attempt.
    """
    r = requests.get(f"{base_url}/hoas/summary", params={"state": state}, timeout=60)
    r.raise_for_status()
    rows = r.json().get("results", [])

    # Pull set of hoa_ids currently shown on the map
    mr = requests.get(f"{base_url}/hoas/map-points", params={"state": state}, timeout=60)
    mapped_ids: set[int] = set()
    if mr.status_code == 200:
        for p in (mr.json() or []):
            hid = p.get("hoa_id")
            if isinstance(hid, int):
                mapped_ids.add(hid)

    unmapped = []
    for row in rows:
        if reupgrade:
            unmapped.append(row); continue
        hid = row.get("hoa_id")
        if hid not in mapped_ids:
            unmapped.append(row)
    return unmapped


def fetch_hoa_ocr_text(name: str, base_url: str, max_chars: int = 8000) -> str:
    """Return concatenated OCR text from the HOA's documents.

    Uses /hoas/{name}/documents/searchable which returns an HTML page with
    chunked OCR text inside <pre> blocks. We strip tags and concatenate.
    """
    try:
        # First fetch the documents list to get relative paths
        rl = requests.get(
            f"{base_url}/hoas/{quote(name, safe='')}/documents", timeout=30,
        )
        if rl.status_code != 200:
            return ""
        docs = rl.json() if isinstance(rl.json(), list) else (rl.json().get("results") or [])
    except Exception:
        return ""

    text_chunks: list[str] = []
    total = 0
    for d in docs[:5]:  # cap at first 5 docs per HOA
        if total >= max_chars:
            break
        rp = d.get("relative_path") or d.get("path") or d.get("filename")
        if not rp:
            continue
        try:
            r = requests.get(
                f"{base_url}/hoas/{quote(name, safe='')}/documents/searchable",
                params={"path": rp},
                timeout=60,
            )
            if r.status_code != 200:
                continue
            html_body = r.text
            # Strip HTML tags: keep <pre> contents and discard the rest.
            for m in re.finditer(r"<pre[^>]*>([\s\S]*?)</pre>", html_body, re.IGNORECASE):
                inner = re.sub(r"<[^>]+>", " ", m.group(1))
                inner = unescape(inner)
                inner = re.sub(r"\s+", " ", inner).strip()
                if inner:
                    text_chunks.append(inner)
                    total += len(inner)
                    if total >= max_chars:
                        break
        except Exception:
            continue
    return " ".join(text_chunks)[:max_chars]


# Regex patterns for address extraction.
STREET_TYPES = (
    r"Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Lane|Ln|Way|Place|Pl|"
    r"Boulevard|Blvd|Court|Ct|Circle|Cir|Trail|Trl|Pike|Pkwy|Parkway|"
    r"Highway|Hwy|Terrace|Ter|Square|Sq|Loop|Crescent|Cres|Run|Path|Walk"
)
STREET_ADDR_RE = re.compile(
    rf"\b(\d{{1,6}}[A-Z]?\s+(?:[NSEW]\.?\s+)?[A-Z][\w\s.&-]{{1,40}}\b(?:{STREET_TYPES})\b\.?)",
    re.IGNORECASE,
)
CITY_STATE_ZIP_RE = re.compile(
    r"\b([A-Z][\w\s.-]{{1,40}}?),\s*([A-Z]{{2}})\s+(\d{{5}}(?:-\d{{4}})?)\b"
)
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
COUNTY_RE = re.compile(
    r"\b([A-Z][a-zA-Z']{2,30}(?:\s+[A-Z][a-zA-Z']{2,30})?)\s+County\b"
)


def extract_address_candidates(name: str, ocr_text: str, state: str,
                               manifest_city: str | None,
                               manifest_county: str | None) -> list[str]:
    """Build a ranked list of HERE-friendly query strings from OCR clues."""
    candidates: list[str] = []
    seen = set()
    state_full = STATE_FULL.get(state, state)

    # Pattern 1: full street + city + state + zip in OCR
    for m in CITY_STATE_ZIP_RE.finditer(ocr_text):
        city, st, zipc = m.groups()
        if st.upper() != state:
            continue
        # Look backward for a street address within 80 chars
        start = max(0, m.start() - 80)
        chunk = ocr_text[start:m.start()]
        sam = list(STREET_ADDR_RE.finditer(chunk))
        if sam:
            street = sam[-1].group(1).strip()
            q = f"{street}, {city.strip()}, {st} {zipc}"
        else:
            q = f"{city.strip()}, {st} {zipc}"
        if q not in seen:
            seen.add(q); candidates.append(q)

    # Pattern 2: HOA name + manifest city/county + state (subdivision search)
    if manifest_city:
        q = f"{name}, {manifest_city}, {state_full}"
        if q not in seen:
            seen.add(q); candidates.append(q)
    if manifest_county and manifest_city is None:
        q = f"{name}, {manifest_county} County, {state_full}"
        if q not in seen:
            seen.add(q); candidates.append(q)

    # Pattern 3: any street address in OCR + the HOA's manifest city
    if manifest_city and len(candidates) < 5:
        for m in list(STREET_ADDR_RE.finditer(ocr_text))[:2]:
            q = f"{m.group(1).strip()}, {manifest_city}, {state_full}"
            if q not in seen:
                seen.add(q); candidates.append(q)

    # Pattern 4: most-frequent ZIP + state, as a coarse fallback
    zips = ZIP_RE.findall(ocr_text)
    if zips:
        most_common = max(set(zips), key=zips.count)
        if zips.count(most_common) >= 2:
            q = f"{most_common}, {state_full}"
            if q not in seen:
                seen.add(q); candidates.append(q)

    return candidates[:6]


def here_geocode(query: str, api_key: str, state: str,
                 cache: dict, timeout: float = 15.0) -> dict | None:
    """Hit HERE Geocoder; return the best in-state match or None."""
    if query in cache:
        return cache[query]
    bbox_filter = "countryCode:USA"
    state_full = STATE_FULL.get(state, state)
    params = {
        "q": query,
        "apikey": api_key,
        "in": bbox_filter,
        "limit": 5,
    }
    url = HERE_ENDPOINT + "?" + urlencode(params)
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            cache[query] = None
            return None
        items = r.json().get("items") or []
    except Exception:
        cache[query] = None
        return None

    # Pick the best in-state US match. HERE returns address.state as the full
    # state name, but sometimes uses the 2-letter code.
    best = None
    for item in items:
        addr = item.get("address") or {}
        country = addr.get("countryCode")
        if country and country.upper() not in ("USA", "US"):
            continue
        ist = (addr.get("stateCode") or addr.get("state") or "").strip()
        if ist.upper() == state.upper() or ist == state_full:
            best = item
            break
    if best is None and items:
        # Fallback: first item if state filter fails (HERE can be loose
        # about state for ZIP-only queries)
        addr = (items[0].get("address") or {})
        ist = (addr.get("stateCode") or addr.get("state") or "").strip()
        if ist.upper() == state.upper() or ist == state_full:
            best = items[0]

    cache[query] = best
    return best


def best_record(item: dict, query: str) -> dict | None:
    """Convert a HERE item into a /admin/backfill-locations record.

    Map HERE resultType → location_quality per the playbook's enum. Skip
    types that would produce stacked-pin spam:

      houseNumber / intersection / street → "address"  (best)
      place                                → "place_centroid" (POI/subdivision)
      postalCodePoint                      → "zip_centroid"
      locality                             → SKIP (city-centroid; would stack)
      administrativeArea / district        → SKIP (too coarse)
    """
    if not item:
        return None
    pos = item.get("position") or {}
    lat, lon = pos.get("lat"), pos.get("lng")
    if lat is None or lon is None:
        return None
    addr = item.get("address") or {}
    result_type = (item.get("resultType") or "").lower()

    if result_type in ("housenumber", "intersection", "street"):
        quality = "address"
    elif result_type == "place":
        quality = "place_centroid"
    elif result_type == "postalcodepoint":
        quality = "zip_centroid"
    else:
        # locality, administrativeArea, district, region — would create
        # stacked city-centroid pins; the playbook explicitly forbids them.
        return None

    return {
        "latitude": lat,
        "longitude": lon,
        "street": addr.get("street") or addr.get("label", "")[:100],
        "city": addr.get("city"),
        "state": addr.get("stateCode") or addr.get("state"),
        "postal_code": addr.get("postalCode"),
        "location_quality": quality,
        "_query": query,
        "_result_type": result_type,
    }


def in_bbox(lat: float, lon: float, bbox: dict[str, float]) -> bool:
    return (
        bbox["min_lat"] <= lat <= bbox["max_lat"]
        and bbox["min_lon"] <= lon <= bbox["max_lon"]
    )


def post_backfill(records: list[dict], base_url: str, token: str,
                  apply: bool) -> dict:
    payload = {"records": [
        {k: v for k, v in r.items() if not k.startswith("_")} for r in records
    ]}
    if not apply:
        return {"would_apply": len(records), "samples": records[:3]}
    r = requests.post(
        f"{base_url}/admin/backfill-locations",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload, timeout=180,
    )
    return {"status": r.status_code, "body": r.json() if r.status_code == 200 else r.text[:500]}


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--bbox-json", help="Override STATE_BBOX (otherwise read from runner)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--apply", action="store_true",
                        help="POST to /admin/backfill-locations (default: dry-run)")
    parser.add_argument("--reupgrade", action="store_true",
                        help="Include already-mapped HOAs (will re-geocode)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap unmapped HOAs processed (0=unlimited)")
    parser.add_argument("--rate-per-sec", type=float, default=4.0)
    parser.add_argument("--cache", default="data/here_geocode_cache.json")
    args = parser.parse_args()

    state = args.state.upper()
    bbox = json.loads(args.bbox_json) if args.bbox_json else load_state_bbox(state)
    if not bbox:
        print(f"FATAL: no STATE_BBOX for {state} (pass --bbox-json)", file=sys.stderr)
        return 2

    api_key = os.environ.get("HERE_API_KEY", "").strip()
    if not api_key:
        print("FATAL: HERE_API_KEY missing in environment", file=sys.stderr)
        return 2

    token = live_admin_token()
    if args.apply and not token:
        print("FATAL: cannot resolve admin token", file=sys.stderr)
        return 2

    cache_path = ROOT / args.cache
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    print(f"Fetching unmapped HOAs for {state} ...")
    hoas = fetch_unmapped_hoas(state, args.base_url, args.reupgrade)
    if args.limit:
        hoas = hoas[: args.limit]
    print(f"  {len(hoas)} candidates to process")

    records: list[dict] = []
    skipped: list[dict] = []
    rate_sleep = 1.0 / args.rate_per_sec if args.rate_per_sec > 0 else 0
    for i, h in enumerate(hoas, 1):
        name = h.get("hoa") or ""
        if not name:
            continue
        manifest_city = (h.get("city") or "").strip() or None
        # /hoas/summary doesn't expose county; we only have city if backfilled.
        manifest_county = None
        ocr = fetch_hoa_ocr_text(name, args.base_url)
        candidates = extract_address_candidates(name, ocr, state, manifest_city, manifest_county)
        if not candidates:
            skipped.append({"hoa": name, "reason": "no_candidates"})
            continue
        chosen = None
        for q in candidates:
            item = here_geocode(q, api_key, state, cache)
            if rate_sleep > 0:
                time.sleep(rate_sleep)
            if not item:
                continue
            rec = best_record(item, q)
            if not rec:
                continue
            if not in_bbox(rec["latitude"], rec["longitude"], bbox):
                continue
            chosen = rec
            chosen["hoa"] = name
            break
        if chosen:
            records.append(chosen)
            if i % 10 == 0:
                cache_path.write_text(json.dumps(cache, indent=2))
                print(f"  [{i}/{len(hoas)}] {len(records)} mapped so far")
        else:
            skipped.append({"hoa": name, "reason": "no_geocode_match",
                            "candidates_tried": len(candidates)})

    cache_path.write_text(json.dumps(cache, indent=2))

    print(f"\nResolved {len(records)} new addresses; skipped {len(skipped)}")
    if not records:
        print("No records to write; exiting.")
        return 0

    if args.apply:
        result = post_backfill(records, args.base_url, token, apply=True)
        print(json.dumps({"backfill_result": result}, indent=2)[:1000])
    else:
        print("(dry-run; pass --apply to write)")
        print(json.dumps({"sample_records": records[:5]}, indent=2)[:1500])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
