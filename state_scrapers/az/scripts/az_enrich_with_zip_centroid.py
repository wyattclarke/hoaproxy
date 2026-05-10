#!/usr/bin/env python3
"""ZIP-centroid geometry baseline for AZ bank manifests.

For each AZ bank manifest:
  1. Read manifest.address.postal_code (or manifest.address.zip).
  2. If geometry.confidence is already 'subdpoly-polygon' or
     'place-polygon' (better-quality), skip.
  3. Look up ZIP in Census 2020 ZCTA gazetteer; stamp centroid lat/lon.
  4. Mark geometry.source='zip-centroid', confidence='zip-centroid'.

Idempotent. Read-modify-write via gsutil.

Usage:
    source .venv/bin/activate
    python state_scrapers/az/scripts/az_enrich_with_zip_centroid.py \\
        --apply [--counties pima,maricopa,...]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GAZ_PATH = ROOT / "data" / "gazetteer" / "2023_Gaz_zcta_national.txt"
RESULTS_DIR = ROOT / "state_scrapers" / "az" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

BANK_BUCKET = "hoaproxy-bank"
BANK_PREFIX = f"gs://{BANK_BUCKET}/v1/AZ"

# Geometry sources we won't overwrite (better quality than ZIP centroid).
SOURCES_NO_OVERWRITE = {
    "subdpoly-polygon",
    "place-polygon",
    "osm-place-polygon",
    "here-address",
}


def load_zcta_centroids() -> dict[str, tuple[float, float]]:
    """Return zip5 -> (lat, lon)."""
    if not GAZ_PATH.exists():
        raise FileNotFoundError(f"missing {GAZ_PATH}; run from project root")
    out: dict[str, tuple[float, float]] = {}
    with GAZ_PATH.open() as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        # The Census gazetteer has whitespace-padded headers/values.
        for row in reader:
            zip5 = (row.get("GEOID") or row.get("GEOID_ZCTA5") or "").strip().zfill(5)
            if not zip5 or len(zip5) != 5:
                continue
            try:
                lat = float((row.get("INTPTLAT") or "").strip())
                lon = float((row.get("INTPTLONG") or "").strip())
            except ValueError:
                continue
            out[zip5] = (lat, lon)
    return out


def gcs_read_json(uri: str) -> dict | None:
    try:
        r = subprocess.run(["gsutil", "cat", uri], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


def gcs_write_json(uri: str, data: dict) -> bool:
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    try:
        r = subprocess.run(["gsutil", "cp", "-", uri], input=payload,
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def list_county_manifests(county_slug: str) -> list[str]:
    cmd = ["gsutil", "ls", f"{BANK_PREFIX}/{county_slug}/**/manifest.json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception:
        return []
    return [l.strip() for l in (r.stdout or "").splitlines() if l.strip().endswith("manifest.json")]


def list_all_az_counties() -> list[str]:
    cmd = ["gsutil", "ls", f"{BANK_PREFIX}/"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return []
    counties = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line.endswith("/"):
            continue
        slug = line.rstrip("/").rsplit("/", 1)[-1]
        if slug:
            counties.append(slug)
    return counties


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--counties", default="",
                   help="Comma-separated county slugs (default: all under v1/AZ)")
    p.add_argument("--limit-per-county", type=int, default=None)
    args = p.parse_args()

    print("Loading ZCTA centroids ...")
    zcta = load_zcta_centroids()
    print(f"  loaded {len(zcta):,} ZIPs with centroids")

    if args.counties:
        counties = [c.strip() for c in args.counties.split(",") if c.strip()]
    else:
        counties = list_all_az_counties()
    print(f"Counties: {counties}")

    started = time.time()
    summary: dict[str, dict] = {}
    for county in counties:
        manifests = list_county_manifests(county)
        if args.limit_per_county:
            manifests = manifests[:args.limit_per_county]
        stats = {"manifests": len(manifests), "stamped": 0, "skip_better": 0,
                 "skip_no_zip": 0, "skip_unknown_zip": 0, "write_fail": 0}
        for uri in manifests:
            m = gcs_read_json(uri)
            if not m:
                continue
            existing = (m.get("geometry") or {}).get("source") or ""
            if existing in SOURCES_NO_OVERWRITE:
                stats["skip_better"] += 1
                continue
            addr = m.get("address") or {}
            zip5 = (addr.get("postal_code") or addr.get("zip") or "").strip()
            if zip5 and "-" in zip5:
                zip5 = zip5.split("-", 1)[0]
            zip5 = zip5.zfill(5) if zip5 and zip5.isdigit() else ""
            if not zip5:
                stats["skip_no_zip"] += 1
                continue
            if zip5 not in zcta:
                stats["skip_unknown_zip"] += 1
                continue
            lat, lon = zcta[zip5]
            new_geom = {
                "source": "zip-centroid",
                "confidence": "zip-centroid",
                "centroid_lat": lat,
                "centroid_lon": lon,
                "match": {"postal_code": zip5},
                "enriched_at": datetime.now(tz=timezone.utc).isoformat(),
            }
            if not args.apply:
                stats["stamped"] += 1
                continue
            audits = m.setdefault("audit", {})
            audits.setdefault("geometry_history", []).append({
                "previous_source": existing or None,
                "replaced_at": new_geom["enriched_at"],
                "replaced_by": "zip-centroid",
            })
            m["geometry"] = new_geom
            if gcs_write_json(uri, m):
                stats["stamped"] += 1
            else:
                stats["write_fail"] += 1
        summary[county] = stats
        print(f"  {county:20s} stamped={stats['stamped']:5d} skip_better={stats['skip_better']:5d}"
              f" skip_no_zip={stats['skip_no_zip']:5d} skip_unknown={stats['skip_unknown_zip']:5d}")

    elapsed = time.time() - started
    out = RESULTS_DIR / f"az_zip_centroid_summary_{int(time.time())}.json"
    out.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(f"\nSummary: {out}\nWall time: {elapsed/60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
