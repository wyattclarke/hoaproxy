#!/usr/bin/env python3
"""Pull CO HOA seed leads from the Colorado HOA Information Office (DORA)
registered-HOA list (already cached at ``data/colorado_hoa_active.csv``).

Source provenance
-----------------
Colorado statute C.R.S. 38-33.3-401 requires every HOA to register annually
with the HOA Information & Resource Center inside the Department of Regulatory
Agencies (DORA, Division of Real Estate). DORA publishes the public list at
``apps.colorado.gov/dre/licensing/Lookup/LicenseLookup.aspx`` (entity-by-entity
lookup) and releases periodic bulk CSV exports through the open-data portal.
This snapshot is from 2026-03-31 and contains the active HOA universe.

The CSV has full street addresses (``Address Line 1``, ``City``, ``County``,
``ZipCode``) and unit counts, so the leads here are richer than the CA seed.

Output: ``state_scrapers/co/leads/co_registry_seed.jsonl``
Shape:  ``{"name", "state": "CO", "city", "postal_code", "metadata_type",
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
SRC_CSV = ROOT / "data" / "colorado_hoa_active.csv"
OUT = ROOT / "state_scrapers" / "co" / "leads" / "co_registry_seed.jsonl"
SOURCE_URL = "https://apps.colorado.gov/dre/licensing/Lookup/LicenseLookup.aspx"
SOURCE_LABEL = "co_dora_hoa_registry"


def classify(name: str, description: str = "") -> str:
    n = name.upper()
    if re.search(r"\bCONDOMINIUM(S)?\b|\bCONDO(S)?\b|\bC\.?O\.?A\.?\b", n):
        return "condo"
    if re.search(r"\bCO[\-\s]?OP\b|\bCOOPERATIVE\b", n):
        return "coop"
    return "hoa"


ZIP5_RE = re.compile(r"^(\d{5})")


def normalize_zip(z: str) -> str:
    z = (z or "").strip()
    if not z:
        return ""
    m = ZIP5_RE.match(z)
    return m.group(1) if m else ""


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
            name = (row.get("BusinessName") or "").strip().strip('"').strip()
            if not name:
                stats["skip_empty_name"] += 1
                continue
            status = (row.get("Description") or "").strip().lower()
            if status and status != "active":
                stats["skip_inactive"] += 1
                continue
            key = re.sub(r"\s+", " ", name.upper())
            if key in seen:
                stats["skip_dup_name"] += 1
                continue
            seen.add(key)

            city = (row.get("City") or "").strip().strip('"')
            zip5 = normalize_zip(row.get("ZipCode") or "")
            metadata_type = classify(name)

            lead = {
                "name": name,
                "state": "CO",
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

    print("=== CO registry pull stats ===", file=sys.stderr)
    for k, v in stats.most_common():
        print(f"  {k}: {v:,}", file=sys.stderr)
    print(f"\noutput -> {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
