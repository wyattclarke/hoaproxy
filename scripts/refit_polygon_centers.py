#!/usr/bin/env python3
"""Backfill fix: when an HOA has a polygon AND a lat/lon far from the polygon
centroid, the lat/lon is almost certainly a stale city-center geocode from
the old code path that ran address-geocoding before consulting the polygon.
Replace those lat/lon values with the polygon centroid.

Why this exists: pre-fix /upload computed lat/lon from "city, state" geocoding
(which lands on the city center) BEFORE checking the polygon. The polygon was
the more specific source of truth; we want the centroid to win.

Usage:
    HOA_DB_PATH=/path/to/prod-or-local.db python scripts/refit_polygon_centers.py --dry-run
    HOA_DB_PATH=/path/to/prod-or-local.db python scripts/refit_polygon_centers.py
    # or against prod via the admin endpoint:
    curl -X POST -H "Authorization: Bearer $JWT_SECRET" \
      "https://hoaproxy.org/admin/refit-polygon-centers?distance_threshold_km=1.5"
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hoaware import db
from hoaware.config import load_settings


def polygon_center(boundary_geojson_str: str) -> tuple[float, float] | None:
    """Return (lat, lon) bbox-centroid of a GeoJSON Polygon/MultiPolygon."""
    try:
        parsed = json.loads(boundary_geojson_str)
    except Exception:
        return None
    points: list[tuple[float, float]] = []

    def collect(coords):
        if not isinstance(coords, list):
            return
        if coords and isinstance(coords[0], (int, float)) and len(coords) >= 2:
            points.append((float(coords[1]), float(coords[0])))
            return
        for c in coords:
            collect(c)

    collect(parsed.get("coordinates"))
    if not points:
        return None
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return ((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular approximation — good enough for the threshold check."""
    dx = (lon2 - lon1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * 111.32
    return math.sqrt(dx * dx + dy * dy)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--distance-threshold-km", type=float, default=1.5,
                    help="Update lat/lon if it's farther than this from the polygon centroid")
    args = ap.parse_args()

    settings = load_settings()
    print(f"Target DB: {settings.db_path}")
    print(f"Threshold: {args.distance_threshold_km} km from polygon centroid")
    print()

    fixed = 0
    skipped_already_close = 0
    skipped_no_polygon = 0
    skipped_no_latlon = 0
    bad_polygon = 0

    with db.get_connection(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT h.name, l.latitude, l.longitude, l.boundary_geojson
            FROM hoa_locations l
            JOIN hoas h ON h.id = l.hoa_id
            """
        ).fetchall()

    print(f"Examining {len(rows)} HOA locations...")
    updates: list[tuple[str, float, float, float, float, float]] = []

    for r in rows:
        if not r["boundary_geojson"]:
            skipped_no_polygon += 1
            continue
        center = polygon_center(r["boundary_geojson"])
        if not center:
            bad_polygon += 1
            continue
        if r["latitude"] is None or r["longitude"] is None:
            # No lat/lon — backfill from polygon
            skipped_no_latlon += 1
            updates.append((r["name"], center[0], center[1], None, None, 0.0))
            continue
        d = distance_km(r["latitude"], r["longitude"], center[0], center[1])
        if d <= args.distance_threshold_km:
            skipped_already_close += 1
            continue
        updates.append((r["name"], center[0], center[1], r["latitude"], r["longitude"], d))

    print()
    print(f"  HOAs with polygon:                  {len(rows) - skipped_no_polygon}")
    print(f"  Polygon parse failed:               {bad_polygon}")
    print(f"  Already close (≤ threshold):        {skipped_already_close}")
    print(f"  Will fix (lat/lon too far):         {len(updates) - skipped_no_latlon}")
    print(f"  Will fill (lat/lon was missing):    {skipped_no_latlon}")
    print()

    # Show sample
    print("Sample of fixes (sorted by distance descending):")
    for name, new_lat, new_lon, old_lat, old_lon, d in sorted(updates, key=lambda x: -x[5])[:15]:
        if old_lat is None:
            print(f"  {name[:40]:40s}  fill  → ({new_lat:.5f}, {new_lon:.5f})")
        else:
            print(f"  {name[:40]:40s}  move {d:6.1f}km  ({old_lat:.4f},{old_lon:.4f}) → ({new_lat:.5f}, {new_lon:.5f})")

    if args.dry_run:
        print("\nDRY-RUN — no writes.")
        return 0

    print(f"\nApplying {len(updates)} updates...")
    with db.get_connection(settings.db_path) as conn:
        for name, new_lat, new_lon, _, _, _ in updates:
            conn.execute(
                """
                UPDATE hoa_locations
                SET latitude = ?, longitude = ?
                WHERE hoa_id = (SELECT id FROM hoas WHERE name = ?)
                """,
                (new_lat, new_lon, name),
            )
        conn.commit()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
