#!/usr/bin/env python3
"""Generate per-county Serper query files for the NH keyword-Serper sweep.

Each county gets ~22 queries: a county-anchored set + one query per
top-3-population municipality so common HOA name patterns surface. Output
files match the names referenced by ``scripts/run_state_ingestion.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
QUERIES_DIR = ROOT / "state_scrapers/nh/queries"

# (county, [primary towns/cities, ordered by population/HOA-density])
COUNTIES: list[tuple[str, list[str]]] = [
    ("Hillsborough", ["Manchester", "Nashua", "Merrimack", "Bedford", "Hudson", "Goffstown", "Milford", "Amherst"]),
    ("Rockingham",   ["Salem", "Derry", "Londonderry", "Portsmouth", "Exeter", "Hampton", "Windham", "Stratham"]),
    ("Belknap",      ["Laconia", "Gilford", "Meredith", "Belmont", "Tilton", "Alton", "Sanbornton"]),
    ("Carroll",      ["Conway", "Wolfeboro", "Ossipee", "Tamworth", "Bartlett", "Jackson", "Tuftonboro"]),
    ("Merrimack",    ["Concord", "Bow", "Hooksett", "Pembroke", "Boscawen", "Henniker", "Hopkinton", "New London"]),
    ("Strafford",    ["Dover", "Rochester", "Somersworth", "Durham", "Barrington", "Farmington", "Lee"]),
    ("Grafton",      ["Lebanon", "Hanover", "Plymouth", "Littleton", "Bristol", "Enfield", "Lincoln", "Woodstock"]),
    ("Cheshire",     ["Keene", "Swanzey", "Jaffrey", "Walpole", "Winchester", "Rindge", "Hinsdale"]),
    ("Sullivan",     ["Claremont", "Newport", "Sunapee", "Charlestown", "Grantham", "Cornish"]),
    ("Coos",         ["Berlin", "Gorham", "Lancaster", "Whitefield", "Colebrook", "Jefferson", "Carroll"]),
]

# Anchor every query on the state to suppress out-of-state hits. NH has many
# town/county names that overlap other states (Bristol, Lincoln, Washington,
# Manchester, Hampton, Hudson, Sullivan, Salem, etc.).
STATE = "NH"
STATE_NAME = "New Hampshire"


def county_queries(county: str, towns: list[str]) -> list[str]:
    qs: list[str] = []
    qs.append(f"{county} County {STATE} HOA governing documents")
    qs.append(f"{county} County {STATE_NAME} homeowners association declaration")
    qs.append(f"{county} County {STATE} HOA bylaws and covenants")
    qs.append(f"{county} County {STATE} subdivision deed restrictions")
    qs.append(f"{county} County {STATE_NAME} condominium association declaration filetype:pdf")
    qs.append(f"{county} County {STATE} HOA covenants conditions restrictions filetype:pdf")
    qs.append(f"{county} County {STATE_NAME} master deed condominium filetype:pdf")
    qs.append(f"{county} County {STATE} HOA rules and regulations filetype:pdf")
    qs.append(f"{county} County {STATE_NAME} declaration of covenants filetype:pdf")
    qs.append(f"{county} County {STATE} community association documents")
    for t in towns[:5]:
        qs.append(f"{t} {STATE_NAME} HOA declaration covenants filetype:pdf")
        qs.append(f"{t} {STATE_NAME} condominium association documents filetype:pdf")
    return qs


def main() -> int:
    QUERIES_DIR.mkdir(parents=True, exist_ok=True)
    summary = []
    for county, towns in COUNTIES:
        slug = county.lower().replace(" ", "_")
        path = QUERIES_DIR / f"nh_{slug}_serper_queries.txt"
        qs = county_queries(county, towns)
        path.write_text("\n".join(qs) + "\n", encoding="utf-8")
        summary.append((path.name, len(qs)))
    for name, n in summary:
        print(f"{name}: {n} queries", file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
