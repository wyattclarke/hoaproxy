#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hoaware.config import load_settings
from scripts.legal.source_quality import classify_source_quality


US_STATES = [
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DC",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
]

COMMUNITY_TYPES = ("hoa", "condo", "coop")
DEFAULT_ENTITY_FORM = "unknown"

GOVERNING_BUCKETS = (
    "community_act",
    "records_access",
    "records_sharing_limits",
    "proxy_voting",
    "electronic_transactions_overlay",
    "nonprofit_corp_overlay",
    "business_corp_overlay",
)

DEFAULT_REGISTRY_SEEDS_PATH = Path("data/legal/state_source_registry.json")


PILOT_SEEDS: dict[str, list[dict]] = {
    "AZ": [
        {
            "community_type": "hoa",
            "governing_law_bucket": "records_access",
            "source_type": "statute",
            "citation": "A.R.S. § 33-1805",
            "source_url": "https://www.azleg.gov/ars/33/01805.htm",
            "publisher": "Arizona State Legislature",
        }
    ],
    "CA": [
        {
            "community_type": "hoa",
            "governing_law_bucket": "records_access",
            "source_type": "statute",
            "citation": "Cal. Civ. Code § 5200",
            "source_url": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=5200.&lawCode=CIV",
            "publisher": "California Legislative Information",
        },
        {
            "community_type": "hoa",
            "governing_law_bucket": "records_access",
            "source_type": "statute",
            "citation": "Cal. Civ. Code § 5210",
            "source_url": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=5210.&lawCode=CIV",
            "publisher": "California Legislative Information",
        },
        {
            "community_type": "hoa",
            "governing_law_bucket": "records_sharing_limits",
            "source_type": "statute",
            "citation": "Cal. Civ. Code § 5220",
            "source_url": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?sectionNum=5220.&lawCode=CIV",
            "publisher": "California Legislative Information",
        },
    ],
    "FL": [
        {
            "community_type": "hoa",
            "governing_law_bucket": "records_access",
            "source_type": "statute",
            "citation": "Fla. Stat. § 720.303",
            "source_url": "https://www.flsenate.gov/Laws/Statutes/2025/720.303",
            "publisher": "The Florida Senate",
        },
        {
            "community_type": "hoa",
            "governing_law_bucket": "proxy_voting",
            "source_type": "statute",
            "citation": "Fla. Stat. § 720.306",
            "source_url": "https://www.flsenate.gov/Laws/Statutes/2025/720.306",
            "publisher": "The Florida Senate",
        },
        {
            "community_type": "hoa",
            "governing_law_bucket": "nonprofit_corp_overlay",
            "source_type": "statute",
            "citation": "Fla. Stat. § 617.0721",
            "source_url": "https://www.flsenate.gov/Laws/Statutes/2025/617.0721",
            "publisher": "The Florida Senate",
        },
    ],
    "NC": [
        {
            "community_type": "hoa",
            "governing_law_bucket": "records_access",
            "source_type": "statute",
            "citation": "N.C. Gen. Stat. § 47F-3-118",
            "source_url": "https://www.ncleg.gov/EnactedLegislation/Statutes/HTML/BySection/Chapter_47F/GS_47F-3-118.html",
            "publisher": "North Carolina General Assembly",
        },
        {
            "community_type": "condo",
            "governing_law_bucket": "records_access",
            "source_type": "statute",
            "citation": "N.C. Gen. Stat. § 47C-3-118",
            "source_url": "https://www.ncleg.gov/EnactedLegislation/Statutes/HTML/BySection/Chapter_47C/GS_47C-3-118.html",
            "publisher": "North Carolina General Assembly",
        },
        {
            "community_type": "hoa",
            "governing_law_bucket": "nonprofit_corp_overlay",
            "source_type": "statute",
            "citation": "N.C. Gen. Stat. § 55A-16-04",
            "source_url": "https://www.ncleg.gov/EnactedLegislation/Statutes/HTML/BySection/Chapter_55A/GS_55A-16-04.html",
            "publisher": "North Carolina General Assembly",
        },
        {
            "community_type": "hoa",
            "governing_law_bucket": "nonprofit_corp_overlay",
            "source_type": "statute",
            "citation": "N.C. Gen. Stat. § 55A-16-05",
            "source_url": "https://www.ncleg.net/EnactedLegislation/Statutes/PDF/BySection/Chapter_55a/GS_55A-16-05.pdf",
            "publisher": "North Carolina General Assembly",
        },
    ],
    "NY": [
        {
            "community_type": "coop",
            "governing_law_bucket": "business_corp_overlay",
            "source_type": "statute",
            "citation": "N.Y. Business Corporation Law Article 6 (Shareholders)",
            "source_url": "https://www.nysenate.gov/legislation/laws/BSC/A6",
            "publisher": "New York State Senate",
        }
    ],
    "TX": [
        {
            "community_type": "hoa",
            "governing_law_bucket": "records_access",
            "source_type": "statute",
            "citation": "Tex. Prop. Code § 209.005",
            "source_url": "https://statutes.capitol.texas.gov/Docs/PR/htm/PR.209.htm#209.005",
            "publisher": "Texas Constitution and Statutes",
        },
        {
            "community_type": "hoa",
            "governing_law_bucket": "records_sharing_limits",
            "source_type": "statute",
            "citation": "Tex. Prop. Code § 202.006",
            "source_url": "https://statutes.capitol.texas.gov/Docs/PR/htm/PR.202.htm#202.006",
            "publisher": "Texas Constitution and Statutes",
        },
    ],
    "VA": [
        {
            "community_type": "hoa",
            "governing_law_bucket": "records_access",
            "source_type": "statute",
            "citation": "Va. Code § 55.1-1815",
            "source_url": "https://law.lis.virginia.gov/vacode/title55.1/chapter18/section55.1-1815/",
            "publisher": "Code of Virginia",
        }
    ],
}

DISCOVERED_REJECT_URL_TOKENS = (
    "/assembly/",
    "/committees/",
    "/members/",
    "/schedule/",
    "/journals/",
    "/votes/",
    "/vote",
    "/publicacts",
    "/sessioninfo/",
    "/billbook",
    "/bill_status/",
    "/standingcommittees",
    "/roster",
    "/search/",
)

DISCOVERED_STATUTE_URL_TOKENS = (
    "/statute",
    "/statutes",
    "statute",
    "statutes",
    "/code",
    "code",
    "codes",
    "/rcw/",
    "/nrs/",
    "/ors/",
    "/vacode/",
    "cencode",
    "citeid=",
    "infobase=statutes",
    "laws_toc",
    "onechapter.aspx",
    "xcode",
    "chapterid=",
    "actid=",
    "/title",
    "/chapter",
    "/section",
    "/laws/",
    "/ars/",
    "/idstat/",
    "/hrs",
)

DISCOVERED_BUCKET_TOKENS = {
    "electronic_transactions_overlay": (
        "electronic",
        "signature",
        "record",
        "ueta",
    ),
    "proxy_voting": (
        "proxy",
        "ballot",
        "quorum",
    ),
    "records_access": (
        "records",
        "inspect",
        "books",
    ),
    "records_sharing_limits": (
        "records",
        "privacy",
        "confidential",
        "redact",
    ),
    "nonprofit_corp_overlay": (
        "nonprofit",
        "corporation",
        "corporate",
    ),
    "business_corp_overlay": (
        "business",
        "corporation",
        "corporate",
    ),
    "community_act": (
        "condominium",
        "homeowners",
        "planned community",
        "common interest",
    ),
}


def _seed_row(state: str, community_type: str, bucket: str) -> dict:
    return {
        "jurisdiction": state,
        "community_type": community_type,
        "entity_form": DEFAULT_ENTITY_FORM,
        "governing_law_bucket": bucket,
        "source_slot": 1,
        "source_type": "unknown",
        "citation": None,
        "source_url": None,
        "publisher": None,
        "priority": 99,
        "retrieval_status": "pending_discovery",
        "verification_status": "unverified",
        "source_quality": "unknown",
        "notes": "Placeholder row; source still needs discovery and verification.",
    }


def _load_registry_seed_map(registry_path: Path) -> dict[str, list[dict[str, Any]]]:
    if not registry_path.exists():
        return {}
    try:
        raw = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, list):
        return {}

    seed_map: dict[str, list[dict[str, Any]]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        state = str(item.get("jurisdiction") or "").upper()
        community_type = str(item.get("community_type") or "").lower()
        bucket = str(item.get("governing_law_bucket") or "").lower()
        source_url = str(item.get("source_url") or "").strip()
        if not state or state not in US_STATES:
            continue
        if community_type not in COMMUNITY_TYPES:
            continue
        if bucket not in GOVERNING_BUCKETS:
            continue
        if not source_url:
            continue
        seed = dict(item)
        seed["jurisdiction"] = state
        seed["community_type"] = community_type
        seed["governing_law_bucket"] = bucket
        seed["source_url"] = source_url
        seed_map.setdefault(state, []).append(seed)
    for state, seeds in seed_map.items():
        seeds.sort(key=lambda row: int(row.get("priority") or 999))
    return seed_map


def build_source_map(registry_path: Path | None = None) -> list[dict]:
    rows: list[dict] = []
    by_key: dict[tuple[str, str, str], list[int]] = {}
    for state in US_STATES:
        for community_type in COMMUNITY_TYPES:
            for bucket in GOVERNING_BUCKETS:
                row = _seed_row(state, community_type, bucket)
                key = (state, community_type, bucket)
                by_key[key] = [len(rows)]
                rows.append(row)

    registry_seeds = _load_registry_seed_map(registry_path or DEFAULT_REGISTRY_SEEDS_PATH)
    seeds_by_state: dict[str, list[dict[str, Any]]] = {}
    for state, seeds in PILOT_SEEDS.items():
        seeds_by_state.setdefault(state, []).extend(seeds)
    for state, seeds in registry_seeds.items():
        # Registry is deterministic source-of-truth and should be applied first.
        combined = list(seeds)
        if state in seeds_by_state:
            combined.extend(seeds_by_state[state])
        seeds_by_state[state] = combined

    seeded_slot_counts: dict[tuple[str, str, str], int] = {}
    seeded_urls_by_key: dict[tuple[str, str, str], set[str]] = {}
    for state, seeds in seeds_by_state.items():
        for idx, seed in enumerate(seeds, start=1):
            community_type = seed["community_type"]
            bucket = seed["governing_law_bucket"]
            key = (state, community_type, bucket)
            if key not in by_key:
                continue
            source_url = str(seed.get("source_url") or "").strip()
            if not source_url:
                continue
            seen_urls = seeded_urls_by_key.setdefault(key, set())
            if source_url in seen_urls:
                continue
            seen_urls.add(source_url)
            slot = seeded_slot_counts.get(key, 0) + 1
            seeded_slot_counts[key] = slot
            if slot == 1:
                row_idx = by_key[key][0]
                row = rows[row_idx]
            else:
                row = _seed_row(state, community_type, bucket)
                row["source_slot"] = slot
                row["priority"] = idx
                rows.append(row)
                by_key[key].append(len(rows) - 1)
            default_notes = "Seeded from existing project references; verify section text before production legal reliance."
            verification_status = str(seed.get("verification_status") or "seed_unverified")
            notes = str(seed.get("notes") or default_notes)
            row.update(
                {
                    "source_slot": slot,
                    "entity_form": str(seed.get("entity_form") or row.get("entity_form") or DEFAULT_ENTITY_FORM).lower(),
                    "source_type": seed.get("source_type") or "statute",
                    "citation": seed["citation"],
                    "source_url": source_url,
                    "publisher": seed["publisher"],
                    "priority": int(seed.get("priority") or idx),
                    "retrieval_status": str(seed.get("retrieval_status") or "seeded"),
                    "verification_status": verification_status,
                    "source_quality": str(seed.get("source_quality") or "").strip().lower()
                    or classify_source_quality(
                        source_type=str(seed.get("source_type") or "unknown"),
                        source_url=source_url,
                    ),
                    "notes": notes,
                }
            )
    rows.sort(
        key=lambda r: (
            r["jurisdiction"],
            r["community_type"],
            r["governing_law_bucket"],
            r.get("source_slot", 1),
            r["priority"],
        )
    )
    return rows


def merge_discovered_seeds(rows: list[dict], discovered_path: Path) -> list[dict]:
    if not discovered_path.exists():
        return rows
    try:
        discovered = json.loads(discovered_path.read_text(encoding="utf-8"))
    except Exception:
        return rows
    if not isinstance(discovered, list):
        return rows

    keyed_rows: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        key = (
            str(row.get("jurisdiction")),
            str(row.get("community_type")),
            str(row.get("governing_law_bucket")),
        )
        keyed_rows.setdefault(key, []).append(row)

    def _accept_discovered_seed(seed: dict) -> bool:
        url = str(seed.get("source_url") or "").strip().lower()
        bucket = str(seed.get("governing_law_bucket") or "").strip().lower()
        notes = str(seed.get("notes") or "").strip().lower()
        citation = str(seed.get("citation") or "").strip().lower()
        source_type = str(seed.get("source_type") or "unknown")
        if not url:
            return False
        if any(token in url for token in DISCOVERED_REJECT_URL_TOKENS):
            return False
        has_statute_token = any(token in url for token in DISCOVERED_STATUTE_URL_TOKENS)
        if not has_statute_token:
            source_quality = classify_source_quality(source_type=source_type, source_url=url)
            official_like = source_quality in {"official_primary", "official_secondary"}
            fallback_note = "fallback emitted from seed url" in notes
            statute_context = any(
                token in f"{url} {citation} {notes}"
                for token in (
                    "statute",
                    "statutes",
                    "code",
                    "codes",
                    "chapter",
                    "title",
                    "section",
                )
            )
            if not (official_like and (fallback_note or statute_context)):
                return False
        expected_tokens = DISCOVERED_BUCKET_TOKENS.get(bucket, ())
        if expected_tokens:
            haystack = f"{url} {citation} {notes}"
            if not any(token in haystack for token in expected_tokens):
                return False
        return True

    for seed in discovered:
        if not isinstance(seed, dict):
            continue
        if not _accept_discovered_seed(seed):
            continue
        jurisdiction = str(seed.get("jurisdiction") or "").upper()
        community_type = str(seed.get("community_type") or "hoa").lower()
        bucket = str(seed.get("governing_law_bucket") or "").lower()
        source_url = str(seed.get("source_url") or "").strip()
        if not jurisdiction or not bucket or not source_url:
            continue
        key = (jurisdiction, community_type, bucket)
        existing_rows = keyed_rows.setdefault(key, [])
        duplicate = any(str(row.get("source_url") or "").strip() == source_url for row in existing_rows)
        if duplicate:
            continue
        source_slot = len(existing_rows) + 1
        row = {
            "jurisdiction": jurisdiction,
            "community_type": community_type,
            "entity_form": str(seed.get("entity_form") or DEFAULT_ENTITY_FORM).lower(),
            "governing_law_bucket": bucket,
            "source_slot": source_slot,
            "source_type": str(seed.get("source_type") or "secondary_aggregator"),
            "citation": seed.get("citation"),
            "source_url": source_url,
            "publisher": seed.get("publisher"),
            "priority": int(seed.get("priority") or 80),
            "retrieval_status": str(seed.get("retrieval_status") or "seeded"),
            "verification_status": str(seed.get("verification_status") or "discovered_unverified"),
            "source_quality": str(seed.get("source_quality") or "").strip().lower()
            or classify_source_quality(
                source_type=str(seed.get("source_type") or "secondary_aggregator"),
                source_url=source_url,
            ),
            "notes": str(seed.get("notes") or "Discovered seed source."),
        }
        rows.append(row)
        existing_rows.append(row)

    for row in rows:
        if str(row.get("source_quality") or "").strip():
            continue
        row["source_quality"] = classify_source_quality(
            source_type=str(row.get("source_type") or "unknown"),
            source_url=str(row.get("source_url") or ""),
        )

    rows.sort(
        key=lambda r: (
            r["jurisdiction"],
            r["community_type"],
            r["governing_law_bucket"],
            r.get("source_slot", 1),
            r["priority"],
        )
    )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Build baseline legal source map with 50-state placeholders and pilot seeds.")
    parser.add_argument("--out", type=Path, default=None, help="Output JSON path (defaults to HOA_LEGAL_SOURCE_MAP_PATH)")
    parser.add_argument(
        "--registry-seeds",
        type=Path,
        default=DEFAULT_REGISTRY_SEEDS_PATH,
        help="Curated deterministic registry seed rows (state_source_registry.json).",
    )
    parser.add_argument(
        "--discovered-seeds",
        type=Path,
        default=Path("data/legal/discovered_seeds.json"),
        help="Optional discovered seed rows merged into source map.",
    )
    args = parser.parse_args()

    settings = load_settings()
    out_path = args.out or settings.legal_source_map_path
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = build_source_map(registry_path=args.registry_seeds)
    rows = merge_discovered_seeds(rows, args.discovered_seeds)
    out_path.write_text(json.dumps(rows, indent=2, sort_keys=False) + "\n")
    seeded = sum(1 for row in rows if row["retrieval_status"] == "seeded")
    print(f"Wrote {len(rows)} rows to {out_path}")
    print(f"Seeded rows: {seeded}")
    print(f"Pending rows: {len(rows) - seeded}")


if __name__ == "__main__":
    main()
