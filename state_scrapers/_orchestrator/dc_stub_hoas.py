#!/usr/bin/env python3
"""DC condo docless-stub experiment: bulk-create stub HOAs from the CAMA
seed JSONL with polygon centroids from DC GIS Layer 40.

For each of the 3,289 entities in `dc_cama_condo_seed.jsonl`:
  1. Skip if already live (slug matches existing live HOA name).
  2. Skip parser artifacts (single-word names, all-numeric, etc.).
  3. Query Layer 40 (Owner Polygons / Common Ownership Layer) WHERE
     CONDO_REGIME_NUM=<regime> to get the polygon for the condo regime.
  4. If hit: compute centroid → location_quality='polygon', also save
     simplified boundary_geojson for map display. Reverse-geocode via
     DC Master Address Repository (MAR) for street address.
  5. If miss: fall back to city=Washington, state=DC, location_quality
     unset (city-only — won't appear on map but still in /hoas/summary).
  6. Batch POST to /admin/create-stub-hoas in batches of 50.

This is an experiment to test whether having ~3,000 docless DC condos
(name + location only) is worth shipping. The pipeline currently treats
docs as authoritative; this stub flow is the first time we surface
entities without docs.

Run:
  .venv/bin/python state_scrapers/_orchestrator/dc_stub_hoas.py --apply
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
SEED_PATH = ROOT / "state_scrapers/dc/leads/dc_cama_condo_seed.jsonl"
LAYER40 = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/Property_and_Land_WebMercator/FeatureServer/40/query"
MAR_REVERSE = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_APPS/DCGIS_MAR/GeocodeServer/reverseGeocode"

DC_BBOX = {"min_lat": 38.79, "max_lat": 39.00, "min_lon": -77.12, "max_lon": -76.91}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def live_admin_token() -> str | None:
    if os.environ.get("HOAPROXY_ADMIN_BEARER"):
        return os.environ["HOAPROXY_ADMIN_BEARER"]
    api_key = os.environ.get("RENDER_API_KEY")
    service_id = os.environ.get("RENDER_SERVICE_ID")
    if api_key and service_id:
        try:
            r = requests.get(
                f"https://api.render.com/v1/services/{service_id}/env-vars",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            r.raise_for_status()
            for env in r.json():
                e = env.get("envVar", env)
                if e.get("key") == "JWT_SECRET" and e.get("value"):
                    return e["value"]
        except Exception:
            pass
    return os.environ.get("JWT_SECRET")


def is_acceptable_name(name: str) -> tuple[bool, str | None]:
    """Filter parser artifacts and obviously-bad names."""
    if not name or len(name) < 4:
        return False, "too_short"
    if not re.search(r"[A-Za-z]", name):
        return False, "no_letters"
    # Single all-letter word (likely a parser artifact like "Kewalo")
    if re.match(r"^[A-Z][a-z]+$", name):
        return False, "single_word"
    if re.match(r"^[A-Z][A-Za-z]+\s+[A-Z][a-z]+$", name) and "Condo" not in name:
        # two-word names without Condo suffix — likely a project name fragment
        # (we still keep them; pretty common in real condo names)
        pass
    return True, None


def beautify_name(name: str) -> str:
    """Light cosmetic touches for display:
      - "Condo" -> "Condominium"
      - title-case the okina-bearing words
      - tidy whitespace
    """
    n = re.sub(r"\bCondo(s)?\b", "Condominium", name).strip()
    n = re.sub(r"\s+", " ", n)
    return n


def fetch_existing_live_names(base_url: str) -> set[str]:
    try:
        r = requests.get(f"{base_url}/hoas/summary", params={"state": "DC", "limit": 5000}, timeout=60)
        body = r.json()
        results = body.get("results") if isinstance(body, dict) else body
        return {(row.get("hoa") or "").strip().lower() for row in (results or [])}
    except Exception:
        return set()


def query_layer40(regime: str, session: requests.Session) -> dict[str, Any] | None:
    """Return {centroid: (lon, lat), boundary_geojson: str, ssl: str} or None."""
    try:
        r = session.get(
            LAYER40,
            params={
                "where": f"CONDO_REGIME_NUM='{regime}' AND UNDERLIES_CONDO=1",
                "outFields": "SSL,SQUARE,SUFFIX,LOT",
                "returnGeometry": "true",
                "outSR": 4326,
                "f": "json",
                "resultRecordCount": 1,
            },
            timeout=20,
        )
        if r.status_code != 200:
            return None
        d = r.json()
        feats = d.get("features", [])
        if not feats:
            return None
        f = feats[0]
        attrs = f.get("attributes") or {}
        rings = (f.get("geometry") or {}).get("rings", [])
        if not rings or not rings[0]:
            return None
        # Compute centroid (simple averaging of vertices)
        coords = rings[0]
        xs = [p[0] for p in coords]
        ys = [p[1] for p in coords]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        # Validate bbox
        if not (DC_BBOX["min_lat"] <= cy <= DC_BBOX["max_lat"]
                and DC_BBOX["min_lon"] <= cx <= DC_BBOX["max_lon"]):
            return None
        # Build boundary GeoJSON Polygon (rings[0] is the outer ring)
        boundary = {
            "type": "Polygon",
            "coordinates": [coords],
        }
        return {
            "centroid": (cx, cy),
            "boundary_geojson": json.dumps(boundary),
            "ssl": (attrs.get("SSL") or "").strip(),
        }
    except Exception:
        return None


def reverse_geocode_mar(lon: float, lat: float, session: requests.Session) -> dict[str, str] | None:
    """Best-effort DC MAR reverse-geocode → {street, city, postal_code}."""
    try:
        r = session.get(
            MAR_REVERSE,
            params={"location": f"{lon},{lat}", "distance": 200, "f": "json"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        d = r.json()
        addr = d.get("address") or {}
        out = {
            "street": addr.get("StAddr") or addr.get("Address"),
            "city": "Washington",
            "state": "DC",
            "postal_code": addr.get("Postal") or addr.get("ZIP4"),
        }
        return out
    except Exception:
        return None


def post_batch(records: list[dict], base_url: str, token: str) -> dict:
    try:
        r = requests.post(
            f"{base_url}/admin/create-stub-hoas",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"records": records},
            timeout=600,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text[:500]}
        return {"status": r.status_code, **body}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org"))
    parser.add_argument("--seed", default=str(SEED_PATH))
    parser.add_argument("--ledger", default=str(ROOT / f"state_scrapers/dc/results/dc_stub_hoas_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl"))
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--limit", type=int, default=0, help="0 = all")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-mar", action="store_true", help="Skip DC MAR reverse-geocode (faster, no street address)")
    parser.add_argument("--mar-delay", type=float, default=0.05, help="Seconds between MAR calls")
    parser.add_argument("--gis-delay", type=float, default=0.1, help="Seconds between Layer 40 queries")
    args = parser.parse_args()

    token = live_admin_token()
    if not token:
        print("FATAL: no admin token", file=sys.stderr)
        return 2

    seed_path = Path(args.seed)
    if not seed_path.exists():
        print(f"FATAL: seed file missing: {seed_path}", file=sys.stderr)
        return 2

    ledger_path = Path(args.ledger)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    existing = fetch_existing_live_names(args.base)
    print(f"Live DC HOA names already present: {len(existing)}", file=sys.stderr)

    # Load seeds
    seeds: list[dict] = []
    with seed_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                seeds.append(json.loads(line))
            except Exception:
                continue
    if args.limit:
        seeds = seeds[: args.limit]
    print(f"Loaded {len(seeds)} seed entries", file=sys.stderr)

    session = requests.Session()
    session.headers["User-Agent"] = "HOAproxy stub-hoa builder (+https://hoaproxy.org)"

    pending: list[dict] = []
    counts = {"skipped_artifact": 0, "skipped_existing": 0, "polygon": 0, "city_only": 0, "mar_hits": 0, "errors": 0}
    t0 = time.time()
    last_report = t0

    for i, seed in enumerate(seeds):
        raw_name = (seed.get("name") or "").strip()
        ok, reason = is_acceptable_name(raw_name)
        if not ok:
            counts["skipped_artifact"] += 1
            with ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"name": raw_name, "skipped": reason}, sort_keys=True) + "\n")
            continue
        clean_name = beautify_name(raw_name)
        if clean_name.lower() in existing:
            counts["skipped_existing"] += 1
            continue

        regime = str(seed.get("regime") or "").strip()
        record: dict[str, Any] = {
            "name": clean_name,
            "metadata_type": (seed.get("metadata_type") or "condo").lower(),
            "city": "Washington",
            "state": "DC",
            "source": seed.get("source") or "dc-gis-cama-condo-regime",
            "source_url": seed.get("source_url"),
        }

        # Try to get polygon
        if regime:
            geo = query_layer40(regime, session)
            time.sleep(args.gis_delay)
            if geo:
                cx, cy = geo["centroid"]
                record["latitude"] = cy
                record["longitude"] = cx
                record["location_quality"] = "polygon"
                record["boundary_geojson"] = geo["boundary_geojson"]
                # MAR reverse-geocode for street address
                if not args.skip_mar:
                    mar = reverse_geocode_mar(cx, cy, session)
                    time.sleep(args.mar_delay)
                    if mar and mar.get("street"):
                        record["street"] = mar["street"]
                        if mar.get("postal_code"):
                            record["postal_code"] = mar["postal_code"]
                        counts["mar_hits"] += 1
                counts["polygon"] += 1
            else:
                # Fall back to city-only
                record["location_quality"] = "city_only"
                counts["city_only"] += 1
        else:
            record["location_quality"] = "city_only"
            counts["city_only"] += 1

        pending.append(record)

        # Periodic progress + flush batches
        if len(pending) >= args.batch_size:
            if args.apply:
                result = post_batch(pending, args.base, token)
                with ledger_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"event": "batch", "size": len(pending), "result": result}, sort_keys=True) + "\n")
                if result.get("status", 200) != 200 or result.get("error"):
                    counts["errors"] += 1
            else:
                with ledger_path.open("a", encoding="utf-8") as f:
                    for rec in pending:
                        f.write(json.dumps({"dry_run": rec}, sort_keys=True) + "\n")
            pending = []

        now = time.time()
        if now - last_report >= 30 or i == len(seeds) - 1:
            last_report = now
            rate = (i + 1) / max(0.001, now - t0)
            print(
                f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                f"{i+1}/{len(seeds)} ({rate:.1f}/s) "
                f"polygon={counts['polygon']} city_only={counts['city_only']} "
                f"mar={counts['mar_hits']} skip_artifact={counts['skipped_artifact']} "
                f"skip_existing={counts['skipped_existing']} errors={counts['errors']}",
                file=sys.stderr, flush=True,
            )

    # Flush any remaining
    if pending and args.apply:
        result = post_batch(pending, args.base, token)
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "batch_final", "size": len(pending), "result": result}, sort_keys=True) + "\n")

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
