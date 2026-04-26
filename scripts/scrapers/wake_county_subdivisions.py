#!/usr/bin/env python3
"""
Scrape subdivision boundary data from Wake County ArcGIS for Cary, NC (and optionally other jurisdictions).

Source: https://maps.wakegov.com/arcgis/rest/services/Planning/Subdivisions/MapServer/0

These are subdivision boundaries, not confirmed HOAs. Most Cary subdivisions
have HOAs, so this is a strong proxy. Names can be cross-referenced against
NC SOS nonprofit records or management company listings for confirmation.

Usage:
    python scripts/scrapers/wake_county_subdivisions.py [--jurisdiction CARY] [--all]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from base import compute_centroid, title_case_name, write_import_file

SERVICE_URL = (
    "https://maps.wakegov.com/arcgis/rest/services/"
    "Planning/Subdivisions/MapServer/0/query"
)

MAX_PER_REQUEST = 2000

OUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "wake_county"

# Map ArcGIS JURISDICTION values to city names
JURISDICTION_CITY = {
    "CARY": "Cary",
    "RALEIGH": "Raleigh",
    "APEX": "Apex",
    "HOLLY SPRINGS": "Holly Springs",
    "FUQUAY-VARINA": "Fuquay-Varina",
    "GARNER": "Garner",
    "KNIGHTDALE": "Knightdale",
    "MORRISVILLE": "Morrisville",
    "ROLESVILLE": "Rolesville",
    "WAKE FOREST": "Wake Forest",
    "WENDELL": "Wendell",
    "ZEBULON": "Zebulon",
}


def fetch_features(jurisdiction: str | None = None, offset: int = 0) -> dict:
    """Fetch subdivision features from Wake County ArcGIS."""
    where = "STATUS='EXISTING'"
    if jurisdiction:
        where += f" AND JURISDICTION='{jurisdiction}'"

    params = {
        "where": where,
        "outFields": "NAME,STATUS,JURISDICTION,LOTS,ACRES,APPROVDATE,SNUMBER",
        "returnGeometry": "true",
        "outSR": "4326",
        "geometryPrecision": "6",
        "f": "geojson",
        "resultRecordCount": str(MAX_PER_REQUEST),
        "resultOffset": str(offset),
    }
    url = f"{SERVICE_URL}?{urlencode(params)}"
    print(f"Fetching (offset={offset}): {url[:120]}...")
    req = Request(url, headers={"User-Agent": "HOAproxy-scraper/1.0"})
    with urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def fetch_all_features(jurisdiction: str | None = None) -> list[dict]:
    """Fetch all features, paginating if needed."""
    all_features = []
    offset = 0
    while True:
        data = fetch_features(jurisdiction=jurisdiction, offset=offset)
        features = data.get("features", [])
        all_features.extend(features)
        if len(features) < MAX_PER_REQUEST:
            break
        offset += len(features)
    return all_features


def to_import_records(features: list[dict]) -> list[dict]:
    """Convert GeoJSON features to standard import records."""
    records = []
    for feat in features:
        props = feat.get("properties", {})
        raw_name = (props.get("NAME") or "").strip()
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

        # Map jurisdiction to city name
        jurisdiction = (props.get("JURISDICTION") or "").strip().upper()
        city = JURISDICTION_CITY.get(jurisdiction, title_case_name(jurisdiction) if jurisdiction else None)

        # Use subdivision name as HOA name
        hoa_name = title_case_name(raw_name)
        # Append common suffix if the name doesn't already suggest an HOA/community
        if not any(kw in hoa_name.lower() for kw in ("hoa", "homeowner", "association", "community")):
            display_name = hoa_name
        else:
            display_name = None

        records.append({
            "name": hoa_name,
            "display_name": display_name,
            "city": city,
            "state": "NC",
            "country": "US",
            "latitude": centroid[0] if centroid else None,
            "longitude": centroid[1] if centroid else None,
            "boundary_geojson": boundary_geojson,
        })

    return records


def main():
    parser = argparse.ArgumentParser(description="Scrape Wake County subdivision boundaries")
    parser.add_argument("--jurisdiction", default="CARY",
                        help="ArcGIS JURISDICTION value (default: CARY). Use --all for everything.")
    parser.add_argument("--all", action="store_true",
                        help="Fetch all jurisdictions in Wake County")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    jurisdiction = None if args.all else args.jurisdiction.upper()
    label = "wake_county" if args.all else args.jurisdiction.lower()

    print(f"=== Fetching {'all Wake County' if args.all else args.jurisdiction} subdivisions ===")
    features = fetch_all_features(jurisdiction=jurisdiction)
    print(f"Got {len(features)} features")

    # Save raw GeoJSON
    geojson = {"type": "FeatureCollection", "features": features}
    geojson_path = OUT_DIR / f"{label}_subdivisions.geojson"
    with open(geojson_path, "w") as f:
        json.dump(geojson, f)
    size_mb = geojson_path.stat().st_size / (1024 * 1024)
    print(f"Saved {len(features)} features → {geojson_path} ({size_mb:.1f} MB)")

    # Convert to import format
    records = to_import_records(features)
    source = f"wake_county_subdivisions_{label}"
    import_path = OUT_DIR / f"{label}_import.json"
    write_import_file(records, source=source, output_path=import_path)

    # Summary
    cities = {}
    for r in records:
        c = r.get("city") or "Unknown"
        cities[c] = cities.get(c, 0) + 1
    print(f"\n=== Summary ===")
    print(f"Total records: {len(records)}")
    for city, count in sorted(cities.items(), key=lambda x: -x[1]):
        print(f"  {city}: {count}")


if __name__ == "__main__":
    main()
