#!/usr/bin/env python3
"""
Import scraped Tucson HOA data into the HOAware database.
Reads from data/tucson_hoa/tucson_hoa.geojson and inserts into hoas + hoa_locations.
"""

import json
import os
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hoaware import db
from hoaware.config import load_settings

GEOJSON_PATH = ROOT / "data" / "tucson_hoa" / "tucson_hoa.geojson"


def title_case_name(name: str) -> str:
    """Convert 'ACACIA PUEBLO HOMEOWNERS ASSOCIATION' → 'Acacia Pueblo Homeowners Association'."""
    # Words that should stay lowercase (unless first word)
    lower_words = {"of", "the", "at", "in", "for", "and", "or", "no", "no."}
    parts = name.split()
    result = []
    for i, word in enumerate(parts):
        if i == 0:
            result.append(word.capitalize())
        elif word.lower() in lower_words:
            result.append(word.lower())
        else:
            result.append(word.capitalize())
    return " ".join(result)


def compute_centroid(geometry: dict) -> tuple[float, float] | None:
    """Compute centroid (lat, lon) from a GeoJSON geometry."""
    points = []

    def collect(coords):
        if not isinstance(coords, list):
            return
        if coords and isinstance(coords[0], (int, float)) and len(coords) >= 2:
            points.append((coords[1], coords[0]))  # lat, lon
            return
        for child in coords:
            collect(child)

    collect(geometry.get("coordinates"))
    if not points:
        return None
    avg_lat = sum(p[0] for p in points) / len(points)
    avg_lon = sum(p[1] for p in points) / len(points)
    return avg_lat, avg_lon


def main():
    settings = load_settings()

    with open(GEOJSON_PATH) as f:
        data = json.load(f)

    features = data["features"]
    print(f"Loaded {len(features)} features from {GEOJSON_PATH.name}")

    # Filter to active HOAs only
    active = [f for f in features if f["properties"].get("HOA_STATUS") == "1"]
    inactive = len(features) - len(active)
    print(f"Active: {len(active)}, Inactive: {inactive} (skipping inactive)")

    imported = 0
    skipped = 0

    with db.get_connection(settings.db_path) as conn:
        for feat in active:
            props = feat["properties"]
            raw_name = props.get("HOA_NAME", "").strip()
            if not raw_name:
                skipped += 1
                continue

            hoa_name = title_case_name(raw_name)
            geometry = feat.get("geometry")

            # Build boundary GeoJSON string (just the geometry, Polygon/MultiPolygon)
            boundary_geojson = None
            if geometry and geometry.get("coordinates"):
                boundary_geojson = json.dumps({
                    "type": geometry["type"],
                    "coordinates": geometry["coordinates"],
                })

            # Compute centroid from boundary
            centroid = compute_centroid(geometry) if geometry else None
            lat = centroid[0] if centroid else None
            lon = centroid[1] if centroid else None

            # Subdivision name as display name if different from HOA name
            sub_name = props.get("SUB_NAME", "").strip()
            display_name = title_case_name(sub_name) if sub_name else None

            db.upsert_hoa_location(
                conn,
                hoa_name,
                display_name=display_name,
                city="Tucson",
                state="AZ",
                country="US",
                latitude=lat,
                longitude=lon,
                boundary_geojson=boundary_geojson,
                source="arcgis_tucson",
            )
            imported += 1

        conn.commit()

    print(f"\nDone: {imported} HOAs imported, {skipped} skipped (no name)")
    print(f"Database: {settings.db_path}")


if __name__ == "__main__":
    main()
