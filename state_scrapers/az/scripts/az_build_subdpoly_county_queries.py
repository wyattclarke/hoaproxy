#!/usr/bin/env python3
"""Build name-anchored Serper queries from the Maricopa/Pima subdpoly seed.

Reads state_scrapers/az/data/az_subdpoly.jsonl and emits 2 queries per
subdivision (filetype:pdf, governing-doc keywords).

Usage:
    python state_scrapers/az/scripts/az_build_subdpoly_county_queries.py \
        --county maricopa \
        --output benchmark/results/az_subdpoly_maricopa/queries.txt \
        [--max-queries 1500] [--offset 0]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SEED_PATH = ROOT / "state_scrapers" / "az" / "data" / "az_subdpoly.jsonl"

# Trailing legal/parcel-style suffix tokens (subdpoly NAMEs often carry plat
# numbers, phase markers, etc. that hurt search precision).
STRIP_RE = re.compile(
    r"\s+("
    r"PARCEL\s+\d+[A-Z]?|UNIT\s+\d+|PHASE\s+[\dIV]+|TRACT\s+[A-Z\d-]+|"
    r"LOT\s+\d+|REPLAT|AMD|AMENDED|CORRECTED|RESUB|"
    r"AMENDED\s+PLAT|FIRST\s+AMENDED|SECOND\s+AMENDED|FINAL\s+PLAT|"
    r"BLOCK\s+\d+"
    r")\s*$",
    re.IGNORECASE,
)
# Strip trailing punctuation/whitespace
TRAIL_RE = re.compile(r"[\s,;.\-]+$")

MIN_NAME_LEN = 8  # subdpoly names tend to be slightly shorter than corp names


def clean_name(raw: str) -> str:
    name = raw.strip()
    # Repeat strip in case of stacked suffixes ("FOO PARCEL 3 AMENDED")
    prev = ""
    while prev != name:
        prev = name
        name = STRIP_RE.sub("", name)
        name = TRAIL_RE.sub("", name)
    return name


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", required=True, help="AZ county slug, e.g. maricopa")
    ap.add_argument("--output", "-o")
    ap.add_argument("--max-queries", type=int, default=1500)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument(
        "--seed-path", default=str(SEED_PATH),
        help="Override seed JSONL path (default: state_scrapers/az/data/az_subdpoly.jsonl)",
    )
    args = ap.parse_args()

    county = args.county.lower().strip()

    raw_names: list[str] = []
    seed = Path(args.seed_path)
    if not seed.exists():
        print(f"missing seed file: {seed}\n  run az_subdpoly_pull.py first", file=sys.stderr)
        sys.exit(1)

    with seed.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (row.get("county") or "").lower() != county:
                continue
            raw_names.append(row.get("name", ""))

    # Dedup, clean, length-filter.
    seen: set[str] = set()
    names: list[str] = []
    for raw in raw_names:
        cleaned = clean_name(raw)
        if len(cleaned) < MIN_NAME_LEN:
            continue
        key = cleaned.upper()
        if key in seen:
            continue
        seen.add(key)
        names.append(cleaned)

    print(
        f"county={county} raw={len(raw_names)} unique_clean={len(names)}"
        f" offset={args.offset}",
        file=sys.stderr,
    )

    names = names[args.offset:]
    max_q = args.max_queries

    lines: list[str] = [
        f"# AZ Subdpoly Driver A' — county={county} offset={args.offset}"
    ]
    count = 0
    used_names = 0
    for name in names:
        if count >= max_q:
            break
        lines.append(f'"{name}" "Arizona" filetype:pdf')
        count += 1
        if count >= max_q:
            used_names += 1
            break
        lines.append(f'"{name}" "Arizona" "declaration" OR "covenants" OR "bylaws"')
        count += 1
        used_names += 1

    text = "\n".join(lines) + "\n"

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(
            f"county={county} names_used={used_names} queries_written={count} -> {out}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
