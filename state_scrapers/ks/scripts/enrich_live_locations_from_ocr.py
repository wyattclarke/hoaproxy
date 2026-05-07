#!/usr/bin/env python3
"""Enrich live Kansas HOA locations from OCR text and OSM/Nominatim.

This is a KS-specific cleanup tool for the live site. It reads live HOA
summaries/documents, pulls already-indexed searchable OCR text, extracts
location clues, then proposes or posts map-quality updates:

- OSM/Nominatim subdivision/neighborhood polygons when an extracted alias plus
  city/county resolves to a credible Kansas polygon.
- Census ZCTA centroids when document ZIP evidence is repeated and no polygon
  is found.

The script is intentionally conservative. It refuses out-of-Kansas candidates,
does not post city-only points, and defaults to dry-run.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import urlretrieve

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from scripts import prepare_bank_for_ingest as prepared  # noqa: E402

BASE_URL = "https://hoaproxy.org"
STATE = "KS"
STATE_NAME = "Kansas"
KS_BBOX = (36.8, 40.2, -102.2, -94.4)  # min_lat, max_lat, min_lon, max_lon
DEFAULT_USER_AGENT = "HOAproxy KS OCR location cleanup/1.0 (admin@hoaproxy.org)"
ZCTA_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2024_Gazetteer/2024_Gaz_zcta_national.zip"
)

STOP_ALIAS_WORDS = {
    "declaration",
    "covenants",
    "covenant",
    "conditions",
    "restrictions",
    "restriction",
    "bylaws",
    "bylaw",
    "articles",
    "article",
    "amendment",
    "resolution",
    "final",
    "plat",
    "map",
    "page",
    "book",
    "county",
    "deeds",
    "recorder",
    "register",
    "state",
    "kansas",
    "missouri",
}

SUFFIX_RE = re.compile(
    r"\b("
    r"homeowners association|homeowners|home owners association|"
    r"homes association|property owners association|owners association|"
    r"community association|association|hoa|inc\.?|homes"
    r")\b",
    re.I,
)

STATE_RE = re.compile(
    r"\b(Kansas|Missouri|Nebraska|Oklahoma|Colorado|Arkansas|Iowa)\b",
    re.I,
)
ZIP_RE = re.compile(r"\b(6\d{4})(?:-\d{4})?\b")
COUNTY_RE = re.compile(r"\b([A-Z][A-Za-z .'-]+?)\s+County,\s+Kansas\b")
CITY_RE_PATTERNS = [
    re.compile(r"\bCity\s+of\s+([A-Z][A-Za-z .'-]+?),\s+Kansas\b"),
    re.compile(r"\b([A-Z][A-Za-z .'-]+?),\s+Kansas\b"),
]
ALIAS_PATTERNS = [
    re.compile(
        r"\b(?:Declaration|Declarations|Covenants|Restrictions|Bylaws|Articles|Final Plat|Plat)"
        r"(?:\s+of|\s+for|\s+affecting)?\s+([A-Z][A-Za-z0-9 &'.,-]{3,80}?)(?:\n|,|\.|;)",
        re.I,
    ),
    re.compile(r"\b([A-Z][A-Za-z0-9 &'.,-]{3,80}?)\s+Addition\b"),
    re.compile(r"\b([A-Z][A-Za-z0-9 &'.,-]{3,80}?)\s+Subdivision\b"),
    re.compile(r"\bknown\s+as\s+([A-Z][A-Za-z0-9 &'.,-]{3,80}?)(?:\n|,|\.|;)", re.I),
]


def _request_json(method: str, url: str, **kwargs: Any) -> Any:
    response = requests.request(method, url, timeout=kwargs.pop("timeout", 30), **kwargs)
    response.raise_for_status()
    return response.json()


def _fetch_render_jwt() -> str:
    if os.environ.get("HOAPROXY_ADMIN_BEARER"):
        return os.environ["HOAPROXY_ADMIN_BEARER"].removeprefix("Bearer ").strip()
    api_key = os.environ.get("RENDER_API_KEY")
    service_id = os.environ.get("RENDER_SERVICE_ID")
    if api_key and service_id:
        rows = _request_json(
            "GET",
            f"https://api.render.com/v1/services/{service_id}/env-vars",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        for item in rows:
            env = item.get("envVar") or item
            if env.get("key") == "JWT_SECRET" and env.get("value"):
                return str(env["value"])
    if os.environ.get("JWT_SECRET"):
        return os.environ["JWT_SECRET"]
    raise RuntimeError("HOAPROXY_ADMIN_BEARER, Render API credentials, or JWT_SECRET is required for --apply")


def _compact_name(name: str) -> str:
    return " ".join(SUFFIX_RE.sub(" ", name or "").split())


def _clean_alias(value: str) -> str | None:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value).strip(" .,:;-'\"")
    value = re.sub(r"^(?:of|for|affecting|the)\s+", "", value, flags=re.I)
    value = re.sub(r"\s+(?:Homeowners Association|Homes Association|HOA|Inc\.?)$", "", value, flags=re.I)
    if len(value) < 4 or len(value) > 70:
        return None
    words = [w.casefold().strip(".,") for w in value.split()]
    if not words or all(w in STOP_ALIAS_WORDS for w in words):
        return None
    if re.search(
        r"\b(page|book|instrument|notary|public|secretary|office|recorded|"
        r"recorder|register|deeds?|clerk)\b",
        value,
        re.I,
    ):
        return None
    return value


def _in_kansas(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    min_lat, max_lat, min_lon, max_lon = KS_BBOX
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def _extract_pre_text(searchable_html: str) -> str:
    parts = re.findall(r"<pre>(.*?)</pre>", searchable_html, flags=re.S | re.I)
    return "\n".join(html.unescape(re.sub(r"<[^>]+>", " ", part)) for part in parts)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _fetch_all_summaries(base_url: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload = _request_json(
            "GET",
            f"{base_url}/hoas/summary",
            params={"state": STATE, "limit": 500, "offset": offset},
        )
        batch = payload.get("results") or []
        rows.extend(batch)
        if len(rows) >= int(payload.get("total") or 0) or not batch:
            return rows
        offset += len(batch)


def _fetch_document_text(base_url: str, hoa: str, limit_docs: int) -> str:
    docs = _request_json("GET", f"{base_url}/hoas/{quote(hoa, safe='')}/documents")
    text_parts: list[str] = []
    for doc in docs[:limit_docs]:
        rel_path = doc.get("relative_path")
        if not rel_path:
            continue
        try:
            response = requests.get(
                f"{base_url}/hoas/{quote(hoa, safe='')}/documents/searchable",
                params={"path": rel_path},
                timeout=30,
            )
            if response.status_code == 404:
                continue
            response.raise_for_status()
            text_parts.append(_extract_pre_text(response.text))
        except Exception:
            continue
    return "\n".join(text_parts)


def _extract_clues(hoa: dict[str, Any], text: str) -> dict[str, Any]:
    aliases = []
    for candidate in [hoa.get("hoa") or "", _compact_name(hoa.get("hoa") or "")]:
        cleaned = _clean_alias(candidate)
        if cleaned:
            aliases.append(cleaned)
    for pattern in ALIAS_PATTERNS:
        for match in pattern.finditer(text):
            cleaned = _clean_alias(match.group(1))
            if cleaned:
                aliases.append(cleaned)

    cities = []
    if hoa.get("city"):
        cities.append(str(hoa["city"]))
    for pattern in CITY_RE_PATTERNS:
        for match in pattern.finditer(text):
            city = _clean_alias(match.group(1))
            if city and city.casefold() not in {"state", "county", "kansas"}:
                cities.append(city)

    counties = []
    for match in COUNTY_RE.finditer(text):
        county = _clean_alias(match.group(1))
        if county:
            counties.append(county)

    zip_counts = Counter(ZIP_RE.findall(text))
    state_counts = Counter(s.title() for s in STATE_RE.findall(text))

    def unique(values: list[str], limit: int) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                out.append(value)
            if len(out) >= limit:
                break
        return out

    return {
        "aliases": unique(aliases, 8),
        "cities": unique(cities, 5),
        "counties": unique(counties, 5),
        "zip_counts": dict(zip_counts.most_common(5)),
        "state_counts": dict(state_counts),
    }


def _valid_city(value: str) -> bool:
    return not re.search(r"\b(county|deeds?|recorder|register|clerk|office|book|page)\b", value, re.I)


def _best_city(hoa: dict[str, Any], clues: dict[str, Any]) -> str | None:
    for city in clues.get("cities") or []:
        if _valid_city(str(city)):
            return str(city)
    return hoa.get("city")


def _nominatim_queries(clues: dict[str, Any]) -> list[str]:
    aliases = clues.get("aliases") or []
    cities = [city for city in (clues.get("cities") or []) if _valid_city(city)]
    counties = clues.get("counties") or []
    queries: list[str] = []
    for alias in aliases[:5]:
        for city in cities[:2]:
            queries.append(f"{alias}, {city}, Kansas")
        for county in counties[:2]:
            queries.append(f"{alias}, {county} County, Kansas")
        queries.append(f"{alias}, Kansas")
    out: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = query.casefold()
        if key not in seen:
            seen.add(key)
            out.append(query)
    return out[:12]


def _is_kansas_result(result: dict[str, Any]) -> bool:
    address = result.get("address") if isinstance(result.get("address"), dict) else {}
    display = str(result.get("display_name") or "").casefold()
    return str(address.get("state") or "").casefold() == "kansas" or ", kansas," in display


def _select_polygon(results: Any, clues: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(results, list):
        return None
    selected = prepared._select_nominatim_geometry([r for r in results if _is_kansas_result(r)])
    if not selected:
        return None
    lat = selected.get("latitude")
    lon = selected.get("longitude")
    if not _in_kansas(lat, lon):
        return None
    display = str(selected.get("geography_display_name") or "").casefold()
    alias_bits = [
        word.casefold()
        for alias in clues.get("aliases", [])[:4]
        for word in alias.split()
        if len(word) > 3
    ]
    if alias_bits and not any(bit in display for bit in alias_bits[:6]):
        return None
    cities = [str(c).casefold() for c in clues.get("cities", [])]
    counties = [str(c).casefold() for c in clues.get("counties", [])]
    if cities and not any(city in display for city in cities[:2]):
        if counties and not any(county in display for county in counties[:2]):
            return None
    return selected


def _ensure_zcta_gazetteer(path: Path) -> Path:
    txt = path
    if txt.exists() and txt.suffix == ".txt":
        return txt
    path.parent.mkdir(parents=True, exist_ok=True)
    zip_path = path.with_suffix(".zip") if path.suffix != ".zip" else path
    if not zip_path.exists():
        urlretrieve(ZCTA_URL, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(path.parent)
    candidates = sorted(path.parent.glob("*Gaz_zcta_national.txt"))
    if not candidates:
        raise RuntimeError(f"ZCTA gazetteer not found after extracting {zip_path}")
    return candidates[-1]


def _load_ks_zctas(path: Path) -> dict[str, tuple[float, float]]:
    txt = _ensure_zcta_gazetteer(path)
    out: dict[str, tuple[float, float]] = {}
    with txt.open(encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for raw in reader:
            row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
            geoid = row.get("GEOID")
            if not geoid:
                continue
            lat = float(row["INTPTLAT"])
            lon = float(row["INTPTLONG"])
            if _in_kansas(lat, lon):
                out[geoid] = (lat, lon)
    return out


def _record_from_polygon(hoa: dict[str, Any], selected: dict[str, Any], query: str, clues: dict[str, Any]) -> dict[str, Any]:
    return {
        "hoa": hoa["hoa"],
        "city": _best_city(hoa, clues),
        "state": STATE,
        "latitude": selected.get("latitude"),
        "longitude": selected.get("longitude"),
        "boundary_geojson": json.dumps(selected.get("boundary_geojson")),
        "source": "nominatim_ocr_clues",
        "location_quality": "polygon",
        "_query": query,
        "_display": selected.get("geography_display_name"),
        "_clues": clues,
    }


def _record_from_zip(hoa: dict[str, Any], clues: dict[str, Any], zctas: dict[str, tuple[float, float]], min_count: int) -> dict[str, Any] | None:
    for zip_code, count in sorted(
        (clues.get("zip_counts") or {}).items(),
        key=lambda item: (-int(item[1]), item[0]),
    ):
        if int(count) < min_count or zip_code not in zctas:
            continue
        lat, lon = zctas[zip_code]
        return {
            "hoa": hoa["hoa"],
            "city": _best_city(hoa, clues),
            "state": STATE,
            "postal_code": zip_code,
            "latitude": lat,
            "longitude": lon,
            "source": "census_zcta_ocr_zip",
            "location_quality": "zip_centroid",
            "_zip_count": count,
            "_clues": clues,
        }
    return None


def build_records(args: argparse.Namespace) -> dict[str, Any]:
    summaries = _fetch_all_summaries(args.base_url)
    map_points = _request_json("GET", f"{args.base_url}/hoas/map-points", params={"state": STATE})
    mapped = {row.get("hoa") for row in map_points}
    zctas = _load_ks_zctas(args.zcta_gazetteer)
    cache: dict[str, Any] = _load_json(args.nominatim_cache, {})

    records: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    checked = 0
    for hoa in summaries:
        if args.limit and checked >= args.limit:
            break
        if hoa.get("hoa") in mapped and not args.include_mapped:
            continue
        if hoa.get("boundary_geojson") and not args.include_mapped:
            continue
        checked += 1
        text = _fetch_document_text(args.base_url, str(hoa["hoa"]), args.max_docs)
        clues = _extract_clues(hoa, text)
        if args.require_kansas_text and clues["state_counts"]:
            ks = int(clues["state_counts"].get("Kansas") or 0)
            other = sum(v for k, v in clues["state_counts"].items() if k != "Kansas")
            if other > ks and not hoa.get("city"):
                audit.append({"hoa": hoa["hoa"], "decision": "skip_state_conflict", "clues": clues})
                continue

        chosen = None
        if not args.skip_nominatim:
            for query in _nominatim_queries(clues):
                key = f"nominatim:v3:{query}"
                if key in cache:
                    results = cache[key]
                else:
                    time.sleep(max(0.0, args.nominatim_delay_s))
                    response = requests.get(
                        "https://nominatim.openstreetmap.org/search",
                        params={
                            "q": query,
                            "format": "jsonv2",
                            "polygon_geojson": 1,
                            "addressdetails": 1,
                            "limit": 5,
                            "countrycodes": "us",
                        },
                        headers={"User-Agent": args.user_agent},
                        timeout=30,
                    )
                    if response.status_code == 429:
                        audit.append({
                            "hoa": hoa["hoa"],
                            "decision": "nominatim_rate_limited",
                            "query": query,
                        })
                        _write_json(args.output, {"records": records, "audit": audit})
                        if args.stop_on_rate_limit:
                            raise RuntimeError("Nominatim rate limited; rerun later with the same cache")
                        time.sleep(max(60.0, args.nominatim_delay_s * 10))
                        continue
                    response.raise_for_status()
                    results = response.json()
                    cache[key] = results
                    _write_json(args.nominatim_cache, cache)
                selected = _select_polygon(results, clues)
                if selected:
                    chosen = _record_from_polygon(hoa, selected, query, clues)
                    break

        if not chosen:
            chosen = _record_from_zip(hoa, clues, zctas, args.min_zip_count)

        if chosen:
            records.append(chosen)
            audit.append({"hoa": hoa["hoa"], "decision": "mapped", "quality": chosen["location_quality"], "record": chosen})
        else:
            audit.append({"hoa": hoa["hoa"], "decision": "unmapped", "clues": clues})

        if checked % 25 == 0:
            _write_json(args.output, {"records": records, "audit": audit})
            print(json.dumps({"checked": checked, "records": len(records)}), flush=True)

    return {
        "records": records,
        "audit": audit,
        "summary_total": len(summaries),
        "already_mapped": len(mapped),
        "checked": checked,
    }


def apply_records(args: argparse.Namespace, records: list[dict[str, Any]]) -> dict[str, Any]:
    jwt = _fetch_render_jwt()
    clean = [{k: v for k, v in record.items() if not k.startswith("_")} for record in records]
    return _request_json(
        "POST",
        f"{args.base_url}/admin/backfill-locations",
        headers={"Authorization": f"Bearer {jwt}"},
        json={"records": clean},
        timeout=120,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--output", type=Path, default=Path("state_scrapers/ks/results/live_location_ocr_enrichment.json"))
    parser.add_argument("--nominatim-cache", type=Path, default=Path("state_scrapers/ks/cache/nominatim_ocr_location_cache.json"))
    parser.add_argument("--zcta-gazetteer", type=Path, default=Path("data/census/2024_Gaz_zcta_national.txt"))
    parser.add_argument("--max-docs", type=int, default=4, help="Searchable documents to inspect per HOA")
    parser.add_argument("--min-zip-count", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-mapped", action="store_true")
    parser.add_argument("--require-kansas-text", dest="require_kansas_text", action="store_true", default=True)
    parser.add_argument("--allow-state-conflict", dest="require_kansas_text", action="store_false")
    parser.add_argument("--nominatim-delay-s", type=float, default=1.1)
    parser.add_argument("--skip-nominatim", action="store_true", help="Use only OCR ZIP clues and Census ZCTA centroids")
    parser.add_argument("--stop-on-rate-limit", dest="stop_on_rate_limit", action="store_true", default=True)
    parser.add_argument("--continue-on-rate-limit", dest="stop_on_rate_limit", action="store_false")
    parser.add_argument("--user-agent", default=os.environ.get("HOAPROXY_NOMINATIM_USER_AGENT", DEFAULT_USER_AGENT))
    parser.add_argument("--apply", action="store_true", help="Post records to /admin/backfill-locations")
    args = parser.parse_args()

    payload = build_records(args)
    _write_json(args.output, payload)
    summary = {
        "output": str(args.output),
        "summary_total": payload["summary_total"],
        "already_mapped": payload["already_mapped"],
        "checked": payload["checked"],
        "records": len(payload["records"]),
        "by_quality": dict(Counter(record["location_quality"] for record in payload["records"])),
    }
    print(json.dumps(summary, sort_keys=True))
    if args.apply and payload["records"]:
        result = apply_records(args, payload["records"])
        print(json.dumps({"applied": result}, sort_keys=True))
    elif not args.apply:
        print("dry-run only; pass --apply to post records", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
