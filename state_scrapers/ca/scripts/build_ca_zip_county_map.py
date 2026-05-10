#!/usr/bin/env python3
"""Build CA ZIP5 -> county slug map from Census 2020 ZCTA-County crosswalk.

Mirrors state_scrapers/fl/scripts/build_fl_zip_county_map.py for CA (FIPS=06).
Output: data/ca_zip_to_county.json
"""
from __future__ import annotations

import csv
import io
import json
import os
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OUTPUT_PATH = os.path.join(REPO_ROOT, "data", "ca_zip_to_county.json")

URL = "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt"

CA_STATE_FIPS = "06"

# All 58 CA counties keyed by 3-digit FIPS suffix; slug = lowercase, no
# "county" suffix, hyphens for spaces, periods stripped.
CA_COUNTY_FIPS = {
    "001": "alameda",
    "003": "alpine",
    "005": "amador",
    "007": "butte",
    "009": "calaveras",
    "011": "colusa",
    "013": "contra-costa",
    "015": "del-norte",
    "017": "el-dorado",
    "019": "fresno",
    "021": "glenn",
    "023": "humboldt",
    "025": "imperial",
    "027": "inyo",
    "029": "kern",
    "031": "kings",
    "033": "lake",
    "035": "lassen",
    "037": "los-angeles",
    "039": "madera",
    "041": "marin",
    "043": "mariposa",
    "045": "mendocino",
    "047": "merced",
    "049": "modoc",
    "051": "mono",
    "053": "monterey",
    "055": "napa",
    "057": "nevada",
    "059": "orange",
    "061": "placer",
    "063": "plumas",
    "065": "riverside",
    "067": "sacramento",
    "069": "san-benito",
    "071": "san-bernardino",
    "073": "san-diego",
    "075": "san-francisco",
    "077": "san-joaquin",
    "079": "san-luis-obispo",
    "081": "san-mateo",
    "083": "santa-barbara",
    "085": "santa-clara",
    "087": "santa-cruz",
    "089": "shasta",
    "091": "sierra",
    "093": "siskiyou",
    "095": "solano",
    "097": "sonoma",
    "099": "stanislaus",
    "101": "sutter",
    "103": "tehama",
    "105": "trinity",
    "107": "tulare",
    "109": "tuolumne",
    "111": "ventura",
    "113": "yolo",
    "115": "yuba",
}


def download_crosswalk() -> str:
    print(f"Downloading Census ZCTA-to-County crosswalk from:\n  {URL}")
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw = resp.read().decode("utf-8")
    print(f"  Downloaded {len(raw):,} bytes")
    return raw


def build_map(raw_text: str) -> dict:
    reader = csv.DictReader(io.StringIO(raw_text), delimiter="|")
    best: dict[str, tuple[float, str]] = {}
    rows_processed = 0
    ca_rows = 0
    for row in reader:
        rows_processed += 1
        geoid_county = row.get("GEOID_COUNTY_20", "").strip()
        if len(geoid_county) != 5:
            continue
        if geoid_county[:2] != CA_STATE_FIPS:
            continue
        slug = CA_COUNTY_FIPS.get(geoid_county[2:])
        if slug is None:
            continue
        zip5 = row.get("GEOID_ZCTA5_20", "").strip().zfill(5)
        if not zip5 or len(zip5) != 5:
            continue
        try:
            land = float(row.get("AREALAND_INTERSECTION", 0) or 0)
        except ValueError:
            land = 0.0
        ca_rows += 1
        if zip5 not in best or land > best[zip5][0]:
            best[zip5] = (land, slug)
    print(f"  Total rows in file: {rows_processed:,}")
    print(f"  CA ZCTA-county pairs: {ca_rows:,}")
    return {z: slug for z, (_, slug) in best.items()}


# CA USPS ZIP prefix fallback for P.O. box-only ZIPs not in Census ZCTA.
# Coverage approximation; the prefix-3 ranges are unique to one CA county
# in the vast majority of cases.
CA_PREFIX_FALLBACK = {
    # LA County (900xx-905xx, 906-908 partial overlap; Long Beach 907-908)
    "900": "los-angeles", "901": "los-angeles", "902": "los-angeles",
    "903": "los-angeles", "904": "los-angeles", "905": "los-angeles",
    "906": "los-angeles", "907": "los-angeles", "908": "los-angeles",
    "910": "los-angeles", "911": "los-angeles", "912": "los-angeles",
    "913": "los-angeles", "914": "los-angeles", "915": "los-angeles",
    "916": "los-angeles", "917": "los-angeles", "918": "los-angeles",
    # Orange (926-928)
    "926": "orange", "927": "orange", "928": "orange",
    # San Diego (919, 920-921, 922)
    "919": "san-diego", "920": "san-diego", "921": "san-diego",
    "922": "san-diego",
    # Riverside (922 partial, 925)
    "925": "riverside",
    # San Bernardino (923, 924)
    "923": "san-bernardino", "924": "san-bernardino",
    # Bay Area: SF 941, San Mateo 944, Santa Clara 950-951,
    # Alameda 945-947, Contra Costa 945-948 partial
    "940": "san-mateo", "941": "san-francisco", "942": "san-francisco",
    "943": "san-mateo", "944": "san-mateo",
    "945": "alameda", "946": "alameda", "947": "alameda",
    "948": "contra-costa", "949": "marin",
    "950": "santa-clara", "951": "santa-clara",
    # Santa Cruz (950 partial, 95066+)
    # Monterey (939)
    "939": "monterey",
    # Sacramento (956-958)
    "956": "sacramento", "957": "sacramento", "958": "sacramento",
    # Stockton/San Joaquin (952)
    "952": "san-joaquin",
    # Modesto/Stanislaus (953)
    "953": "stanislaus",
    # Fresno (936-937)
    "936": "fresno", "937": "fresno",
    # Bakersfield/Kern (932-933)
    "932": "kern", "933": "kern",
    # Santa Barbara (931)
    "931": "santa-barbara",
    # San Luis Obispo (934)
    "934": "san-luis-obispo",
    # Ventura (930)
    "930": "ventura",
    # Sonoma (954)
    "954": "sonoma",
    # Napa (945 overlap, 944 overlap; using 949 marin for inland Napa is wrong)
    # Solano (945 overlap; 945-948 already mapped to alameda/contra-costa)
    # North coast: Mendocino 954+ overlap; Humboldt 955; Del Norte 955
    "955": "humboldt",
    # Far north: Shasta/Tehama (960)
    "960": "shasta",
    # Eureka/Humboldt (overlap)
    # Tahoe area: El Dorado (957 overlap), Placer (956 overlap)
    # Yuba/Sutter (959)
    "959": "yuba",
}


def apply_prefix_fallback(mapping: dict) -> dict:
    added = 0
    for z3, county in CA_PREFIX_FALLBACK.items():
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
    print(f"  Total CA ZIPs in map: {len(mapping):,}")
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(mapping, f, sort_keys=True, indent=2)
    print(f"Saved -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
