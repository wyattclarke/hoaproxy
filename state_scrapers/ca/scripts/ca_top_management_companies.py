#!/usr/bin/env python3
"""Driver C — Identify top CA property management companies from CA
registry registered-agent column.

Reads data/ca_registry_hoas.jsonl, normalizes RA names, rejects law firms /
RA services / personal names, outputs top 50 by HOA count to
data/ca_top_management_companies.json.

Mirrors state_scrapers/fl/scripts/fl_top_management_companies.py with
CA-specific reject patterns (Adams Stirling, Berding Weil, etc.).
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INPUT = ROOT / "data" / "ca_registry_hoas.jsonl"
OUTPUT = ROOT / "data" / "ca_top_management_companies.json"

LEGAL_SUFFIXES = re.compile(
    r"\b(INC\.?|LLC|L\.L\.C\.?|P\.A\.?|LTD\.?|CORP\.?|CO\.?|LP|L\.P\.?|"
    r"PLLC|PLC|PC|P\.C\.?|INCORPORATED|LIMITED|COMPANY|CORPORATION|"
    r"A\s+CALIFORNIA\s+CORPORATION|"
    r"A\s+PROFESSIONAL\s+(LAW\s+)?CORPORATION)\b\.?$",
    re.IGNORECASE,
)

REJECT_PATTERNS = [
    # Law firm keywords
    r"\bLAW\b", r"\bLEGAL\b", r"\bATTORNEY\b", r"\bATTORNEYS\b",
    r"\bCOUNSEL\b", r"\bESQ\b", r"\bA\s+PROFESSIONAL\s+CORP\b",
    r"\bAPC\b", r"\bAPLC\b",
    # Named CA HOA law firms
    r"ADAMS\s+STIRLING", r"BERDING\s+WEIL", r"WHITNEY[\s&]+SMITH",
    r"PETERS[\s&]+FREEDMAN", r"FELDMAN\s+BERMAN",
    r"FIORE\s+RACOBS\s+POWERS", r"SADDLEBACK\s+VALLEY",
    r"TINNELLY\s+LAW", r"NEWMEYER\s+DILLION",
    r"NEUMILLER[\s&]+BEARDSLEE", r"EPSTEN\s+GRINNELL",
    r"FIRESTONE\s+&\s+PETERS", r"DELPHI\s+LAW",
    r"WHITE,?\s+ZUCKERMAN", r"KULIK\s+GOTTESMAN",
    # Generic registered-agent services
    r"CORPORATION SERVICE COMPANY", r"\bCSC\b",
    r"CT CORPORATION", r"\bSKRLD\b",
    r"CORPORATE CREATIONS NETWORK", r"REGISTERED AGENTS",
    r"NORTHWEST REGISTERED", r"NATIONAL REGISTERED",
    r"INCORP SERVICES", r"HARBOR COMPLIANCE",
    r"LEGALZOOM", r"INCFILE", r"BIZFILE",
    # Self-RA / generic terms
    r"^\s*HOMEOWNERS?\s+ASSOCIATION", r"^\s*CONDOMINIUM\s+ASSOC",
]
REJECT_RE = re.compile("|".join(REJECT_PATTERNS), re.IGNORECASE)


def normalize(name: str) -> str:
    n = name.upper().strip()
    n = re.sub(r"[,.\s]+$", "", n)
    n = re.sub(r"\s+", " ", n)
    prev = None
    while prev != n:
        prev = n
        n = LEGAL_SUFFIXES.sub("", n).strip().rstrip(",. ")
    return n.strip()


def is_personal_name(name: str) -> bool:
    tokens = name.split()
    if len(tokens) == 2:
        a, b = tokens
        if a.isalpha() and b.isalpha() and len(a) + len(b) < 22:
            return True
    if len(tokens) == 3:
        a, b, c = tokens
        # Middle initial pattern: "JOHN A SMITH"
        if a.isalpha() and len(b) <= 2 and c.isalpha() and len(a) + len(c) < 22:
            return True
    return False


def should_reject(normalized: str, corp_name: str) -> tuple[bool, str]:
    if not normalized:
        return True, "empty"
    if REJECT_RE.search(normalized):
        return True, "law_firm_or_generic_ra"
    if is_personal_name(normalized):
        return True, "personal_name"
    corp_norm = normalize(corp_name)
    if corp_norm and normalized == corp_norm:
        return True, "self_registered_agent"
    if len(normalized) < 4:
        return True, "too_short"
    return False, ""


def main() -> None:
    counts: Counter[str] = Counter()
    norm_to_raw: dict[str, str] = {}
    norm_to_hoas: dict[str, list[str]] = defaultdict(list)

    with INPUT.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            ra_name = (rec.get("agent_name") or "").strip()
            corp_name = (rec.get("name") or "").strip()
            if not ra_name:
                continue
            norm = normalize(ra_name)
            reject, reason = should_reject(norm, corp_name)
            if reject:
                continue
            counts[norm] += 1
            norm_to_raw[norm] = ra_name
            norm_to_hoas[norm].append(corp_name)

    top50 = counts.most_common(50)
    result = []
    for norm, count in top50:
        sample = norm_to_hoas[norm][:5]
        result.append(
            {
                "normalized_name": norm,
                "name": norm_to_raw[norm],
                "hoa_count": count,
                "sample_hoas": sample,
            }
        )

    OUTPUT.write_text(json.dumps(result, indent=2))
    print(f"Wrote {len(result)} entries to {OUTPUT}")
    print("\nTop 30 CA management companies by HOA count:")
    print(f"{'#':>3}  {'Count':>6}  Name")
    print("-" * 60)
    for i, entry in enumerate(result[:30], 1):
        print(f"{i:>3}  {entry['hoa_count']:>6}  {entry['normalized_name']}")


if __name__ == "__main__":
    main()
