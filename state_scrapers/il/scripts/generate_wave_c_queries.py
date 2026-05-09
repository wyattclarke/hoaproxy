"""Generate Wave-C per-county query files using the Wave-B Winnebago template.

Wave-C IL counties are long-tail (smaller populations, fewer HOAs). Same
query shapes as Wave-B (filetype:pdf + governing-doc keyword + county
anchor + IL-specific exclusions) but tighter `--max-queries-per-county`
budget at run time. The brief's kill rule (<3 productive leads → stop
that county) is enforced by the natural Serper yield, not by this script.
"""

from __future__ import annotations

import sys
from pathlib import Path

WAVE_C_COUNTIES = [
    # Highest-priority first — university towns and metros tend to produce
    # more recorded HOAs than agricultural counties.
    ("dekalb", "DeKalb"),       # NIU / Sycamore
    ("lasalle", "LaSalle"),     # Ottawa / Streator
    ("macon", "Macon"),         # Decatur
    ("williamson", "Williamson"),  # Marion / Carbondale region
    ("adams", "Adams"),         # Quincy
]


# Generic exclusions identical to Wave-B's tighten step.
GENERIC_EXCLUSIONS = " ".join([
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


# Same query shapes as Wave-B per-county files.
QUERY_TEMPLATES = [
    'filetype:pdf "{C} County" "Illinois" "Declaration of Covenants" "Homeowners Association"',
    'filetype:pdf "{C} County, Illinois" "Declaration of Restrictions" "Homes Association"',
    'filetype:pdf "{C} County" "Illinois" "Declaration of Condominium" "Association"',
    'filetype:pdf "Register of Deeds" "{C} County, Illinois" "Homeowners Association"',
    'filetype:pdf "Articles of Incorporation" "{C} County, Illinois" "Homeowners Association"',
    'filetype:pdf "Amendment to Declaration" "{C} County, Illinois" "Homeowners Association"',
    'filetype:pdf "Restated Bylaws" "Illinois" "{C}" "Homeowners Association"',
    'filetype:pdf "Supplemental Declaration" "Illinois" "{C} County"',
    'filetype:pdf "{C} County" "Illinois" "Property Owners Association" "Covenants"',
    'filetype:pdf "{C} County" "Illinois" "Master Deed" "Condominium"',
    '"{C} County" "Illinois" "HOA documents" "bylaws"',
    '"{C} County" "Illinois" "homes association" documents',
    '"{C} County" "Illinois" "governing documents" "homeowners association"',
    '"{C} County" "Illinois" "condominium association" "declaration"',
    'site:.gov/DocumentCenter/View "Illinois" "{C}" "Homeowners Association" "Declaration"',
    'site:.gov/AgendaCenter/ViewFile "Illinois" "{C}" "Homeowners Association"',
    'inurl:/wp-content/uploads/ "Illinois" "{C}" "homeowners association" bylaws',
    'inurl:/wp-content/uploads/ "Illinois" "{C}" "condominium association" declaration',
]


SENTINEL = "# wave-c-v1"


def main() -> int:
    queries_dir = Path(__file__).resolve().parent.parent / "queries"
    written = 0
    skipped = 0
    for slug, county_name in WAVE_C_COUNTIES:
        path = queries_dir / f"il_{slug}_serper_queries.txt"
        if path.exists() and SENTINEL in path.read_text():
            print(f"  already-generated  {path.name}")
            skipped += 1
            continue
        lines = []
        for tmpl in QUERY_TEMPLATES:
            q = tmpl.format(C=county_name)
            lines.append(f"{q} {GENERIC_EXCLUSIONS}")
        lines.append(SENTINEL)
        path.write_text("\n".join(lines) + "\n")
        print(f"  wrote              {path.name} ({len(lines)} lines)")
        written += 1
    print(f"\nwrote: {written}  already-generated: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
