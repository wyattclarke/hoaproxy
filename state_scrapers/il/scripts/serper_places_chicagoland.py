#!/usr/bin/env python3
"""Serper Places mapping enrichment for unmapped Chicagoland HOAs.

For each currently-live IL HOA in Chicagoland scope WITHOUT lat/lon, query
Serper Places for "<name>, <city>, IL" and POST the result to /admin/backfill-
locations with location_quality="place_centroid".

Same pattern that lifted GA from 14% → 60% map coverage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)

sys.path.insert(0, str(ROOT / "state_scrapers" / "ga" / "scripts"))
from clean_dirty_hoa_names import _fetch_summaries, _live_admin_token  # noqa: E402

sys.path.insert(0, str(ROOT / "state_scrapers" / "il" / "scripts"))
from dedup_and_clean_il_chicagoland import (  # noqa: E402
    _is_chicagoland_eligible,
    _name_to_prefix_map,
)

BASE_URL = "https://hoaproxy.org"
SERPER_PLACES = "https://google.serper.dev/places"

# IL bbox — anything outside is rejected even if Places returned a hit.
IL_BBOX = {"min_lat": 36.97, "max_lat": 42.51, "min_lon": -91.52, "max_lon": -87.49}


def serper_places(query: str) -> list[dict[str, Any]]:
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        raise RuntimeError("SERPER_API_KEY not set")
    body = {"q": query, "gl": "us"}
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    r = requests.post(SERPER_PLACES, json=body, headers=headers, timeout=30)
    if r.status_code != 200:
        return []
    return r.json().get("places") or []


def in_il(lat: float, lon: float) -> bool:
    return (
        IL_BBOX["min_lat"] <= lat <= IL_BBOX["max_lat"]
        and IL_BBOX["min_lon"] <= lon <= IL_BBOX["max_lon"]
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--name-to-prefix", action="append", default=None)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--out-dir",
                   default="state_scrapers/il/results/places_chicagoland")
    p.add_argument("--sleep-s", type=float, default=0.4)
    p.add_argument("--max-queries", type=int, default=200,
                   help="Serper Places budget cap (each call ~$0.001)")
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = out_dir / "places_decisions.jsonl"

    paths = [Path(p) for p in (args.name_to_prefix or [
        "state_scrapers/il/results/il_20260508_114942_claude_phase2/live_import_report.json",
        "state_scrapers/il/results/il_chicagoland_20260509_061922_claude_phase2/live_import_report.json",
        "state_scrapers/il/results/il_chicagoland_assessor_20260509_145512_claude_phase2/live_import_report.json",
    ])]
    name_to_prefix = _name_to_prefix_map(paths)
    summaries = _fetch_summaries(args.base_url, "IL")

    eligible = [r for r in summaries if _is_chicagoland_eligible(r.get("hoa") or "", name_to_prefix)[0]]
    unmapped = [r for r in eligible if r.get("latitude") is None]
    print(f"Chicagoland eligible: {len(eligible)}", file=sys.stderr)
    print(f"Unmapped: {len(unmapped)}", file=sys.stderr)

    decisions: list[dict[str, Any]] = []
    backfill_records: list[dict[str, Any]] = []
    queries = 0

    for i, row in enumerate(unmapped, 1):
        if queries >= args.max_queries:
            print(f"hit max-queries cap {args.max_queries}", file=sys.stderr)
            break
        name = row.get("hoa") or ""
        city = row.get("city") or ""
        # Build query: "<name>" "Illinois" with city if known
        if city:
            q = f'"{name}" "{city}, Illinois"'
        else:
            q = f'"{name}" Chicago Illinois'
        try:
            places = serper_places(q)
        except Exception as e:
            decisions.append({"hoa_id": row["hoa_id"], "name": name, "decision": "serper_error", "error": str(e)})
            continue
        queries += 1

        if not places:
            decisions.append({"hoa_id": row["hoa_id"], "name": name, "decision": "no_results", "query": q})
            time.sleep(args.sleep_s)
            continue

        # Pick best match: must have lat/lon inside IL bbox
        best = None
        for pl in places:
            lat = pl.get("latitude")
            lon = pl.get("longitude")
            if lat is None or lon is None:
                continue
            try:
                lat_f = float(lat); lon_f = float(lon)
            except (TypeError, ValueError):
                continue
            if not in_il(lat_f, lon_f):
                continue
            best = {**pl, "latitude": lat_f, "longitude": lon_f}
            break
        if not best:
            decisions.append({"hoa_id": row["hoa_id"], "name": name, "decision": "no_il_match", "query": q})
            time.sleep(args.sleep_s)
            continue

        rec = {
            "hoa": name,
            "latitude": best["latitude"],
            "longitude": best["longitude"],
            "street": (best.get("address") or "").split(",")[0].strip() or None,
            "city": best.get("address", "").split(",")[1].strip() if "," in (best.get("address") or "") else None,
            "state": "IL",
            "location_quality": "place_centroid",
        }
        # Drop None fields
        rec = {k: v for k, v in rec.items() if v is not None}
        backfill_records.append(rec)
        decisions.append({
            "hoa_id": row["hoa_id"], "name": name,
            "decision": "matched",
            "lat": best["latitude"], "lon": best["longitude"],
            "place_address": best.get("address"),
            "place_title": best.get("title"),
        })
        if i % 20 == 0:
            print(f"  scanned {i}/{len(unmapped)} (matched={len(backfill_records)})", file=sys.stderr)
            decisions_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
        time.sleep(args.sleep_s)

    decisions_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
    print(json.dumps({
        "unmapped_total": len(unmapped),
        "queries_run": queries,
        "matched": len(backfill_records),
        "approx_serper_spend_usd": round(queries * 0.001, 3),
        "decisions_path": str(decisions_path),
    }, sort_keys=True))

    if not args.apply or not backfill_records:
        if not args.apply:
            print("dry-run; pass --apply to backfill", file=sys.stderr)
        return 0

    token = _live_admin_token()
    if not token:
        print("no admin token", file=sys.stderr)
        return 1
    headers = {"Authorization": f"Bearer {token}"}
    # Apply in chunks of 50
    for i in range(0, len(backfill_records), 50):
        chunk = backfill_records[i:i + 50]
        r = requests.post(
            f"{args.base_url}/admin/backfill-locations",
            headers=headers,
            json={"records": chunk},
            timeout=300,
        )
        r.raise_for_status()
        print(f"backfill chunk {i//50 + 1}: {r.json()}", file=sys.stderr)
        time.sleep(1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
