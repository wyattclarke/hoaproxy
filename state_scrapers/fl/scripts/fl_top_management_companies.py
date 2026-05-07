#!/usr/bin/env python3
"""Identify top FL property management companies from sunbiz HOA registered agents.

Reads data/fl_sunbiz_hoas_geocoded.jsonl, normalizes registered-agent names,
rejects non-management categories, and outputs top 50 by HOA count to
data/fl_top_management_companies.json.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INPUT = ROOT / "data" / "fl_sunbiz_hoas_geocoded.jsonl"
OUTPUT = ROOT / "data" / "fl_top_management_companies.json"

# ---------------------------------------------------------------------------
# Legal suffix stripping
# ---------------------------------------------------------------------------
LEGAL_SUFFIXES = re.compile(
    r"\b(INC\.?|LLC|L\.L\.C\.?|P\.A\.?|LTD\.?|CORP\.?|CO\.?|LP|L\.P\.?|"
    r"PLLC|PLC|PC|P\.C\.?|INCORPORATED|LIMITED|COMPANY|CORPORATION)\b\.?$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Hard reject patterns (law firms, generic RA services, etc.)
# ---------------------------------------------------------------------------
REJECT_PATTERNS = [
    # Law firm keywords
    r"\bLAW\b",
    r"\bLEGAL\b",
    r"\bATTORNEY\b",
    r"\bATTORNEYS\b",
    r"\bCOUNSEL\b",
    r"\bESQ\b",
    r"\bPLLC\b",  # often law firm
    # Named law firms
    r"BECKER[\s&]+POLIAKOFF",
    r"WASSERSTEIN",
    r"VALANCY",
    r"ASSOCIATION LAW GROUP",
    r"APPLETON\s+REISS",
    r"BERGER\s+SINGERMAN",
    r"KAYE\s+BENDER\s+REMBAUM",
    # Generic registered-agent services
    r"CORPORATION SERVICE COMPANY",
    r"CT CORPORATION",
    r"\bSKRLD\b",
    r"CORPORATE CREATIONS NETWORK",
    r"REGISTERED AGENTS",
    r"NORTHWEST REGISTERED",
    r"NATIONAL REGISTERED",
    r"INCORP SERVICES",
    r"HARBOR COMPLIANCE",
]
REJECT_RE = re.compile("|".join(REJECT_PATTERNS), re.IGNORECASE)


def normalize(name: str) -> str:
    """Uppercase, strip legal suffixes, collapse whitespace, strip punctuation."""
    n = name.upper().strip()
    # strip trailing punctuation (commas, periods, etc.)
    n = re.sub(r"[,.\s]+$", "", n)
    # collapse internal whitespace
    n = re.sub(r"\s+", " ", n)
    # iteratively strip legal suffixes
    prev = None
    while prev != n:
        prev = n
        n = LEGAL_SUFFIXES.sub("", n).strip().rstrip(",. ")
    return n.strip()


def is_personal_name(name: str) -> bool:
    """Heuristic: two tokens both alphabetic, total chars < 22 => personal name."""
    tokens = name.split()
    if len(tokens) == 2:
        a, b = tokens
        if a.isalpha() and b.isalpha() and len(a) + len(b) < 22:
            return True
    # Also catch "LAST, FIRST" format from raw data artifacts
    if re.match(r"^[A-Z]+\s+[A-Z]+$", name) and len(tokens) == 2:
        if all(len(t) < 12 for t in tokens):
            return True
    return False


def should_reject(normalized: str, corp_name: str) -> tuple[bool, str]:
    """Return (reject, reason) for a normalized RA name."""
    if not normalized:
        return True, "empty"
    if REJECT_RE.search(normalized):
        return True, "law_firm_or_generic_ra"
    if is_personal_name(normalized):
        return True, "personal_name"
    # RA name is essentially the same as the HOA corp name (self-RA)
    corp_norm = normalize(corp_name)
    if corp_norm and normalized == corp_norm:
        return True, "self_registered_agent"
    # Suspiciously short (likely junk)
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

            ra = rec.get("registered_agent") or {}
            ra_name = (ra.get("name") or "").strip()
            corp_name = (rec.get("name") or "").strip()

            if not ra_name:
                continue

            norm = normalize(ra_name)
            reject, reason = should_reject(norm, corp_name)
            if reject:
                continue

            counts[norm] += 1
            norm_to_raw[norm] = ra_name  # keep last-seen raw form
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

    # Print top 30 to stdout for review
    print("\nTop 30 FL management companies by HOA count:")
    print(f"{'#':>3}  {'Count':>6}  Name")
    print("-" * 60)
    for i, entry in enumerate(result[:30], 1):
        print(f"{i:>3}  {entry['hoa_count']:>6}  {entry['normalized_name']}")


if __name__ == "__main__":
    main()
