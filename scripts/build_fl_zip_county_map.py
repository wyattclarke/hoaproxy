#!/usr/bin/env python3
"""
Download Census 2020 ZCTA-to-County relationship file and build
a FL ZIP5 -> county slug map saved to data/fl_zip_to_county.json.

County slug rules:
  - lowercase
  - hyphens for spaces
  - no "county" suffix
  - periods stripped (St. Johns -> st-johns)
"""

import csv
import json
import os
import urllib.request
import io
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(REPO_ROOT, "data", "fl_zip_to_county.json")

URL = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"

# Florida FIPS state code
FL_STATE_FIPS = "12"

# Census county FIPS -> county name for Florida (all 67 counties)
# Source: Census FIPS codes for Florida counties
FL_COUNTY_FIPS = {
    "001": "alachua",
    "003": "baker",
    "005": "bay",
    "007": "bradford",
    "009": "brevard",
    "011": "broward",
    "013": "calhoun",
    "015": "charlotte",
    "017": "citrus",
    "019": "clay",
    "021": "collier",
    "023": "columbia",
    "025": "miami-dade",
    "027": "desoto",
    "029": "dixie",
    "031": "duval",
    "033": "escambia",
    "035": "flagler",
    "037": "franklin",
    "039": "gadsden",
    "041": "gilchrist",
    "043": "glades",
    "045": "gulf",
    "047": "hamilton",
    "049": "hardee",
    "051": "hendry",
    "053": "hernando",
    "055": "highlands",
    "057": "hillsborough",
    "059": "holmes",
    "061": "indian-river",
    "063": "jackson",
    "065": "jefferson",
    "067": "lafayette",
    "069": "lake",
    "071": "lee",
    "073": "leon",
    "075": "levy",
    "077": "liberty",
    "079": "madison",
    "081": "manatee",
    "083": "marion",
    "085": "martin",
    "087": "monroe",
    "089": "nassau",
    "091": "okaloosa",
    "093": "okeechobee",
    "095": "orange",
    "097": "osceola",
    "099": "palm-beach",
    "101": "pasco",
    "103": "pinellas",
    "105": "polk",
    "107": "putnam",
    "109": "st-johns",
    "111": "st-lucie",
    "113": "santa-rosa",
    "115": "sarasota",
    "117": "seminole",
    "119": "sumter",
    "121": "suwannee",
    "123": "taylor",
    "125": "union",
    "127": "volusia",
    "129": "wakulla",
    "131": "walton",
    "133": "washington",
}


def download_crosswalk():
    print(f"Downloading Census ZCTA-to-County crosswalk from:\n  {URL}")
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
    print(f"  Downloaded {len(raw):,} bytes")
    return raw


def build_map(raw_text):
    """
    File is pipe-delimited with a header row.
    Key columns: GEOID_ZCTA5_20, GEOID_COUNTY_20, AREALAND_INTERSECTION
    GEOID_COUNTY_20 = 5-char FIPS (2 state + 3 county)
    """
    reader = csv.DictReader(io.StringIO(raw_text), delimiter="|")

    # zip5 -> {county_fips3: total_land_area}
    best = {}  # zip5 -> (max_land, county_slug)

    rows_processed = 0
    fl_rows = 0

    for row in reader:
        rows_processed += 1
        geoid_county = row.get("GEOID_COUNTY_20", "").strip()
        if len(geoid_county) != 5:
            continue
        state_fips = geoid_county[:2]
        if state_fips != FL_STATE_FIPS:
            continue

        county_fips3 = geoid_county[2:]
        slug = FL_COUNTY_FIPS.get(county_fips3)
        if slug is None:
            continue

        zip5 = row.get("GEOID_ZCTA5_20", "").strip().zfill(5)
        if not zip5 or len(zip5) != 5:
            continue

        try:
            land = float(row.get("AREALAND_INTERSECTION", 0) or 0)
        except ValueError:
            land = 0.0

        fl_rows += 1
        if zip5 not in best or land > best[zip5][0]:
            best[zip5] = (land, slug)

    print(f"  Total rows in file: {rows_processed:,}")
    print(f"  FL ZCTA-county pairs: {fl_rows:,}")

    result = {zip5: slug for zip5, (_, slug) in best.items()}
    return result


# Prefix-level fallback for FL USPS-only ZIP ranges not in Census ZCTA file.
# These are P.O. box ZIPs common in FL that lack ZCTA geographic coverage.
# Assignment based on USPS ZIP prefix allocation and city verification.
FL_PREFIX_FALLBACK = {
    # Miami-Dade (330xx, 331xx, 332xx are all Miami-Dade)
    "330": "miami-dade",
    "331": "miami-dade",
    "332": "miami-dade",
    # Broward (333xx)
    "333": "broward",
    # Palm Beach (334xx)
    "334": "palm-beach",
    # Hillsborough/Tampa area (335xx, 336xx)
    "335": "hillsborough",
    "336": "hillsborough",
    # Pinellas/St. Pete/Clearwater (337xx)
    "337": "pinellas",
    # Orange/Orlando (328xx)
    "328": "orange",
    # Seminole/Volusia/Brevard area (327xx)
    "327": "seminole",
    # Brevard/Indian River (329xx)
    "329": "brevard",
    # St. Johns/Clay/Nassau/Duval area (320xx)
    "320": "st-johns",
    # Okaloosa/Santa Rosa/Escambia (325xx)
    "325": "okaloosa",
    # Lee/Charlotte/Collier area (339xx)
    "339": "lee",
    # Sarasota (342xx)
    "342": "sarasota",
    # St. Lucie/Martin (349xx)
    "349": "st-lucie",
}


def apply_prefix_fallback(mapping):
    """Add entries for FL ZIPs 00000-99999 that are in FL prefix ranges but missing."""
    added = 0
    for z3, county in FL_PREFIX_FALLBACK.items():
        for suffix in range(0, 100):
            z5 = f"{z3}{suffix:02d}"
            if z5 not in mapping:
                mapping[z5] = county
                added += 1
    print(f"  Prefix fallback added: {added:,} ZIP entries")
    return mapping


def main():
    raw = download_crosswalk()
    mapping = build_map(raw)
    mapping = apply_prefix_fallback(mapping)
    print(f"  Total FL ZIPs in map: {len(mapping):,}")

    with open(OUTPUT_PATH, "w") as f:
        json.dump(mapping, f, sort_keys=True, indent=2)
    print(f"Saved -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
