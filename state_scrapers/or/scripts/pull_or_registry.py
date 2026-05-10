#!/usr/bin/env python3
"""Pull OR HOA/condo/coop seed leads from the Oregon Secretary of State
"Active Nonprofit Corporations" Socrata bulk CSV (cached at
``data/or_active_nonprofit_corporations.csv``).

Source provenance
-----------------
Oregon SoS Corporation Division publishes the active nonprofit-corporation
universe as a Socrata dataset on ``data.oregon.gov`` (id ``8kyv-b2kw``).
The CSV contains roughly 180k rows (multiple rows per entity, one per
"Associated Name Type" — MAILING ADDRESS / PRESIDENT / REGISTERED AGENT /
etc). Free, no auth, no captcha. Fetch::

    curl -L -o data/or_active_nonprofit_corporations.csv \\
        "https://data.oregon.gov/api/views/8kyv-b2kw/rows.csv?accessType=DOWNLOAD"

Oregon HOAs/condos universally file as nonprofit corporations under
ORS Chapter 65 (mutual benefit) — single-family HOAs, condo unit
owners' associations, and the (rare) housing co-op all show up here.
Filtering rules:

1. Entity Type contains "NONPROFIT" (drops business / co-op / pro-corp).
2. Nonprofit Type is "MUTUAL BENEFIT WITH MEMBERS" or "MUTUAL BENEFIT
   WITHOUT MEMBERS" (drops RELIGIOUS / PUBLIC BENEFIT — churches, charities).
3. Business name matches HOA-shaped patterns (condominium, owners
   association, homeowners, community association, etc.).
4. Reject patterns common to non-HOA mutual-benefit nonprofits
   (clubs, leagues, water districts that aren't HOAs, etc.).
5. Pick the MAILING ADDRESS row (or PRINCIPAL PLACE OF BUSINESS as
   fallback) for the entity's location.

Output: ``state_scrapers/or/leads/or_registry_seed.jsonl``
Shape:  ``{"name", "state": "OR", "city", "postal_code", "metadata_type",
            "source", "source_url"}``
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC_CSV = ROOT / "data" / "or_active_nonprofit_corporations.csv"
OUT = ROOT / "state_scrapers" / "or" / "leads" / "or_registry_seed.jsonl"
SOURCE_URL = "https://data.oregon.gov/Business/Active-Nonprofit-Corporations/8kyv-b2kw"
SOURCE_LABEL = "or_sos_active_nonprofit_corporations"

# OR HOA names look like NY/CA's. CONDOMINIUM, OWNERS ASSOC, HOMEOWNERS,
# COMMUNITY ASSOC, TOWNHOUSE/TOWNHOME, PROPERTY OWNERS, etc.
HOA_NAME_RE = re.compile(
    r"\b("
    r"CONDOMINIUM(S)?"
    r"|CONDO(S)?"
    r"|HOMEOWNERS?\s+ASSOC(IATION)?S?"
    r"|HOME\s+OWNERS?\s+ASSOC(IATION)?S?"
    r"|HOA"
    r"|UNIT\s+OWNERS?\s+ASSOC(IATION)?S?"
    r"|OWNERS?\s+ASSOC(IATION)?S?"
    r"|COMMUNITY\s+ASSOC(IATION)?S?"
    r"|PROPERTY\s+OWNERS?\s+ASSOC(IATION)?S?"
    r"|TOWNHOUSE\s+ASSOC(IATION)?S?"
    r"|TOWN\s*HOUSE\s+ASSOC(IATION)?S?"
    r"|TOWNHOMES?\s+ASSOC(IATION)?S?"
    r"|VILLAGE\s+ASSOC(IATION)?S?"
    r"|PLANNED\s+COMMUNITY"
    r"|MASTER\s+ASSOC(IATION)?S?"
    r"|RESIDENTIAL\s+ASSOC(IATION)?S?"
    r"|HOUSING\s+COOP(ERATIVE)?"
    r"|HOUSING\s+CO\-?OP(ERATIVE)?"
    r"|COOPERATIVE\s+APART(MENT)?S?"
    r")\b",
    re.IGNORECASE,
)

# Reject patterns: non-HOA mutual-benefit nonprofits with HOA-ish names.
REJECT_RE = re.compile(
    r"\b("
    r"CHURCH|MINISTRY|TEMPLE|SYNAGOGUE|MOSQUE|CONGREGATION|CATHEDRAL"
    r"|PARENT\s+TEACHER|PTA|PTO|ALUMNI"
    r"|VETERAN|VFW|AMERICAN\s+LEGION"
    r"|BOOSTER|ATHLETIC|YACHT\s+CLUB|GOLF\s+CLUB"
    r"|FRATERN|SORORITY|MASONIC|LODGE|ROTARY|KIWANIS|LIONS\s+CLUB"
    r"|MEDICAL\s+ASSOC|DENTAL\s+ASSOC|HOSPITAL"
    r"|BAR\s+ASSOC|CHAMBER\s+OF"
    r"|MERCHANTS?\s+ASSOC|LANDLORDS?\s+ASSOC|REALTORS?\s+ASSOC|REALTY\s+ASSOC"
    r"|TENANTS?\s+UNION|TENANTS?\s+ASSOC"
    r"|MUSEUM|FOUNDATION|SCHOLARSHIP"
    r"|WATER\s+DISTRICT|IRRIGATION\s+DISTRICT|WATER\s+SUPPLY"
    r"|FIRE\s+DEPT|FIRE\s+DEPARTMENT"
    r"|GRANGE|FFA|4\-?H"
    r"|HUMANE\s+SOCIETY|ANIMAL\s+RESCUE"
    r"|PARK\s+&\s+RECREATION|PARKS?\s+AND\s+RECREATION"
    r"|SPORTSMEN|HUNTING|FISHING\s+CLUB"
    r"|BUSINESS\s+ASSOC"
    r")\b",
    re.IGNORECASE,
)

ZIP5_RE = re.compile(r"^(\d{5})")


def normalize_zip(z: str) -> str:
    z = (z or "").strip()
    if not z:
        return ""
    m = ZIP5_RE.match(z)
    return m.group(1) if m else ""


def classify(name: str) -> str:
    n = name.upper()
    if re.search(r"\bCONDOMINIUM(S)?\b|\bCONDO(S)?\b|\bUNIT\s+OWNERS?\b|\bC\.?O\.?A\.?\b", n):
        return "condo"
    if re.search(
        r"\bCOOPERATIVE\b|\bCO\-?OP\b|\bMUTUAL\s+HOUSING\b|\bHOUSING\s+CORP\b",
        n,
    ):
        return "coop"
    return "hoa"


# Address-row priority for picking the canonical city/zip per entity.
ADDR_TYPE_PRIORITY = {
    "MAILING ADDRESS": 0,
    "PRINCIPAL PLACE OF BUSINESS": 1,
    "REGISTERED AGENT": 2,
    "PRESIDENT": 3,
}


def main() -> int:
    if not SRC_CSV.exists():
        print(f"ERROR: missing {SRC_CSV}", file=sys.stderr)
        print(
            "Fetch with: curl -L -o data/or_active_nonprofit_corporations.csv "
            "\"https://data.oregon.gov/api/views/8kyv-b2kw/rows.csv?accessType=DOWNLOAD\"",
            file=sys.stderr,
        )
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    stats: Counter[str] = Counter()

    # First pass: collapse rows by registry number, picking best address row.
    entities: dict[str, dict] = {}

    with open(SRC_CSV, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["total_rows"] += 1
            reg = (row.get("Registry Number") or "").strip()
            if not reg:
                continue
            etype = (row.get("Entity Type") or "").strip().upper()
            if "NONPROFIT" not in etype:
                continue
            nptype = (row.get("Nonprofit Type") or "").strip().upper()
            if "MUTUAL BENEFIT" not in nptype:
                continue
            name = (row.get("Business Name") or "").strip()
            if not name:
                continue
            addr_type = (row.get("Associated Name Type") or "").strip().upper()
            city = (row.get("City") or "").strip()
            state = (row.get("State") or "").strip().upper()
            zip5 = normalize_zip(row.get("Zip Code") or "")

            cur = entities.get(reg)
            if cur is None:
                entities[reg] = {
                    "name": name,
                    "best_priority": None,
                    "city": "",
                    "zip5": "",
                    "addr_state": "",
                }
                cur = entities[reg]
            # Always keep the first non-empty business name; entities table is keyed
            # by registry number, so all rows share a name.
            prio = ADDR_TYPE_PRIORITY.get(addr_type)
            if prio is not None and (cur["best_priority"] is None or prio < cur["best_priority"]):
                cur["best_priority"] = prio
                cur["city"] = city
                cur["zip5"] = zip5
                cur["addr_state"] = state

    stats["entities"] = len(entities)
    seen_names: set[str] = set()

    with open(OUT, "w") as out:
        for reg, e in entities.items():
            stats["entity_pass1"] += 1
            name = e["name"]
            if not HOA_NAME_RE.search(name):
                stats["skip_no_hoa_keyword"] += 1
                continue
            if REJECT_RE.search(name):
                stats["skip_rejected_pattern"] += 1
                continue
            key = re.sub(r"\s+", " ", name.upper())
            if key in seen_names:
                stats["skip_dup_name"] += 1
                continue
            seen_names.add(key)
            metadata_type = classify(name)
            # Prefer Oregon-located addresses; if the only address row is out
            # of state, leave city/zip empty rather than mislead.
            city = e["city"] if e["addr_state"] in ("OR", "") else ""
            zip5 = e["zip5"] if e["addr_state"] in ("OR", "") else ""

            lead = {
                "name": name,
                "state": "OR",
                "city": city,
                "postal_code": zip5,
                "metadata_type": metadata_type,
                "source": SOURCE_LABEL,
                "source_url": SOURCE_URL,
            }
            out.write(json.dumps(lead, sort_keys=True) + "\n")
            stats["written"] += 1
            if metadata_type == "condo":
                stats["written_condo"] += 1
            elif metadata_type == "coop":
                stats["written_coop"] += 1
            else:
                stats["written_hoa"] += 1

    print("=== OR registry pull stats ===", file=sys.stderr)
    for k, v in stats.most_common():
        print(f"  {k}: {v:,}", file=sys.stderr)
    print(f"\noutput -> {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
