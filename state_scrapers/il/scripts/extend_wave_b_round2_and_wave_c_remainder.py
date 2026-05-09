"""Append city-anchored round-2 queries to Wave B files; generate Wave C remainder.

Round-2 strategy for Wave B: Cook county hit its biggest yield (50 manifests)
when augmented with municipal anchors (Chicago, Evanston, Oak Park, Schaumburg,
...). Same pattern applies to downstate metros — `Rockford` is more
discoverable than `Winnebago County` for HOA governing docs.

Round-2 queries are appended to existing Wave B files (idempotent via sentinel).
Re-running the appended file is mostly a no-op for round-1 queries (Serper is
deterministic; probe dedups by URL); the new round-2 city queries surface
documents the county-name queries missed.

Wave C remainder: 14 long-tail counties (Vermilion, Knox, Effingham, Coles,
Marion, Boone, Grundy, Stephenson, Whiteside, Henry, Lee, Ogle, Jackson,
Franklin), generated with the same template as the initial 5 plus
city-anchored variants.
"""

from __future__ import annotations

import sys
from pathlib import Path


# Wave B counties with their primary municipalities (3–5 each) for round-2
# city-anchored variants. Sources: US Census 2020 incorporated-place population
# rankings within each county; HOA density tends to track municipality size.
WAVE_B_CITIES = {
    "winnebago": ["Rockford", "Loves Park", "Machesney Park"],
    "sangamon": ["Springfield", "Chatham"],
    "champaign": ["Champaign", "Urbana", "Savoy"],
    "peoria": ["Peoria", "East Peoria", "Bartonville", "Chillicothe"],
    "mclean": ["Bloomington", "Normal"],
    "st-clair": ["Belleville", "O'Fallon", "Fairview Heights"],
    "madison": ["Edwardsville", "Granite City", "Alton", "Glen Carbon"],
    "rock-island": ["Moline", "Rock Island", "East Moline"],
    "tazewell": ["Pekin", "Morton", "Washington"],
    "kankakee": ["Kankakee", "Bourbonnais", "Bradley"],
}

# Wave C remaining 14 counties + their primary city/town
WAVE_C_REMAINDER = [
    ("vermilion", "Vermilion", ["Danville"]),
    ("knox", "Knox", ["Galesburg"]),
    ("effingham", "Effingham", ["Effingham"]),
    ("coles", "Coles", ["Charleston", "Mattoon"]),
    ("marion", "Marion", ["Salem", "Centralia"]),
    ("boone", "Boone", ["Belvidere"]),
    ("grundy", "Grundy", ["Morris"]),
    ("stephenson", "Stephenson", ["Freeport"]),
    ("whiteside", "Whiteside", ["Sterling", "Rock Falls"]),
    ("henry", "Henry", ["Kewanee", "Geneseo"]),
    ("lee", "Lee", ["Dixon"]),
    ("ogle", "Ogle", ["Oregon", "Rochelle", "Byron"]),
    ("jackson", "Jackson", ["Carbondale", "Murphysboro"]),
    ("franklin", "Franklin", ["West Frankfort", "Benton"]),
]

EXCLUSIONS = " ".join([
    "-inurl:case", "-inurl:caselaw", "-inurl:opinion",
    "-inurl:news", "-inurl:articles", "-inurl:press",
    "-inurl:blog", "-inurl:learn-about-law",
    "-site:caselaw.findlaw.com", "-site:casetext.com",
    "-site:law.justia.com", "-site:scholar.google.com",
    "-site:illinoiscourts.gov", "-site:idfpr.illinois.gov",
    "-site:dicklerlaw.com", "-site:oflaherty-law.com",
    "-site:robbinsdimonte.com", "-site:sfbbg.com",
    "-site:broadshouldersmgt.com", "-site:associationvoice.com",
])

ROUND2_SENTINEL = "# round-2-city-anchored-v1"
WAVE_C_SENTINEL = "# wave-c-v1"


def city_anchored_queries(city: str, county_name: str) -> list[str]:
    """Build round-2 city-anchored queries for one county/city pair."""
    qs = [
        f'filetype:pdf "{city}, Illinois" "Declaration of Covenants" "Homeowners Association"',
        f'filetype:pdf "{city}, Illinois" "Declaration of Restrictions" "Homes Association"',
        f'filetype:pdf "{city}, Illinois" "Declaration of Condominium" "Association"',
        f'filetype:pdf "Articles of Incorporation" "{city}, Illinois" "Homeowners Association"',
        f'filetype:pdf "Master Deed" "{city}, Illinois" "Condominium"',
        f'filetype:pdf "Amendment to Declaration" "{city}, Illinois" "Homeowners Association"',
        f'filetype:pdf "{city}" "Illinois" "Property Owners Association" "Covenants"',
        f'"{city}" "Illinois" "homeowners association" bylaws',
        f'"{city}" "Illinois" "condominium association" declaration',
        f'"{city}" "Illinois" "governing documents" "homeowners association"',
        f'inurl:/wp-content/uploads/ "{city}" "Illinois" "homeowners association"',
        f'inurl:/wp-content/uploads/ "{city}" "Illinois" "condominium association"',
    ]
    return [f"{q} {EXCLUSIONS}" for q in qs]


def append_round2_to_wave_b() -> int:
    queries_dir = Path(__file__).resolve().parent.parent / "queries"
    appended = 0
    skipped = 0
    for slug, cities in WAVE_B_CITIES.items():
        path = queries_dir / f"il_{slug}_serper_queries.txt"
        if not path.exists():
            print(f"  MISSING: {path}", file=sys.stderr)
            continue
        text = path.read_text()
        if ROUND2_SENTINEL in text:
            print(f"  already-extended  {path.name}")
            skipped += 1
            continue
        new_lines = [text.rstrip(), "", ROUND2_SENTINEL]
        for city in cities:
            new_lines.append(f"# city anchor: {city}")
            new_lines.extend(city_anchored_queries(city, slug))
        path.write_text("\n".join(new_lines) + "\n")
        print(f"  appended round-2  {path.name} (+{sum(len(city_anchored_queries(c, slug)) for c in cities)} queries)")
        appended += 1
    print(f"\nWave B round-2: appended={appended}  skipped={skipped}")
    return appended


def write_wave_c_remainder() -> int:
    queries_dir = Path(__file__).resolve().parent.parent / "queries"
    written = 0
    skipped = 0
    for slug, county_name, cities in WAVE_C_REMAINDER:
        path = queries_dir / f"il_{slug}_serper_queries.txt"
        if path.exists() and (WAVE_C_SENTINEL in path.read_text() or ROUND2_SENTINEL in path.read_text()):
            print(f"  already-generated  {path.name}")
            skipped += 1
            continue
        # Standard county queries (same as initial 5 Wave C counties)
        county_queries = [
            f'filetype:pdf "{county_name} County" "Illinois" "Declaration of Covenants" "Homeowners Association"',
            f'filetype:pdf "{county_name} County, Illinois" "Declaration of Restrictions" "Homes Association"',
            f'filetype:pdf "{county_name} County" "Illinois" "Declaration of Condominium" "Association"',
            f'filetype:pdf "Register of Deeds" "{county_name} County, Illinois" "Homeowners Association"',
            f'filetype:pdf "Articles of Incorporation" "{county_name} County, Illinois" "Homeowners Association"',
            f'filetype:pdf "Amendment to Declaration" "{county_name} County, Illinois" "Homeowners Association"',
            f'filetype:pdf "Restated Bylaws" "Illinois" "{county_name}" "Homeowners Association"',
            f'filetype:pdf "Supplemental Declaration" "Illinois" "{county_name} County"',
            f'filetype:pdf "{county_name} County" "Illinois" "Property Owners Association" "Covenants"',
            f'filetype:pdf "{county_name} County" "Illinois" "Master Deed" "Condominium"',
            f'"{county_name} County" "Illinois" "HOA documents" "bylaws"',
            f'"{county_name} County" "Illinois" "homes association" documents',
            f'"{county_name} County" "Illinois" "governing documents" "homeowners association"',
            f'"{county_name} County" "Illinois" "condominium association" "declaration"',
            f'site:.gov/DocumentCenter/View "Illinois" "{county_name}" "Homeowners Association" "Declaration"',
            f'site:.gov/AgendaCenter/ViewFile "Illinois" "{county_name}" "Homeowners Association"',
            f'inurl:/wp-content/uploads/ "Illinois" "{county_name}" "homeowners association" bylaws',
            f'inurl:/wp-content/uploads/ "Illinois" "{county_name}" "condominium association" declaration',
        ]
        lines = [f"{q} {EXCLUSIONS}" for q in county_queries]
        # City-anchored variants
        for city in cities:
            lines.append(f"# city anchor: {city}")
            lines.extend(city_anchored_queries(city, slug))
        lines.append(WAVE_C_SENTINEL)
        path.write_text("\n".join(lines) + "\n")
        print(f"  wrote              {path.name} ({len(lines)} lines, cities={cities})")
        written += 1
    print(f"\nWave C remainder: wrote={written}  skipped={skipped}")
    return written


def main() -> int:
    append_round2_to_wave_b()
    write_wave_c_remainder()
    return 0


if __name__ == "__main__":
    sys.exit(main())
