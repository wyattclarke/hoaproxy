#!/usr/bin/env python3
"""Enrich live Kansas HOA locations from Serper Places results.

This is the higher-yield KS map cleanup pass. It queries Serper Places for
currently unmapped live KS HOAs, accepts only Kansas-bounded/name-matching
results, and writes records suitable for `/admin/backfill-locations`.

Quality semantics:
- address: result has a street-like Kansas address.
- place_centroid: result is a Kansas subdivision/neighborhood/place centroid,
  not a street address. The live app must support this quality before applying.

The script defaults to dry-run and writes generated review output under
`state_scrapers/ks/results/`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from state_scrapers.ks.scripts.enrich_live_locations_from_ocr import _fetch_render_jwt  # noqa: E402

BASE_URL = "https://hoaproxy.org"
STATE = "KS"
KS_BBOX = (36.8, 40.2, -102.2, -94.4)  # min_lat, max_lat, min_lon, max_lon
SERPER_ENDPOINT = "https://google.serper.dev/places"
USER_AGENT = "HOAproxy KS Serper place cleanup/1.0 (admin@hoaproxy.org)"

STOP_TOKENS = {
    "and",
    "association",
    "assn",
    "at",
    "community",
    "home",
    "homeowner",
    "homeowners",
    "homes",
    "hoa",
    "inc",
    "llc",
    "of",
    "owner",
    "owners",
    "property",
    "the",
    "townhome",
    "townhomes",
    "villa",
    "villas",
}

BAD_CATEGORY_RE = re.compile(
    r"assisted living|senior|apartment|real estate agent|realtor|"
    r"property management|management company|law firm|attorney|city government",
    re.I,
)

SUFFIX_RE = re.compile(
    r"\b("
    r"homeowners association|home owners association|homes association|"
    r"property owners association|owners association|community association|"
    r"association|hoa|inc\.?|llc"
    r")\b",
    re.I,
)


def _request_json(method: str, url: str, **kwargs: Any) -> Any:
    response = requests.request(method, url, timeout=kwargs.pop("timeout", 30), **kwargs)
    response.raise_for_status()
    return response.json()


def _compact_name(value: str) -> str:
    value = SUFFIX_RE.sub(" ", value or "")
    value = re.sub(r"[^A-Za-z0-9 &'-]+", " ", value)
    return " ".join(value.split())


def _tokens(value: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9]+", (value or "").casefold())
        if len(word) > 2 and word not in STOP_TOKENS
    }


def _name_score(hoa_name: str, place_title: str) -> float:
    want = _tokens(_compact_name(hoa_name))
    got = _tokens(_compact_name(place_title))
    if not want or not got:
        return 0.0
    return len(want & got) / max(1, min(len(want), len(got)))


def _in_ks_bbox(lat: Any, lon: Any) -> bool:
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return False
    min_lat, max_lat, min_lon, max_lon = KS_BBOX
    return min_lat <= lat_f <= max_lat and min_lon <= lon_f <= max_lon


def _address_has_kansas(address: str | None) -> bool:
    return bool(re.search(r"\bKS\b|Kansas|\b66\d{3}\b", address or "", re.I))


def _has_street_address(address: str | None) -> bool:
    return bool(re.search(r"\d+\s+[A-Za-z0-9]", address or ""))


def _fetch_summaries(base_url: str) -> list[dict[str, Any]]:
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


def _acceptance(hoa: dict[str, Any], place: dict[str, Any], min_score: float) -> dict[str, Any] | None:
    title = str(place.get("title") or "")
    address = str(place.get("address") or "")
    category = str(place.get("category") or "")
    if not _in_ks_bbox(place.get("latitude"), place.get("longitude")):
        return None
    if not _address_has_kansas(address):
        return None

    score = _name_score(str(hoa["hoa"]), title)
    if score < min_score:
        return None

    city = str(hoa.get("city") or "").strip()
    if city and city.casefold() not in f"{title} {address}".casefold() and score < 1.0:
        return None

    bad_category = bool(BAD_CATEGORY_RE.search(category) or BAD_CATEGORY_RE.search(title))
    if bad_category and score < 1.0:
        return None
    if bad_category and len(_tokens(_compact_name(str(hoa["hoa"])))) <= 1:
        return None

    return {
        "score": score,
        "location_quality": "address" if _has_street_address(address) else "place_centroid",
    }


def _queries(hoa: dict[str, Any]) -> list[str]:
    name = _compact_name(str(hoa["hoa"]))
    city = str(hoa.get("city") or "").strip()
    out = []
    if city:
        out.append(f'"{name}" {city} KS')
    out.append(f'"{name}" Kansas subdivision')
    return out


def _search_places(api_key: str, query: str, num: int) -> list[dict[str, Any]]:
    response = requests.post(
        SERPER_ENDPOINT,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json", "User-Agent": USER_AGENT},
        json={"q": query, "gl": "us", "hl": "en", "num": num},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("places") or []


def build_records(args: argparse.Namespace) -> dict[str, Any]:
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        raise RuntimeError("SERPER_API_KEY is required")

    summaries = _fetch_summaries(args.base_url)
    map_points = _request_json("GET", f"{args.base_url}/hoas/map-points", params={"state": STATE})
    mapped = {row.get("hoa") for row in map_points}

    records: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    checked = 0
    for hoa in summaries:
        if hoa.get("hoa") in mapped and not args.include_mapped:
            continue
        if args.limit and checked >= args.limit:
            break
        checked += 1

        best_place = None
        best_meta = None
        best_query = None
        for query in _queries(hoa):
            places = _search_places(api_key, query, args.num_results)
            for place in places:
                meta = _acceptance(hoa, place, args.min_score)
                if not meta:
                    continue
                if (
                    best_meta is None
                    or meta["score"] > best_meta["score"]
                    or (
                        meta["score"] == best_meta["score"]
                        and meta["location_quality"] == "address"
                        and best_meta["location_quality"] != "address"
                    )
                ):
                    best_place = place
                    best_meta = meta
                    best_query = query
            if best_meta and best_meta["score"] >= 1.0 and best_meta["location_quality"] == "address":
                break
            time.sleep(max(0.0, args.delay_s / 2))

        if best_place and best_meta:
            address = str(best_place.get("address") or "")
            record = {
                "hoa": hoa["hoa"],
                "street": address if best_meta["location_quality"] == "address" else None,
                "city": hoa.get("city") or None,
                "state": STATE,
                "latitude": float(best_place["latitude"]),
                "longitude": float(best_place["longitude"]),
                "source": "serper_places",
                "location_quality": best_meta["location_quality"],
                "_title": best_place.get("title"),
                "_address": address,
                "_category": best_place.get("category"),
                "_score": best_meta["score"],
                "_query": best_query,
            }
            records.append(record)
            audit.append({"hoa": hoa["hoa"], "decision": "mapped", "record": record})
        else:
            audit.append({"hoa": hoa["hoa"], "decision": "unmapped"})

        if checked % 25 == 0:
            _write_output(args.output, records, audit, summaries, mapped, checked)
            print(json.dumps({"checked": checked, "records": len(records)}), flush=True)
        time.sleep(max(0.0, args.delay_s))

    return {
        "records": records,
        "audit": audit,
        "summary_total": len(summaries),
        "already_mapped": len(mapped),
        "checked": checked,
    }


def _write_output(
    output: Path,
    records: list[dict[str, Any]],
    audit: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    mapped: set[str],
    checked: int,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "records": records,
                "audit": audit,
                "summary_total": len(summaries),
                "already_mapped": len(mapped),
                "checked": checked,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def apply_records(args: argparse.Namespace, records: list[dict[str, Any]]) -> dict[str, Any]:
    jwt = _fetch_render_jwt()
    clean = [{k: v for k, v in record.items() if not k.startswith("_") and v is not None} for record in records]
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
    parser.add_argument("--output", type=Path, default=Path("state_scrapers/ks/results/live_location_serper_places.json"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-mapped", action="store_true")
    parser.add_argument("--num-results", type=int, default=5)
    parser.add_argument("--min-score", type=float, default=0.75)
    parser.add_argument("--delay-s", type=float, default=0.15)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    payload = build_records(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

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
        print(json.dumps({"applied": apply_records(args, payload["records"])}, sort_keys=True))
    elif not args.apply:
        print("dry-run only; pass --apply to post records", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
