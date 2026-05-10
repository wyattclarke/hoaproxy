#!/usr/bin/env python3
"""Pull NY HOA/condo/coop seed leads from the NY DOS Active Corporations
public bulk dump (already cached at ``data/ny_active_corporations.csv``).

Source provenance
-----------------
New York Department of State publishes the full active-corporation universe
as a Socrata dataset on ``data.ny.gov`` (id ``n9v6-gdp6``). The CSV contains
roughly 626k active entities with DOS ID, current name, county, jurisdiction,
entity type, and addresses for the registered agent / CEO / location.
Free, no auth, no captcha. Fetch::

    curl -L -o data/ny_active_corporations.csv \\
        "https://data.ny.gov/api/views/n9v6-gdp6/rows.csv?accessType=DOWNLOAD"

NY HOA/condo/coop universe is heavily condo + housing-coop weighted (NYC
Article XI HDFCs = coops; Mitchell-Lama coops; condominium boards usually
file as not-for-profit corps). We filter on:

1. Entity type in the not-for-profit family (skips LLCs, business corps)
2. Name matches HOA-shaped patterns (condominium, owners association,
   homeowners, condo, COA, HOA, cooperative apartment, tenants corp, etc.)
3. Reject patterns common to non-HOA non-profits (church, PTO, alumni, etc.)

Output: ``state_scrapers/ny/leads/ny_registry_seed.jsonl``
Shape:  ``{"name", "state": "NY", "city", "postal_code", "metadata_type",
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
SRC_CSV = ROOT / "data" / "ny_active_corporations.csv"
OUT = ROOT / "state_scrapers" / "ny" / "leads" / "ny_registry_seed.jsonl"
SOURCE_URL = "https://data.ny.gov/Economic-Development/Active-Corporations-Beginning-1800/n9v6-gdp6"
SOURCE_LABEL = "ny_dos_active_corporations"

# Entity types to keep (case-insensitive substring match). NY HOAs/condos/coops
# file under a wide range of types: NFP for newer HOAs/condos, BUSINESS CORP
# for legacy NYC apartment corps and tenants corps, COOPERATIVE CORPORATION
# for housing coops, CONDOMINIUM for the new dedicated subtype, HDFC for
# Article XI affordable housing coops, LLC for newer Long Island HOAs.
NFP_TYPES = (
    "NOT-FOR-PROFIT",
    "HOUSING DEVELOPMENT FUND",  # NYC Article XI HDFC coops
    "BUSINESS CORPORATION",
    "COOPERATIVE CORPORATION",
    "DOMESTIC CONDOMINIUM",  # NY DOS dedicated condo subtype
    "LIMITED LIABILITY COMPANY",
    "LIMITED-PROFIT HOUSING",  # Mitchell-Lama
    "REDEVELOPMENT COMPANY",
)

# Names that indicate HOA / condo / coop association governance.
HOA_NAME_RE = re.compile(
    r"\b("
    r"CONDOMINIUM(S)?"
    r"|CONDO(S)?"
    r"|HOMEOWNERS?\s+ASSOC(IATION)?S?"
    r"|HOME\s+OWNERS?\s+ASSOC(IATION)?S?"
    r"|HOA"
    r"|OWNERS?\s+ASSOC(IATION)?S?"
    r"|COMMUNITY\s+ASSOC(IATION)?S?"
    r"|PROPERTY\s+OWNERS?\s+ASSOC(IATION)?S?"
    r"|TENANTS?\s+CORP(ORATION)?"
    r"|TENANTS?\s+CO\-?OP(ERATIVE)?"
    r"|HOUSING\s+CORP(ORATION)?"
    r"|HOUSING\s+CO\-?OP(ERATIVE)?"
    r"|COOPERATIVE\s+APART(MENT)?S?"
    r"|APARTMENT\s+CORP(ORATION)?"
    r"|APARTMENT\s+CO\-?OP(ERATIVE)?"
    r"|MUTUAL\s+HOUSING"
    r"|RESIDENTIAL\s+ASSOC(IATION)?S?"
    r"|TOWNHOUSE\s+ASSOC(IATION)?S?"
    r"|TOWN\s*HOUSE\s+ASSOC(IATION)?S?"
    r"|TOWNHOMES?\s+ASSOC(IATION)?S?"
    r"|VILLAGE\s+ASSOC(IATION)?S?"
    r"|GARDEN\s+APART(MENT)?S?"
    r"|HDFC"
    # NYC coop convention: "<address> OWNERS, INC." / "OWNERS LLC" / "OWNERS CORP".
    # Require the word OWNERS at end of legal name, before INC|CORP|LLC|LTD.
    r"|OWNERS,?\s+(INC|CORP|LLC|LTD)"
    r")\b",
    re.IGNORECASE,
)

# Reject patterns: non-HOA non-profits with HOA-ish names.
REJECT_RE = re.compile(
    r"\b("
    r"CHURCH|MINISTRY|TEMPLE|SYNAGOGUE|MOSQUE|CONGREGATION|CATHEDRAL"
    r"|PARENT\s+TEACHER|PTA|PTO|ALUMNI"
    r"|VETERAN|VFW|AMERICAN\s+LEGION"
    r"|BOOSTER|ATHLETIC|YACHT\s+CLUB|GOLF\s+CLUB"
    r"|FRATERN|SORORITY|MASONIC|LODGE|ROTARY|KIWANIS|LIONS\s+CLUB"
    r"|MEDICAL\s+ASSOC|DENTAL\s+ASSOC|HOSPITAL"
    r"|BAR\s+ASSOC|CHAMBER\s+OF"
    r"|BUSINESS\s+IMPROVEMENT\s+DIST"
    r"|NEIGHBORHOOD\s+IMPROVEMENT"
    r"|MERCHANTS?\s+ASSOC|LANDLORDS?\s+ASSOC|REALTORS?\s+ASSOC|REALTY\s+ASSOC"
    r"|BLOCK\s+ASSOC"  # NYC block associations are advocacy, not HOA-style
    r"|TENANTS?\s+UNION|TENANTS?\s+ASSOC"  # tenant advocacy != coop board
    r"|MUSEUM|FOUNDATION|SCHOLARSHIP"
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


def classify(name: str, entity_type: str = "") -> str:
    n = name.upper()
    et = (entity_type or "").upper()
    # Condos: explicit naming or NY DOS dedicated condo subtype.
    if re.search(r"\bCONDOMINIUM(S)?\b|\bCONDO(S)?\b|\bC\.?O\.?A\.?\b", n):
        return "condo"
    if "DOMESTIC CONDOMINIUM" in et:
        return "condo"
    # Coops: explicit coop / tenants corp / apartment corp / HDFC / mutual
    # housing / Mitchell-Lama / NY DOS cooperative-corp subtype.
    if re.search(
        r"\bHDFC\b|\bTENANTS?\s+CORP\b|\bAPARTMENT\s+CORP\b"
        r"|\bCOOPERATIVE\b|\bCO\-?OP\b|\bMUTUAL\s+HOUSING\b"
        r"|\bHOUSING\s+CORP\b|\bHOUSING\s+DEVELOPMENT\s+FUND\b",
        n,
    ):
        return "coop"
    if "COOPERATIVE CORPORATION" in et or "HOUSING DEVELOPMENT FUND" in et \
            or "LIMITED-PROFIT HOUSING" in et:
        return "coop"
    # NYC/LI convention: "<address> OWNERS, INC." / "OWNERS CORP" / "OWNERS LLC".
    # These are cooperative apartment corporations or condo-LLC sponsor entities
    # in the vast majority of cases when the name leads with a numeric address
    # or a Manhattan/LI street name. Tag as coop.
    if re.search(r"\bOWNERS,?\s+(INC|CORP|LLC|LTD)\b", n):
        return "coop"
    return "hoa"


def keep_entity_type(t: str, name: str) -> bool:
    t = (t or "").upper()
    return any(kw in t for kw in NFP_TYPES)


# Reasonable city/state pickers from the row's address fields.
def pick_city(row: dict) -> str:
    for col in ("Location City", "DOS Process City", "CEO City", "Registered Agent City"):
        v = (row.get(col) or "").strip().strip('"')
        s = (row.get(col.replace("City", "State")) or "").strip().upper()
        if v and (not s or s == "NY"):
            return v
    # Fallback to whatever is non-empty
    for col in ("Location City", "DOS Process City", "CEO City", "Registered Agent City"):
        v = (row.get(col) or "").strip().strip('"')
        if v:
            return v
    return ""


def pick_zip(row: dict) -> str:
    for col in ("Location Zip", "DOS Process Zip", "CEO Zip", "Registered Agent Zip"):
        z = normalize_zip(row.get(col) or "")
        if z:
            return z
    return ""


def main() -> int:
    if not SRC_CSV.exists():
        print(f"ERROR: missing {SRC_CSV}", file=sys.stderr)
        print(
            "Fetch with: curl -L -o data/ny_active_corporations.csv "
            "\"https://data.ny.gov/api/views/n9v6-gdp6/rows.csv?accessType=DOWNLOAD\"",
            file=sys.stderr,
        )
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    stats: Counter[str] = Counter()
    seen: set[str] = set()

    with open(SRC_CSV, newline="") as f, open(OUT, "w") as out:
        reader = csv.DictReader(f)
        for row in reader:
            stats["total"] += 1
            name = (row.get("Current Entity Name") or "").strip().strip('"').strip()
            if not name:
                stats["skip_empty_name"] += 1
                continue
            etype = (row.get("Entity Type") or "").strip()
            if not keep_entity_type(etype, name):
                stats["skip_entity_type"] += 1
                continue
            if not HOA_NAME_RE.search(name):
                stats["skip_no_hoa_keyword"] += 1
                continue
            if REJECT_RE.search(name):
                stats["skip_rejected_pattern"] += 1
                continue
            key = re.sub(r"\s+", " ", name.upper())
            if key in seen:
                stats["skip_dup_name"] += 1
                continue
            seen.add(key)

            county = (row.get("County") or "").strip().strip('"')
            city = pick_city(row)
            zip5 = pick_zip(row)
            metadata_type = classify(name, etype)

            lead = {
                "name": name,
                "state": "NY",
                "city": city,
                "postal_code": zip5,
                "metadata_type": metadata_type,
                "source": SOURCE_LABEL,
                "source_url": SOURCE_URL,
            }
            if county:
                lead["county"] = county
            out.write(json.dumps(lead, sort_keys=True) + "\n")
            stats["written"] += 1
            if metadata_type == "condo":
                stats["written_condo"] += 1
            elif metadata_type == "coop":
                stats["written_coop"] += 1
            else:
                stats["written_hoa"] += 1

    print("=== NY registry pull stats ===", file=sys.stderr)
    for k, v in stats.most_common():
        print(f"  {k}: {v:,}", file=sys.stderr)
    print(f"\noutput -> {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
