#!/usr/bin/env python3
"""Pull CA HOA/condo seed leads from the CA Secretary of State bulk corporation
dump (already cached at ``data/california_hoa_entities.csv``).

Source provenance
-----------------
The CSV is the public CA SoS bulk corp dump filtered to nonprofit-mutual-benefit
corporations whose names contain HOA/condo/POA/COA-style keywords. The bulk file
is published by ``bizfile.sos.ca.gov`` (no auth, no captcha) and refreshed by
the CA SoS at irregular intervals; this snapshot is from 2026-03-30. The full
business search at ``bizfile.sos.ca.gov`` enforces a Cloudflare challenge for
ad-hoc queries, so the bulk dump is the only free, programmatic path.

We do NOT have street/postal-code data for most CA SoS rows (only
``agent_address_city`` and ``mail_city`` are provided in the bulk dump). We
emit those as the city hint and leave ``postal_code`` blank when missing.

Output: ``state_scrapers/ca/leads/ca_registry_seed.jsonl``
Shape:  ``{"name", "state": "CA", "city", "postal_code", "metadata_type",
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
SRC_CSV = ROOT / "data" / "california_hoa_entities.csv"
OUT = ROOT / "state_scrapers" / "ca" / "leads" / "ca_registry_seed.jsonl"
SOURCE_URL = "https://bizfileonline.sos.ca.gov/search/business"
SOURCE_LABEL = "ca_sos_bulk_corp"

# Names that look HOA-shaped but are NOT governed by Davis-Stirling. Same
# reject list as ca_parse_corp_registry.py, kept in sync.
REJECT_NAME_RE = re.compile(
    r"\b("
    r"MOBILE\s+HOME|MOBILEHOME|MHP|MOBILE\s+HOME\s+PARK"
    r"|TRAILER\s+PARK|RV\s+PARK|R\.V\.\s+PARK|MANUFACTURED\s+HOME"
    r"|COMMUNITY\s+FACILITIES\s+DISTRICT|MELLO[-\s]ROOS|CFD"
    r"|ASSESSMENT\s+DISTRICT|COMMUNITY\s+SERVICES\s+DISTRICT"
    r"|BAR\s+ASSOC|BUSINESS\s+ASSOC|MERCHANTS?\s+ASSOC|CHAMBER\s+OF"
    r"|REALTORS?\s+ASSOC|REALTY\s+ASSOC|LANDLORDS?\s+ASSOC"
    r"|PARENT\s+TEACHER|PTA|PTO|ALUMNI"
    r"|CHURCH|MINISTRY|TEMPLE|SYNAGOGUE|MOSQUE"
    r"|VETERAN|VFW|AMERICAN\s+LEGION"
    r"|BOOSTER|ATHLETIC|SOCCER|BASEBALL|FOOTBALL|TENNIS\s+CLUB"
    r"|GARDEN\s+CLUB|YACHT\s+CLUB|GOLF\s+CLUB|BRIDGE\s+CLUB"
    r"|FRATERN|SORORITY|MASONIC|LODGE|ROTARY|KIWANIS|LIONS\s+CLUB"
    r"|MEDICAL\s+ASSOC|DENTAL\s+ASSOC|HOSPITAL"
    r"|WATER\s+DISTRICT|IRRIGATION\s+DISTRICT"
    r"|UTILITY\s+DISTRICT|FIRE\s+PROTECTION\s+DISTRICT"
    r")\b",
    re.IGNORECASE,
)


def classify(name: str) -> str:
    n = name.upper()
    if re.search(r"\bCONDOMINIUM(S)?\b|\bCONDO(S)?\b|\bC\.?O\.?A\.?\b", n):
        return "condo"
    if re.search(r"\bCO[\-\s]?OP\b|\bCOOPERATIVE\b", n):
        return "coop"
    return "hoa"


def main() -> int:
    if not SRC_CSV.exists():
        print(f"ERROR: missing {SRC_CSV}", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    stats: Counter[str] = Counter()
    seen: set[str] = set()

    with open(SRC_CSV, newline="") as f, open(OUT, "w") as out:
        reader = csv.DictReader(f)
        for row in reader:
            stats["total"] += 1
            name = (row.get("entity_name") or "").strip().strip('"').strip()
            if not name:
                stats["skip_empty_name"] += 1
                continue
            status = (row.get("status") or "").strip()
            if status != "1":
                stats["skip_inactive"] += 1
                continue
            if REJECT_NAME_RE.search(name):
                stats["skip_rejected_pattern"] += 1
                continue
            key = re.sub(r"\s+", " ", name.upper())
            if key in seen:
                stats["skip_dup_name"] += 1
                continue
            seen.add(key)

            agent_city = (row.get("agent_address_city") or "").strip().strip('"')
            mail_city = (row.get("mail_city") or "").strip().strip('"')
            agent_state = (row.get("agent_address_state") or "").strip().upper()
            mail_state = (row.get("mail_state") or "").strip().upper()
            # Prefer agent_city when the agent is in CA (CA-resident requirement
            # makes agent address more reliable than mail address).
            if agent_city and (not agent_state or agent_state == "CA"):
                city = agent_city
            elif mail_city and (not mail_state or mail_state == "CA"):
                city = mail_city
            else:
                city = agent_city or mail_city

            lead = {
                "name": name,
                "state": "CA",
                "city": city,
                "postal_code": "",  # bulk dump has no street/zip
                "metadata_type": classify(name),
                "source": SOURCE_LABEL,
                "source_url": SOURCE_URL,
            }
            out.write(json.dumps(lead, sort_keys=True) + "\n")
            stats["written"] += 1

    print("=== CA registry pull stats ===", file=sys.stderr)
    for k, v in stats.most_common():
        print(f"  {k}: {v:,}", file=sys.stderr)
    print(f"\noutput -> {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
