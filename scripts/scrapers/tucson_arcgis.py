#!/usr/bin/env python3
"""
Scrape Tucson HOA boundary data from City of Tucson ArcGIS feature layer.

Outputs:
  data/tucson_hoa/import.json   — standard bulk-import format
  data/tucson_hoa/tucson_hoa.geojson — raw GeoJSON for reference

Source: https://gis.tucsonaz.gov/public/rest/services/PublicMaps/NeighborhoodsPlans/MapServer/14
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from base import compute_centroid, title_case_name, write_import_file

SERVICE_URL = (
    "https://gis.tucsonaz.gov/public/rest/services/"
    "PublicMaps/NeighborhoodsPlans/MapServer/14/query"
)

OUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "tucson_hoa"


def fetch_geojson() -> dict:
    """Fetch all features with geometry in WGS84."""
    params = {
        "where": "1=1",
        "outFields": "OBJECTID,SUB_NAME,HOA_NAME,HOA_STATUS",
        "returnGeometry": "true",
        "outSR": "4326",
        "geometryPrecision": "6",
        "f": "geojson",
    }
    url = f"{SERVICE_URL}?{urlencode(params)}"
    print(f"Fetching: {url[:120]}...")
    req = Request(url, headers={"User-Agent": "HOAproxy-scraper/1.0"})
    with urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def to_import_records(geojson: dict) -> list[dict]:
    """Convert GeoJSON features to standard import records."""
    features = geojson.get("features", [])
    active = [f for f in features if f.get("properties", {}).get("HOA_STATUS") == "1"]
    print(f"Total features: {len(features)}, Active: {len(active)}")

    records = []
    for feat in active:
        props = feat["properties"]
        raw_name = (props.get("HOA_NAME") or "").strip()
        if not raw_name:
            continue

        geometry = feat.get("geometry")
        boundary_geojson = None
        if geometry and geometry.get("coordinates"):
            boundary_geojson = {
                "type": geometry["type"],
                "coordinates": geometry["coordinates"],
            }

        centroid = compute_centroid(geometry) if geometry else None
        sub_name = (props.get("SUB_NAME") or "").strip()

        records.append({
            "name": title_case_name(raw_name),
            "display_name": title_case_name(sub_name) if sub_name else None,
            "city": "Tucson",
            "state": "AZ",
            "country": "US",
            "latitude": centroid[0] if centroid else None,
            "longitude": centroid[1] if centroid else None,
            "boundary_geojson": boundary_geojson,
        })

    return records


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    geojson = fetch_geojson()

    # Save raw GeoJSON for reference
    geojson_path = OUT_DIR / "tucson_hoa.geojson"
    with open(geojson_path, "w") as f:
        json.dump(geojson, f)
    n = len(geojson.get("features", []))
    size_mb = geojson_path.stat().st_size / (1024 * 1024)
    print(f"Saved {n} features → {geojson_path} ({size_mb:.1f} MB)")

    # Convert to import format
    records = to_import_records(geojson)
    write_import_file(records, source="arcgis_tucson", output_path=OUT_DIR / "import.json")


if __name__ == "__main__":
    main()
