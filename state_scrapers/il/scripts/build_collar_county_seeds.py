#!/usr/bin/env python3
"""Build collar-county Chicagoland seeds via per-county ArcGIS REST FeatureServers.

DuPage:  https://gis.dupageco.org/arcgis/rest/services/DuPage_County_IL/ParcelsWithRealEstateCC/FeatureServer/0
         BILLNAME field is mailing-recipient name (Cook's mail_address_name equivalent).

Lake:    Geometry-only ArcGIS at maps.lakecountyil.gov/arcgis/rest/services/CCAO/...; no exposed
         BILLNAME-like field. Skipping — requires alternate path (Lake Data Extract / FOIA).

Will:    Will County GIS Data Viewer is JS-driven; no clean public REST endpoint with mailing-name
         field surfaced in §2a probe. Skipping — defer to round 4.

Kane:    gistech.countyofkane.org/arcgis/rest/services exposes only aerial imagery + geocoders;
         no parcel-with-mailing-name layer. Skipping — defer to round 4.

So this script only handles DuPage. The seed JSONL is appended to
state_scrapers/il/leads/il_chicagoland_collar_seed.jsonl.
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

DUPAGE_REST = (
    "https://gis.dupageco.org/arcgis/rest/services/DuPage_County_IL/"
    "ParcelsWithRealEstateCC/FeatureServer/0/query"
)

# Mailing-name patterns we treat as associations.
ASSOC_LIKE = (
    "ASSOCIATION", "ASSN.", "ASSN", "ASSOC.",
    "CONDOMINIUM", "CONDOMINIUMS",
    "HOMEOWNERS", "HOMEOWNER",
    "HOA",
    "TOWNHOME", "TOWNHOMES", "TOWNHOUSE",
)

LEADING_MGMT_RE = re.compile(r"^(C/O|CO|CARE\s+OF)\s+", re.I)

_BAD_NAME_TOKENS = re.compile(
    r"\b("
    r"ASSOCIATES|MANAGEMENT|MANAGEMENT\s+SERVICES|"
    r"BANK|TRUSTEE|TRUST\s+CO|TRUST\s+COMPANY|"
    r"PROPERTIES|REALTY|REAL\s+ESTATE|MORTGAGE|"
    r"INVESTMENT|HOLDINGS|GROUP|ENTERPRISES|"
    r"PARTNERSHIP|VENTURES|DEVELOPMENT\s+CO|"
    r"COUNTY\s+OF|CITY\s+OF|VILLAGE\s+OF|TOWN\s+OF|"
    r"DEPT\s+OF|DEPARTMENT\s+OF"
    r")\b",
    re.I,
)


def normalize_mailing_name(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    s = s.strip(" \t\"'")
    s = LEADING_MGMT_RE.sub("", s)
    s = re.sub(r"\s+(C/O|C\.O\.|C\s+O|CARE\s+OF)\s+.*$", "", s, flags=re.I)
    s = re.sub(r"\s*,?\s*(INC|LLC|CORP|LTD|LP|N\.?P\.?|CO)\.?\s*$", "", s, flags=re.I)
    if s == s.upper() and any(c.isalpha() for c in s):
        words = s.split()
        out = []
        for i, w in enumerate(words):
            if i > 0 and w.lower() in {"of", "the", "at", "on", "and", "de", "la", "del", "in", "by", "for"}:
                out.append(w.lower())
            elif w in {"II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"}:
                out.append(w)
            elif re.match(r"^\d", w):
                out.append(w)
            else:
                out.append(w.title())
        s = " ".join(out)
        s = re.sub(r"\bAssn\b", "Association", s)
        s = re.sub(r"\bAssoc\b", "Association", s)
        s = re.sub(r"\bTwnhm\b", "Townhomes", s, flags=re.I)
        s = re.sub(r"\bTh\b", "Townhomes", s, flags=re.I)
        s = re.sub(r"\bTerr\b", "Terrace", s, flags=re.I)
        s = re.sub(r"\bMgmt\b", "Management", s, flags=re.I)
        s = re.sub(r"\bCondo\b", "Condominium", s, flags=re.I)
        s = re.sub(r"\bDev\b", "Development", s, flags=re.I)
    return s


def looks_like_assoc(name: str) -> bool:
    if not name:
        return False
    upper = name.upper()
    matched = any(re.search(rf"\b{re.escape(tok)}\b", upper) for tok in ASSOC_LIKE)
    if not matched:
        return False
    if _BAD_NAME_TOKENS.search(upper):
        return False
    return True


def fetch_dupage_paginated(*, page_size: int = 1000, max_pages: int = 50, delay: float = 0.1):
    where_or_clauses = " OR ".join(f"BILLNAME LIKE '%{tok}%'" for tok in ASSOC_LIKE)
    where = f"({where_or_clauses})"
    offset = 0
    pages = 0
    while True:
        params = {
            "where": where,
            "outFields": "BILLNAME,PROPNAME,PROPSTNAME,REA017_PROP_CLASS",
            "resultRecordCount": page_size,
            "resultOffset": offset,
            "orderByFields": "OBJECTID",
            "f": "json",
            "returnGeometry": "false",
        }
        r = requests.get(DUPAGE_REST, params=params, timeout=120)
        r.raise_for_status()
        body = r.json()
        feats = body.get("features", []) or []
        if not feats:
            return
        for f in feats:
            yield f.get("attributes") or {}
        if len(feats) < page_size:
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
        default=str(ROOT / "state_scrapers/il/leads/il_chicagoland_collar_seed.jsonl"),
    )
    p.add_argument("--limit-rows", type=int, default=0)
    p.add_argument("--show-samples", type=int, default=10)
    args = p.parse_args()

    seen_key: dict[tuple[str, str], dict] = {}
    rows_seen = 0
    for row in fetch_dupage_paginated():
        rows_seen += 1
        raw_name = row.get("BILLNAME") or ""
        if not looks_like_assoc(raw_name):
            continue
        canonical = normalize_mailing_name(raw_name)
        if not canonical or len(canonical) < 6:
            continue
        addr_street = row.get("PROPSTNAME") or ""
        addr_city = row.get("PROPNAME") or ""  # In DuPage, PROPNAME is sometimes the place name
        key = (canonical.lower(), addr_city.upper())
        if key in seen_key:
            seen_key[key]["pin_count"] = seen_key[key].get("pin_count", 1) + 1
            continue
        seen_key[key] = {
            "name": canonical,
            "raw_assessor_name": raw_name,
            "state": "IL",
            "county": "DuPage",
            "metadata_type": (
                "condo" if any(t in raw_name.upper() for t in ("CONDOMINIUM", "CONDO"))
                else "hoa"
            ),
            "address": {
                "street": addr_street,
                "city": addr_city,
                "state": "IL",
            },
            "pin_count": 1,
            "source": "dupage-arcgis-parcelswithrealestatecc-billname",
            "source_url": (
                "https://gis.dupageco.org/arcgis/rest/services/DuPage_County_IL/"
                "ParcelsWithRealEstateCC/FeatureServer/0"
            ),
            "discovery_pattern": "name-list-first-mail-address",
        }
        if args.limit_rows and rows_seen >= args.limit_rows:
            break

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for ent in seen_key.values():
            f.write(json.dumps(ent, sort_keys=True) + "\n")

    print(json.dumps({
        "rows_seen": rows_seen,
        "unique_assoc_name_city": len(seen_key),
        "output": str(out_path),
    }, sort_keys=True))

    if args.show_samples and seen_key:
        print("\nsample entries:")
        for ent in list(seen_key.values())[: args.show_samples]:
            print(f"  {ent['name'][:45]:45s}  @ {(ent['address'].get('street') or '')[:30]:30s}  ({ent.get('pin_count','?')} units)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
