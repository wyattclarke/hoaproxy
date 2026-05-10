#!/usr/bin/env python3
"""Pull TX HOA/condo seed leads from the TREC HOA Management Certificate
registry (already cached at ``data/tx_hoa_management_certificates.csv``).

Source provenance
-----------------
Texas SB 1588 (2021) requires every property owners' association governed by
Chapter 209 of the Texas Property Code to file an electronic management
certificate with TREC. TREC publishes the data through the Texas Open Data
Portal (Socrata, dataset ``8auc-hzdi``) and exposes a public search at
``hoa.texas.gov/management-certificates-search``. The bulk CSV is free, no
auth, no captcha. Fetch::

    curl -L -o data/tx_hoa_management_certificates.csv \\
        "https://data.texas.gov/api/views/8auc-hzdi/rows.csv?accessType=DOWNLOAD"

The CSV is association-level (one row per management certificate), with
columns: Name, County, City, Zip, Type (POA|COA), Certificate (PDF URL).
"Type" maps directly to our ``metadata_type`` (POA -> hoa, COA -> condo).

Output: ``state_scrapers/tx/leads/tx_registry_seed.jsonl``
Shape:  ``{"name", "state": "TX", "city", "postal_code", "metadata_type",
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
SRC_CSV = ROOT / "data" / "tx_hoa_management_certificates.csv"
OUT = ROOT / "state_scrapers" / "tx" / "leads" / "tx_registry_seed.jsonl"
SOURCE_URL = "https://www.hoa.texas.gov/management-certificates-search"
SOURCE_LABEL = "tx_trec_hoa_management_certificates"

ZIP5_RE = re.compile(r"(\d{5})")


def normalize_zip(z: str) -> str:
    z = (z or "").strip()
    if not z:
        return ""
    m = ZIP5_RE.search(z)
    return m.group(1) if m else ""


def classify(name: str, type_field: str) -> str:
    t = (type_field or "").strip().upper()
    if t == "COA":
        return "condo"
    if t == "POA":
        # POA can still be a condo if name contains condo terms; trust name first
        n = name.upper()
        if re.search(r"\bCONDOMINIUM(S)?\b|\bCONDO(S)?\b", n):
            return "condo"
        return "hoa"
    # Unknown type — fall back to name
    n = name.upper()
    if re.search(r"\bCONDOMINIUM(S)?\b|\bCONDO(S)?\b", n):
        return "condo"
    return "hoa"


def main() -> int:
    if not SRC_CSV.exists():
        print(f"ERROR: missing {SRC_CSV}", file=sys.stderr)
        print(
            "Fetch with: curl -L -o data/tx_hoa_management_certificates.csv "
            "\"https://data.texas.gov/api/views/8auc-hzdi/rows.csv?accessType=DOWNLOAD\"",
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
            name = (row.get("Name") or "").strip().strip('"').strip()
            if not name:
                stats["skip_empty_name"] += 1
                continue
            key = re.sub(r"\s+", " ", name.upper())
            if key in seen:
                stats["skip_dup_name"] += 1
                continue
            seen.add(key)

            county_raw = (row.get("County") or "").strip().strip('"')
            # The "County" column sometimes contains the state (when blank in source).
            # Treat 2-letter values as missing county.
            county = "" if len(county_raw) <= 2 else county_raw
            city = (row.get("City") or "").strip().strip('"')
            zip5 = normalize_zip(row.get("Zip") or "")
            metadata_type = classify(name, row.get("Type") or "")

            lead = {
                "name": name,
                "state": "TX",
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
            else:
                stats["written_hoa"] += 1

    print("=== TX registry pull stats ===", file=sys.stderr)
    for k, v in stats.most_common():
        print(f"  {k}: {v:,}", file=sys.stderr)
    print(f"\noutput -> {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
