#!/usr/bin/env python3
"""Build Cook Chicagoland seed from the Cook Assessor parcel-addresses dataset.

Cook Assessor's `Assessor - Parcel Addresses` Socrata dataset (3723-97qp) has
a `mail_address_name` field. For condo/HOA buildings, that field often
contains the association's truncated name (Cook Assessor truncates at ~22
chars). Pulling all distinct pin10s where the mailing name matches an
association suffix gives us a deterministic ~11,620-entity registry of
Cook-County named condo/HOA buildings — the §2a fast-path the playbook was
written for.

Output schema matches DC reference (state_scrapers/dc/leads/dc_cama_condo_seed.jsonl).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[3]

SOCRATA_BASE = "https://datacatalog.cookcountyil.gov/resource/3723-97qp.json"

# Mailing-name patterns we treat as associations. Note: ASSOCIATES (without the
# trailing N or no-N) is a business-LLC suffix, NOT an association — exclude.
ASSOC_LIKE = (
    "ASSOCIATION", "ASSN.", "ASSN", "ASSOC.",
    "CONDOMINIUM", "CONDOMINIUMS",
    "HOMEOWNERS", "HOMEOWNER",
    "HOA",
    "TOWNHOME", "TOWNHOMES", "TOWNHOUSE",
)

# Property-tax-class codes that indicate residential condo / townhome.
# 299 = condominium unit, 211/212 = co-op, 234 = townhome
CONDO_CLASSES = {"299", "211", "212", "234"}

# Tokens we strip from the mailing name (mgmt-co prefixes that contaminate)
# so the canonical entity name comes out cleaner.
LEADING_MGMT_RE = re.compile(
    r"^(C/O|CO|CARE\s+OF)\s+",
    re.I,
)


def normalize_mailing_name(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    # Strip trailing/leading whitespace and quotes
    s = s.strip(" \t\"'")
    # Strip "C/O ..." prefix
    s = LEADING_MGMT_RE.sub("", s)
    # Strip mid-string "C/O <mgmt-co>" — keep only the part BEFORE C/O.
    # Example: "PB HOA C O ALMA PROP" → "PB HOA"
    s = re.sub(r"\s+(C/O|C\.O\.|C\s+O|CARE\s+OF)\s+.*$", "", s, flags=re.I)
    # Strip trailing INC / LLC / CORP boilerplate (we'll keep one canonical "Inc" later if needed)
    s = re.sub(r"\s*,?\s*(INC|LLC|CORP|LTD|LP|N\.?P\.?|CO)\.?\s*$", "", s, flags=re.I)
    # Title-case the all-caps name (keep small connector words lowercase)
    if s == s.upper() and any(c.isalpha() for c in s):
        words = s.split()
        out = []
        for i, w in enumerate(words):
            if i > 0 and w.lower() in {"of", "the", "at", "on", "and", "de", "la", "del", "in", "by", "for"}:
                out.append(w.lower())
            elif w in {"II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"}:
                out.append(w)  # keep Roman numerals
            elif re.match(r"^\d", w):
                out.append(w)  # keep numeric
            else:
                out.append(w.title())
        s = " ".join(out)
        # Common abbreviation expansions
        s = re.sub(r"\bAssn\b", "Association", s)
        s = re.sub(r"\bAssoc\b", "Association", s)
        s = re.sub(r"\bTwnhm\b", "Townhomes", s, flags=re.I)
        s = re.sub(r"\bTh\b", "Townhomes", s, flags=re.I)
        s = re.sub(r"\bTerr\b", "Terrace", s, flags=re.I)
        s = re.sub(r"\bMgmt\b", "Management", s, flags=re.I)
        s = re.sub(r"\bCondo\b", "Condominium", s, flags=re.I)
        s = re.sub(r"\bDev\b", "Development", s, flags=re.I)
    return s


_BAD_NAME_TOKENS = re.compile(
    r"\b("
    r"ASSOCIATES|MANAGEMENT|MANAGEMENT\s+SERVICES|"  # business LLCs masquerading as assocs
    r"BANK|TRUSTEE|TRUST\s+CO|TRUST\s+COMPANY|"
    r"PROPERTIES|REALTY|REAL\s+ESTATE|MORTGAGE|"
    r"INVESTMENT|HOLDINGS|GROUP|ENTERPRISES|"
    r"PARTNERSHIP|VENTURES|DEVELOPMENT\s+CO|"
    r"COUNTY\s+OF|CITY\s+OF|VILLAGE\s+OF|TOWN\s+OF|"
    r"DEPT\s+OF|DEPARTMENT\s+OF"
    r")\b",
    re.I,
)


def looks_like_assoc(name: str) -> bool:
    if not name:
        return False
    upper = name.upper()
    # Word-boundary match — exclude e.g. "ASSOCIATES" matching ASSN
    matched = False
    for tok in ASSOC_LIKE:
        # Require word boundary before AND after the token.
        if re.search(rf"\b{re.escape(tok)}\b", upper):
            matched = True
            break
    if not matched:
        return False
    # Reject if it contains business-LLC noise tokens
    if _BAD_NAME_TOKENS.search(upper):
        return False
    return True


def fetch_paginated(where: str, *, page_size: int = 1000, max_pages: int = 100, delay: float = 0.05):
    """Generator yielding rows from the Socrata SODA endpoint."""
    offset = 0
    pages = 0
    while True:
        params = {
            "$where": where,
            "$select": (
                "pin10, prop_address_full, prop_address_city_name, "
                "prop_address_zipcode_1, mail_address_name, year"
            ),
            "$limit": page_size,
            "$offset": offset,
            "$order": "pin10",
        }
        r = requests.get(SOCRATA_BASE, params=params, timeout=120)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return
        for row in rows:
            yield row
        if len(rows) < page_size:
            return
        offset += page_size
        pages += 1
        if pages >= max_pages:
            return
        time.sleep(delay)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--output",
        default=str(ROOT / "state_scrapers/il/leads/il_chicagoland_assessor_seed.jsonl"),
    )
    p.add_argument("--year", type=int, default=2024,
                   help="Tax year to pull (latest with full data is usually current_year - 1)")
    p.add_argument("--limit-rows", type=int, default=0,
                   help="0 = unlimited; cap for testing")
    p.add_argument("--show-samples", type=int, default=10)
    args = p.parse_args()

    # Build SoQL where clause: condo class AND mail name looks like an association
    name_clause = " OR ".join(
        [f"upper(mail_address_name) like '%{tok}%'" for tok in ASSOC_LIKE]
    )
    where = f"year={args.year} AND ({name_clause})"

    # Group by canonical (name, city) — keep one representative pin10/address
    # per association. Multiple pin10s can share the same association (e.g.
    # six condo units in the same building).
    seen_key: dict[tuple[str, str], dict] = {}
    rows_seen = 0
    for row in fetch_paginated(where, page_size=1000, max_pages=200):
        rows_seen += 1
        pin10 = row.get("pin10")
        if not pin10:
            continue
        raw_name = row.get("mail_address_name") or ""
        if not looks_like_assoc(raw_name):
            continue
        canonical = normalize_mailing_name(raw_name)
        if not canonical or len(canonical) < 6:
            continue
        city = (row.get("prop_address_city_name") or "").upper()
        key = (canonical.lower(), city)
        if key in seen_key:
            # Already have a representative; just track pin10 count
            seen_key[key]["pin10_count"] = seen_key[key].get("pin10_count", 1) + 1
            continue
        seen_key[key] = {
            "name": canonical,
            "raw_assessor_name": raw_name,
            "state": "IL",
            "county": "Cook",
            "metadata_type": (
                "condo" if any(t in raw_name.upper() for t in ("CONDOMINIUM", "CONDO"))
                else "hoa"
            ),
            "address": {
                "street": row.get("prop_address_full"),
                "city": row.get("prop_address_city_name"),
                "state": "IL",
                "postal_code": row.get("prop_address_zipcode_1"),
            },
            "registry_id": pin10,
            "pin10": pin10,
            "pin10_count": 1,
            "source": "cook-assessor-3723-97qp-mail-name",
            "source_url": (
                f"https://datacatalog.cookcountyil.gov/resource/3723-97qp.json"
                f"?$where=pin10='{pin10}'"
            ),
            "discovery_pattern": "name-list-first-mail-address",
        }
        if args.limit_rows and rows_seen >= args.limit_rows:
            break

    seen_pin10 = seen_key  # rename for downstream code

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for ent in seen_pin10.values():
            f.write(json.dumps(ent, sort_keys=True) + "\n")

    print(json.dumps({
        "rows_seen": rows_seen,
        "unique_assoc_name_city": len(seen_pin10),
        "output": str(out_path),
        "year": args.year,
    }, sort_keys=True))

    if args.show_samples and seen_pin10:
        print("\nsample entries:")
        for ent in list(seen_pin10.values())[: args.show_samples]:
            print(f"  pin10={ent['pin10']}  {ent['name']:50s}  @ {(ent['address'].get('street') or '')[:30]}, "
                  f"{(ent['address'].get('city') or '')[:15]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
