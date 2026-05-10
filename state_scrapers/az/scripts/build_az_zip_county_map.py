#!/usr/bin/env python3
"""
Build an AZ ZIP5 -> county slug map from the local Census 2020 ZCTA<->county
crosswalk (data/gazetteer/2020_Gaz_zcta_national.txt is the gazetteer header
file, not the relationship file — we fetch the relationship file).

Output: data/az_zip_to_county.json

County slug rules: lowercase, hyphens for spaces, no "county" suffix,
periods stripped (St. Johns has no analog in AZ; we keep "santa-cruz",
"la-paz").
"""

from __future__ import annotations

import csv
import io
import json
import os
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OUTPUT_PATH = os.path.join(REPO_ROOT, "data", "az_zip_to_county.json")

URL = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"

AZ_STATE_FIPS = "04"

AZ_COUNTY_FIPS = {
    "001": "apache",
    "003": "cochise",
    "005": "coconino",
    "007": "gila",
    "009": "graham",
    "011": "greenlee",
    "012": "la-paz",
    "013": "maricopa",
    "015": "mohave",
    "017": "navajo",
    "019": "pima",
    "021": "pinal",
    "023": "santa-cruz",
    "025": "yavapai",
    "027": "yuma",
}

# Prefix-level fallback for AZ ZIP ranges not present in Census ZCTA file
# (PO-box-only ZIPs lack ZCTA geographic coverage). Assignments by USPS
# ZIP prefix → dominant county. AZ ZIP space is largely 850-865.
AZ_PREFIX_FALLBACK = {
    # Phoenix metro (Maricopa) — 850, 852, 853
    "850": "maricopa",
    "852": "maricopa",
    "853": "maricopa",
    # Tucson metro (Pima) — 856, 857
    "856": "pima",
    "857": "pima",
    # Yuma — 853x lower / 854x — but 853 is mainly Maricopa, 854 isn't standard
    # Outlying — 854xx is unused; 859xx northeast (Show Low / Holbrook = Navajo)
    "859": "navajo",
    # Flagstaff / Coconino — 860, 861, 863
    "860": "coconino",
    "861": "coconino",
    "863": "coconino",
    # Prescott / Yavapai — 863 overlaps; 864xx
    "864": "mohave",  # Kingman, Lake Havasu, Bullhead = Mohave (864/865)
    "865": "mohave",
    # Yuma — 853 is Maricopa; Yuma is 853xx subset already covered;
    # actual Yuma county dominant: 853x lowest tier. We rely on Census
    # crosswalk for the precise split.
}


def download_crosswalk() -> str:
    print(f"Downloading Census ZCTA-to-County crosswalk from:\n  {URL}")
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
    print(f"  Downloaded {len(raw):,} bytes")
    return raw


def build_map(raw_text: str) -> dict[str, str]:
    reader = csv.DictReader(io.StringIO(raw_text), delimiter="|")
    best: dict[str, tuple[float, str]] = {}
    rows_processed = 0
    az_rows = 0

    for row in reader:
        rows_processed += 1
        geoid_county = row.get("GEOID_COUNTY_20", "").strip()
        if len(geoid_county) != 5:
            continue
        if geoid_county[:2] != AZ_STATE_FIPS:
            continue
        county_fips3 = geoid_county[2:]
        slug = AZ_COUNTY_FIPS.get(county_fips3)
        if slug is None:
            continue
        zip5 = row.get("GEOID_ZCTA5_20", "").strip().zfill(5)
        if not zip5 or len(zip5) != 5:
            continue
        try:
            land = float(row.get("AREALAND_INTERSECTION", 0) or 0)
        except ValueError:
            land = 0.0
        az_rows += 1
        if zip5 not in best or land > best[zip5][0]:
            best[zip5] = (land, slug)

    print(f"  Total rows: {rows_processed:,}")
    print(f"  AZ ZCTA-county pairs: {az_rows:,}")
    return {z: s for z, (_, s) in best.items()}


def apply_prefix_fallback(mapping: dict[str, str]) -> dict[str, str]:
    added = 0
    for z3, county in AZ_PREFIX_FALLBACK.items():
        for suffix in range(0, 100):
            z5 = f"{z3}{suffix:02d}"
            if z5 not in mapping:
                mapping[z5] = county
                added += 1
    print(f"  Prefix fallback added: {added:,} ZIP entries")
    return mapping


def main() -> None:
    raw = download_crosswalk()
    mapping = build_map(raw)
    mapping = apply_prefix_fallback(mapping)
    print(f"  Total AZ ZIPs: {len(mapping):,}")
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(mapping, f, sort_keys=True, indent=2)
    print(f"Saved -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
