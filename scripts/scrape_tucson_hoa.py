#!/usr/bin/env python3
"""
Scrape HOA boundary data from City of Tucson ArcGIS feature layer.
Source: https://www.arcgis.com/home/item.html?id=b98155307f8043b3b048cb79ece8a58c
Service: https://gis.tucsonaz.gov/public/rest/services/PublicMaps/NeighborhoodsPlans/MapServer/14
"""

import json
import csv
import sys
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.parse import urlencode

SERVICE_URL = (
    "https://gis.tucsonaz.gov/public/rest/services/"
    "PublicMaps/NeighborhoodsPlans/MapServer/14/query"
)

# Fields we care about (skip geometry for the CSV; fetch separately as GeoJSON)
ATTR_FIELDS = [
    "OBJECTID", "JURIS", "REC_DATE", "SUB_NAME", "BOOK_PAGE",
    "LOT_COUNT", "BLOCK_CT", "SEQ_NUM", "PROJ_NUM1", "REZONING",
    "REF_NUMS", "HOA_NAME", "HOA_STATUS", "CCR_SEQ_NUM", "ACC_EID2",
    "ST_DED", "DATASOURCE",
]

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "tucson_hoa"


def query_features(*, return_geometry: bool = False, out_sr: int | None = None):
    """Query all features from the ArcGIS layer."""
    params = {
        "where": "1=1",
        "outFields": ",".join(ATTR_FIELDS),
        "returnGeometry": "true" if return_geometry else "false",
        "f": "geojson" if return_geometry else "json",
    }
    if out_sr and return_geometry:
        params["outSR"] = str(out_sr)
    if return_geometry:
        params["geometryPrecision"] = "6"  # 6 decimal places ≈ 0.1 m

    url = f"{SERVICE_URL}?{urlencode(params)}"
    print(f"Fetching: {url[:120]}...")
    req = Request(url, headers={"User-Agent": "HOAproxy-scraper/1.0"})
    with urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return data


def save_csv(features: list[dict], path: Path):
    """Save attribute data as CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ATTR_FIELDS)
        writer.writeheader()
        for feat in features:
            attrs = feat.get("attributes", feat.get("properties", {}))
            row = {k: attrs.get(k, "") for k in ATTR_FIELDS}
            # Convert epoch millis to ISO date
            if row.get("REC_DATE") and isinstance(row["REC_DATE"], (int, float)):
                from datetime import datetime, timezone
                row["REC_DATE"] = datetime.fromtimestamp(
                    row["REC_DATE"] / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            writer.writerow(row)
    print(f"Saved {len(features)} rows → {path}")


def save_geojson(geojson: dict, path: Path):
    """Save GeoJSON (with WGS84 geometries)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(geojson, f)
    size_mb = path.stat().st_size / (1024 * 1024)
    n = len(geojson.get("features", []))
    print(f"Saved {n} features → {path} ({size_mb:.1f} MB)")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Fetch attributes only → CSV
    print("=== Fetching attributes ===")
    attr_data = query_features(return_geometry=False)
    features = attr_data.get("features", [])
    print(f"Got {len(features)} features")
    csv_path = OUT_DIR / "tucson_hoa.csv"
    save_csv(features, csv_path)

    # 2. Fetch with geometry in WGS84 (EPSG:4326) → GeoJSON
    print("\n=== Fetching geometries (WGS84) ===")
    geojson = query_features(return_geometry=True, out_sr=4326)
    geojson_path = OUT_DIR / "tucson_hoa.geojson"
    save_geojson(geojson, geojson_path)

    # 3. Print summary
    print("\n=== Summary ===")
    active = sum(
        1 for f in features
        if f.get("attributes", {}).get("HOA_STATUS") == "ACTIVE"
    )
    inactive = sum(
        1 for f in features
        if f.get("attributes", {}).get("HOA_STATUS") == "INACTIVE"
    )
    with_name = sum(
        1 for f in features
        if f.get("attributes", {}).get("HOA_NAME")
    )
    print(f"Total features: {len(features)}")
    print(f"Active HOAs:    {active}")
    print(f"Inactive HOAs:  {inactive}")
    print(f"With HOA name:  {with_name}")
    print(f"\nOutput files:")
    print(f"  CSV:     {csv_path}")
    print(f"  GeoJSON: {geojson_path}")


if __name__ == "__main__":
    main()
