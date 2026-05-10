#!/usr/bin/env python3
"""Parse CA SoS bulk corp dump (data/california_hoa_entities.csv) + merge
legacy data/california/{hoa_index,hoa_details}.jsonl into a unified
ca_registry_hoas.jsonl.

Status filter: status == "1" (active) only.
Reject patterns: mobile-home parks, CFD/Mello-Roos, voluntary civic, etc.
Output: data/ca_registry_hoas.jsonl

Each output row:
{
  "name": "...",                # uppercased canonical
  "name_clean": "...",          # legal-suffix-stripped, for query gen
  "doc_no": "...",              # entity_number
  "source": "corp" | "legacy_index" | "legacy_details" | "merged",
  "status": "1" | ...,
  "entity_type": "ARTS" | "llc" | ...,
  "file_date": "YYYYMMDD",
  "agent_name": "...",          # registered agent
  "agent_city": "...",
  "agent_state": "...",
  "mail_city": "...",
  "mail_state": "...",
  "city_norm": "...",           # uppercase, used for county lookup
  "county": "san-diego" | null, # slug
  "county_source": "agent_city" | "mail_city" | "legacy" | "zip" | null,
  "zip": "92127" | null,        # if extractable from legacy address
  "pm_company": "...",          # legacy enrichment
  "pm_address": "...",          # legacy enrichment
  "pm_website": "...",          # legacy enrichment
  "legacy_id": "..."            # if matched against legacy
}
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CORP_CSV = ROOT / "data" / "california_hoa_entities.csv"
LEGACY_INDEX = ROOT / "data" / "california" / "hoa_index.jsonl"
LEGACY_DETAILS = ROOT / "data" / "california" / "hoa_details.jsonl"
CITY_COUNTY_MAP = ROOT / "data" / "ca_city_to_county.json"
ZIP_COUNTY_MAP = ROOT / "data" / "ca_zip_to_county.json"
OUTPUT = ROOT / "data" / "ca_registry_hoas.jsonl"
INACTIVE_OUTPUT = ROOT / "data" / "ca_registry_inactive.jsonl"

# Hard reject patterns: things that look like HOAs but aren't governed by
# Davis-Stirling. Layered on top of the CA SoS pre-filter.
REJECT_NAME_RE = re.compile(
    r"\b("
    # Mobile-home parks (Civ Code §799 — different statute)
    r"MOBILE\s+HOME|MOBILEHOME|MHP\b|MOBILE\s+HOME\s+PARK"
    r"|TRAILER\s+PARK|RV\s+PARK|R\.V\.\s+PARK|MANUFACTURED\s+HOME"
    # Mello-Roos / CFDs
    r"|COMMUNITY\s+FACILITIES\s+DISTRICT|MELLO[-\s]ROOS|\bCFD\b"
    r"|ASSESSMENT\s+DISTRICT|COMMUNITY\s+SERVICES\s+DISTRICT"
    # Voluntary civic / professional
    r"|BAR\s+ASSOC|BUSINESS\s+ASSOC|MERCHANTS?\s+ASSOC|CHAMBER\s+OF"
    r"|REALTORS?\s+ASSOC|REALTY\s+ASSOC|LANDLORDS?\s+ASSOC"
    r"|PARENT\s+TEACHER|\bPTA\b|\bPTO\b|ALUMNI"
    r"|CHURCH|MINISTRY|TEMPLE|SYNAGOGUE|MOSQUE"
    r"|VETERAN|VFW\b|AMERICAN\s+LEGION"
    r"|BOOSTER|ATHLETIC|SOCCER|BASEBALL|FOOTBALL|TENNIS\s+CLUB"
    r"|GARDEN\s+CLUB|YACHT\s+CLUB|GOLF\s+CLUB|BRIDGE\s+CLUB"
    r"|FRATERN|SORORITY|MASONIC|LODGE\b|ROTARY|KIWANIS|LIONS\s+CLUB"
    r"|MEDICAL\s+ASSOC|DENTAL\s+ASSOC|HOSPITAL"
    r"|WATER\s+DISTRICT|IRRIGATION\s+DISTRICT"
    r"|UTILITY\s+DISTRICT|FIRE\s+PROTECTION\s+DISTRICT"
    # Senior 55+ keep-list — explicitly NOT rejected; these are real CIDs
    r")",
    re.IGNORECASE,
)

# Legal suffixes to strip for query-name generation
LEGAL_SUFFIX_RE = re.compile(
    r",?\s+(INC\.?|LLC\.?|L\.L\.C\.?|CORP\.?|CORPORATION|LTD\.?|"
    r"COMPANY|CO\.?|INCORPORATED|"
    r"A\s+CALIFORNIA\s+(MUTUAL\s+BENEFIT\s+|NON.?PROFIT\s+|NONPROFIT\s+)?"
    r"(MUTUAL\s+BENEFIT\s+CORPORATION|"
    r"NON.?PROFIT\s+(MUTUAL\s+BENEFIT\s+)?CORPORATION|"
    r"PUBLIC\s+BENEFIT\s+CORPORATION|CORPORATION)|"
    r"MUTUAL\s+BENEFIT\s+CORPORATION|"
    r"NON.?PROFIT\s+(MUTUAL\s+BENEFIT\s+)?CORPORATION)\.?\s*$",
    re.IGNORECASE,
)
TRAIL_RE = re.compile(r"[\s,;.\"]+$")

MIN_NAME_LEN = 12  # for query generation


def clean_name(raw: str) -> str:
    n = (raw or "").strip().strip('"').strip()
    # Strip suffixes iteratively (handles `INC., a California ... Corp`)
    prev = None
    while prev != n:
        prev = n
        n = LEGAL_SUFFIX_RE.sub("", n).strip()
        n = TRAIL_RE.sub("", n)
    return n


def normalize_name_key(raw: str) -> str:
    """Normalized name for cross-source dedup (uppercase, no punctuation,
    no legal suffix, single-spaced)."""
    n = clean_name(raw).upper()
    n = re.sub(r"[^A-Z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def normalize_city(raw: str) -> str:
    n = (raw or "").strip().upper().strip('"').strip()
    return n


def lookup_county(
    city: str,
    zip5: str | None,
    city_map: dict,
    zip_map: dict,
) -> tuple[str | None, str | None]:
    """Returns (county_slug, source). Tries city first, then ZIP."""
    if city:
        slug = city_map.get(city)
        if slug:
            return slug, "city"
    if zip5:
        slug = zip_map.get(zip5)
        if slug:
            return slug, "zip"
    return None, None


def load_legacy_details(path: Path) -> dict:
    """Returns {legacy_id: row}. Each row has city, county, pm_address, etc."""
    out = {}
    if not path.exists():
        return out
    with path.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            lid = str(r.get("id") or "")
            if lid:
                out[lid] = r
    return out


def load_legacy_index(path: Path) -> dict:
    """Returns {legacy_id: {name, county}}."""
    out = {}
    if not path.exists():
        return out
    with path.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            lid = str(r.get("id") or "")
            if lid:
                out[lid] = r
    return out


# Legacy data has known-buggy county tags (e.g., RIVERSIDE city -> Orange).
# We use legacy as a *fallback* for rows where corp dump can't be tagged.
# When both corp-tag and legacy-tag conflict, prefer corp-tag.

ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def extract_zip(addr: str) -> str | None:
    if not addr:
        return None
    m = ZIP_RE.search(addr)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corp-csv", default=str(CORP_CSV))
    ap.add_argument("--legacy-index", default=str(LEGACY_INDEX))
    ap.add_argument("--legacy-details", default=str(LEGACY_DETAILS))
    ap.add_argument("--city-county", default=str(CITY_COUNTY_MAP))
    ap.add_argument("--zip-county", default=str(ZIP_COUNTY_MAP))
    ap.add_argument("--output", default=str(OUTPUT))
    ap.add_argument("--inactive-output", default=str(INACTIVE_OUTPUT))
    args = ap.parse_args()

    if not Path(args.city_county).exists() or not Path(args.zip_county).exists():
        print(
            f"ERROR: missing city/zip county maps. Run "
            f"build_ca_city_county_map.py and build_ca_zip_county_map.py first.",
            file=sys.stderr,
        )
        return 1

    with open(args.city_county) as f:
        city_map = json.load(f)
    with open(args.zip_county) as f:
        zip_map = json.load(f)

    # Load legacy data
    legacy_index = load_legacy_index(Path(args.legacy_index))
    legacy_details = load_legacy_details(Path(args.legacy_details))

    # Build a name -> legacy_id map for matching corp -> legacy
    legacy_name_to_id: dict[str, str] = {}
    for lid, row in legacy_index.items():
        nk = normalize_name_key(row.get("name") or "")
        if nk:
            legacy_name_to_id[nk] = lid
    # Also from details (legal_name field)
    for lid, row in legacy_details.items():
        nk = normalize_name_key(row.get("legal_name") or row.get("aka") or "")
        if nk and nk not in legacy_name_to_id:
            legacy_name_to_id[nk] = lid

    print(
        f"Loaded {len(legacy_index)} legacy_index rows, "
        f"{len(legacy_details)} legacy_details rows, "
        f"{len(legacy_name_to_id)} unique legacy name keys",
        file=sys.stderr,
    )

    stats = Counter()
    by_county: Counter = Counter()
    seen_keys: set = set()
    out_active = open(args.output, "w")
    out_inactive = open(args.inactive_output, "w")

    with open(args.corp_csv, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["total_rows"] += 1
            raw_name = (row.get("entity_name") or "").strip()
            status = (row.get("status") or "").strip()
            entity_type = (row.get("entity_type") or "").strip()
            doc_no = (row.get("entity_number") or "").strip()
            if not raw_name:
                stats["empty_name"] += 1
                continue

            # Reject patterns
            if REJECT_NAME_RE.search(raw_name):
                stats["rejected_pattern"] += 1
                continue

            # Normalize name + dedup key
            name_clean = clean_name(raw_name)
            name_key = normalize_name_key(raw_name)
            if not name_key or len(name_clean) < MIN_NAME_LEN:
                stats["name_too_short"] += 1
                continue

            agent_city = normalize_city(row.get("agent_address_city") or "")
            mail_city = normalize_city(row.get("mail_city") or "")
            agent_state = (row.get("agent_address_state") or "").strip().upper()
            mail_state = (row.get("mail_state") or "").strip().upper()
            agent_name = (row.get("agent_name") or "").strip()

            # Determine county. Prefer agent_city (CA-resident requirement
            # makes RA address more reliable than mail address).
            county, county_src = lookup_county(agent_city, None, city_map, zip_map)
            if not county:
                county, county_src = lookup_county(mail_city, None, city_map, zip_map)
            if not county:
                # Try legacy enrichment for this name
                lid = legacy_name_to_id.get(name_key)
                if lid:
                    legacy_row = legacy_details.get(lid) or legacy_index.get(lid) or {}
                    legacy_county = (legacy_row.get("county") or "").strip().lower().replace(" ", "-")
                    if legacy_county:
                        county = legacy_county
                        county_src = "legacy"

            # Pull legacy enrichment when name matches
            lid = legacy_name_to_id.get(name_key)
            legacy_row = (legacy_details.get(lid) if lid else None) or {}
            legacy_index_row = (legacy_index.get(lid) if lid else None) or {}

            zip5 = extract_zip(legacy_row.get("pm_address") or "") if legacy_row else None
            if zip5 and not county:
                county, county_src = lookup_county("", zip5, city_map, zip_map)

            out_row = {
                "name": raw_name,
                "name_clean": name_clean,
                "name_key": name_key,
                "doc_no": doc_no,
                "source": "corp" + ("+legacy" if lid else ""),
                "status": status,
                "entity_type": entity_type,
                "file_date": (row.get("file_date") or "").strip(),
                "agent_name": agent_name,
                "agent_city": agent_city,
                "agent_state": agent_state,
                "mail_city": mail_city,
                "mail_state": mail_state,
                "city_norm": agent_city or mail_city,
                "county": county,
                "county_source": county_src,
                "zip": zip5,
                "legacy_id": lid,
                "pm_company": (legacy_row.get("pm_company") or "").strip() if legacy_row else "",
                "pm_address": (legacy_row.get("pm_address") or "").strip() if legacy_row else "",
                "pm_website": (legacy_row.get("pm_website") or "").strip() if legacy_row else "",
                "legacy_county": (legacy_index_row.get("county") or legacy_row.get("county") or "") if lid else "",
            }

            # Status filter: 1 = active. Other codes go to inactive output
            # for possible tertiary sweep.
            if status == "1":
                if name_key in seen_keys:
                    stats["dup_active_name"] += 1
                    continue
                seen_keys.add(name_key)
                out_active.write(json.dumps(out_row, sort_keys=True) + "\n")
                stats["kept_active"] += 1
                if county:
                    by_county[county] += 1
                else:
                    stats["no_county"] += 1
            else:
                out_inactive.write(json.dumps(out_row, sort_keys=True) + "\n")
                stats[f"inactive_status_{status or 'blank'}"] += 1

    out_active.close()
    out_inactive.close()

    print("\n=== Stats ===", file=sys.stderr)
    for k, v in stats.most_common():
        print(f"  {k}: {v:,}", file=sys.stderr)

    print("\n=== Top 20 counties (active) ===", file=sys.stderr)
    for c, n in by_county.most_common(20):
        print(f"  {c}: {n:,}", file=sys.stderr)

    untagged = stats["kept_active"] - sum(by_county.values())
    if stats["kept_active"]:
        coverage = 100.0 * sum(by_county.values()) / stats["kept_active"]
        print(
            f"\n  county-tagged: {sum(by_county.values()):,}/{stats['kept_active']:,} "
            f"({coverage:.1f}%)",
            file=sys.stderr,
        )
        print(f"  untagged: {untagged:,}", file=sys.stderr)
    print(f"\nactive   -> {args.output}", file=sys.stderr)
    print(f"inactive -> {args.inactive_output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
