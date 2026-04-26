#!/usr/bin/env python3
"""
Scrape Alexandria, VA HOA and condominium boundaries from the Community Association Viewer.

Source:
  https://www.alexandriava.gov/gis/interactive-maps
  https://maps.alexandriava.gov/arcgis/rest/services/alxCommonOwnershipWm2/MapServer/0

Outputs:
  data/alexandria_va/common_ownership.geojson   - raw parcel-level features
  data/alexandria_va/import.json                - deduped association-level bulk-import data

The live ArcGIS layer contains parcel/common-area polygons. This script groups those
features into one record per named HOA/condominium and merges their boundaries into a
single Polygon or MultiPolygon geometry for import.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from base import compute_centroid, title_case_name, write_import_file

SERVICE_URL = (
    "https://maps.alexandriava.gov/arcgis/rest/services/"
    "alxCommonOwnershipWm2/MapServer/0/query"
)

OUT_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "alexandria_va"
SOURCE = "alexandria_va_common_ownership"
COMMUNITY_TYPE_NAMES = {
    1: "Condo Owners",
    2: "Home Owners",
}


def fetch_page(offset: int = 0, limit: int = 1000) -> dict:
    params = {
        "where": "COMMUNITYTYPE in (1,2)",
        "outFields": ",".join(
            [
                "OBJECTID",
                "COMMUNITYNAME",
                "COMMUNITYTYPE",
                "PROPERTYADD",
                "OWNERNAME",
                "OWNERADD",
                "OWNERCITY",
                "OWNERSTATE",
                "OWNERZIP",
                "WEBSITE",
                "LANDCODE",
                "LABEL",
                "PID",
            ]
        ),
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
        "resultOffset": str(offset),
        "resultRecordCount": str(limit),
    }
    url = f"{SERVICE_URL}?{urlencode(params)}"
    print(f"Fetching offset={offset}...")
    req = Request(url, headers={"User-Agent": "HOAproxy-scraper/1.0"})
    with urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def fetch_all_features() -> list[dict]:
    features: list[dict] = []
    offset = 0
    limit = 1000
    while True:
        payload = fetch_page(offset=offset, limit=limit)
        batch = payload.get("features", [])
        features.extend(batch)
        if not payload.get("exceededTransferLimit"):
            break
        offset += len(batch)
    return features


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split()).strip()
    return cleaned or None


def clean_website(value: str | None) -> str | None:
    website = clean_text(value)
    if not website:
        return None
    if not website.startswith(("http://", "https://")):
        website = f"https://{website}"
    return website


def clean_name(label: str | None, fallback: str | None) -> str | None:
    raw = clean_text(label) or clean_text(fallback)
    if not raw:
        return None
    if raw.isupper():
        return title_case_name(raw)
    return raw


def normalize_group_key(name: str, community_type: int | None) -> tuple[str, int | None]:
    return (" ".join(name.lower().split()), community_type)


def merge_boundaries(geometries: list[dict]) -> dict | None:
    polygons: list[list] = []
    for geometry in geometries:
        if not geometry:
            continue
        geom_type = geometry.get("type")
        coords = geometry.get("coordinates")
        if not coords:
            continue
        if geom_type == "Polygon":
            polygons.append(coords)
        elif geom_type == "MultiPolygon":
            polygons.extend(coords)
    if not polygons:
        return None
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def group_records(features: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, int | None], list[dict]] = defaultdict(list)
    for feature in features:
        props = feature.get("properties", {})
        community_type = props.get("COMMUNITYTYPE")
        name = clean_name(props.get("LABEL"), props.get("COMMUNITYNAME"))
        if not name:
            continue
        grouped[normalize_group_key(name, community_type)].append(feature)

    records: list[dict] = []
    for (_group_name, community_type), group_features in grouped.items():
        props_list = [feature.get("properties", {}) for feature in group_features]
        name_counter = Counter(
            clean_name(props.get("LABEL"), props.get("COMMUNITYNAME"))
            for props in props_list
            if clean_name(props.get("LABEL"), props.get("COMMUNITYNAME"))
        )
        name = name_counter.most_common(1)[0][0]
        websites = [
            clean_website(props.get("WEBSITE"))
            for props in props_list
            if clean_website(props.get("WEBSITE"))
        ]
        boundary = merge_boundaries([feature.get("geometry") for feature in group_features])
        centroid = compute_centroid(boundary) if boundary else None

        records.append(
            {
                "name": name,
                "display_name": None,
                "website_url": websites[0] if websites else None,
                "city": "Alexandria",
                "state": "VA",
                "country": "US",
                "latitude": centroid[0] if centroid else None,
                "longitude": centroid[1] if centroid else None,
                "boundary_geojson": boundary,
                "source": f"{SOURCE}:{COMMUNITY_TYPE_NAMES.get(community_type, 'Unknown')}",
            }
        )

    return sorted(records, key=lambda record: record["name"].lower())


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    features = fetch_all_features()
    print(f"Fetched {len(features)} parcel/common-area features")

    raw_geojson = {"type": "FeatureCollection", "features": features}
    raw_path = OUT_DIR / "common_ownership.geojson"
    with open(raw_path, "w") as f:
        json.dump(raw_geojson, f)
    print(f"Saved raw features -> {raw_path}")

    records = group_records(features)
    import_path = OUT_DIR / "import.json"
    write_import_file(records, source=SOURCE, output_path=import_path)

    condo_count = sum(1 for record in records if record.get("source", "").endswith("Condo Owners"))
    hoa_count = sum(1 for record in records if record.get("source", "").endswith("Home Owners"))
    print("\n=== Summary ===")
    print(f"Association records: {len(records)}")
    print(f"Condominiums:       {condo_count}")
    print(f"HOAs:               {hoa_count}")


if __name__ == "__main__":
    main()
