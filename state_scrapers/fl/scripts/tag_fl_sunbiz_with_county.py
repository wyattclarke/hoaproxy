#!/usr/bin/env python3
"""
Tag each FL Sunbiz HOA row with its county using the ZIP->county crosswalk.

Reads:
  data/fl_sunbiz_hoas.jsonl
  data/fl_zip_to_county.json

Writes:
  data/fl_sunbiz_hoas_geocoded.jsonl  (original rows + "county" field)
  data/fl_sunbiz_county_counts.json   (county -> count, sorted descending)

Prints summary to stdout.
"""

import json
import os
from collections import Counter

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INPUT_JSONL   = os.path.join(REPO_ROOT, "data", "fl_sunbiz_hoas.jsonl")
ZIP_MAP_PATH  = os.path.join(REPO_ROOT, "data", "fl_zip_to_county.json")
OUTPUT_JSONL  = os.path.join(REPO_ROOT, "data", "fl_sunbiz_hoas_geocoded.jsonl")
OUTPUT_COUNTS = os.path.join(REPO_ROOT, "data", "fl_sunbiz_county_counts.json")


def zip5(raw):
    """Return first 5 digits of a zip string, or '' if unavailable."""
    if not raw:
        return ""
    digits = "".join(c for c in raw[:5] if c.isdigit())
    return digits if len(digits) == 5 else ""


def main():
    # Load ZIP -> county map
    with open(ZIP_MAP_PATH) as f:
        zip_map = json.load(f)

    total = 0
    tagged = 0
    null_count = 0
    county_counter = Counter()

    out_lines = []

    with open(INPUT_JSONL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            total += 1

            # Try principal zip first, then mailing
            principal = row.get("principal") or {}
            mailing   = row.get("mailing") or {}

            z = zip5(principal.get("zip", ""))
            county = zip_map.get(z)

            if county is None:
                z2 = zip5(mailing.get("zip", ""))
                county = zip_map.get(z2)

            row["county"] = county

            if county is not None:
                tagged += 1
                county_counter[county] += 1
            else:
                null_count += 1

            out_lines.append(json.dumps(row))

    # Write geocoded JSONL
    with open(OUTPUT_JSONL, "w") as f:
        f.write("\n".join(out_lines) + "\n")

    # Build sorted counts dict
    sorted_counts = dict(sorted(county_counter.items(), key=lambda x: -x[1]))
    with open(OUTPUT_COUNTS, "w") as f:
        json.dump(sorted_counts, f, indent=2)

    # Print summary
    print(f"\n{'='*50}")
    print(f"FL Sunbiz HOA County Tagging Summary")
    print(f"{'='*50}")
    print(f"Total rows:          {total:>8,}")
    print(f"Rows with county:    {tagged:>8,}  ({100*tagged/total:.1f}%)")
    print(f"Rows with null county: {null_count:>6,}  ({100*null_count/total:.1f}%)")
    print(f"\nTop 20 counties by HOA count:")
    print(f"  {'County':<20} {'Count':>6}")
    print(f"  {'-'*20} {'-'*6}")
    for county, count in list(sorted_counts.items())[:20]:
        print(f"  {county:<20} {count:>6,}")

    print(f"\nOutput files:")
    print(f"  {OUTPUT_JSONL}")
    print(f"  {OUTPUT_COUNTS}")


if __name__ == "__main__":
    main()
