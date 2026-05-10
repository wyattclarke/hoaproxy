#!/usr/bin/env python3
"""Prune misrouted manifests under gs://hoaproxy-bank/v1/AZ/.

Some manifests get banked under non-AZ county slugs (brevard, effingham,
hall, etc.) when discovery agents misclassify cross-state document hits.
The state-mismatch reroute pass catches these when OCR finds clear non-AZ
evidence — but if OCR text is weak or the doc is brief, it's skipped.

This script does a slug-level fallback: any v1/AZ/<county>/ where <county>
isn't a recognized AZ county (or our two unknown-buckets) gets the
manifest DELETED from v1/AZ/. The doc itself stays accessible from the
discovery source URL; another state's scrape may re-bank it correctly.

Usage:
    python state_scrapers/az/scripts/az_prune_misrouted.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = ROOT / "state_scrapers" / "az" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

BANK_PREFIX = "gs://hoaproxy-bank/v1/AZ"

# Recognized AZ county slugs (15 counties + 2 placeholder slugs).
AZ_COUNTY_SLUGS = {
    "apache", "cochise", "coconino", "gila", "graham", "greenlee",
    "la-paz", "maricopa", "mohave", "navajo", "pima", "pinal",
    "santa-cruz", "yavapai", "yuma",
    "unknown-county", "_unknown-county", "unresolved-name", "_unresolved-name",
}


def list_county_dirs() -> list[str]:
    cmd = ["gsutil", "ls", f"{BANK_PREFIX}/"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line.endswith("/"):
            continue
        slug = line.rstrip("/").rsplit("/", 1)[-1]
        if slug:
            out.append(slug)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--apply", action="store_true",
                   help="Delete misrouted directories (overrides --dry-run)")
    args = p.parse_args()
    apply = args.apply

    counties = list_county_dirs()
    print(f"AZ bank counties: {sorted(counties)}")
    misrouted = sorted(set(counties) - AZ_COUNTY_SLUGS)
    print(f"Misrouted (not AZ): {misrouted}")
    if not misrouted:
        print("Nothing to prune.")
        return 0

    deleted_count = 0
    for slug in misrouted:
        prefix = f"{BANK_PREFIX}/{slug}/"
        # Count manifests for reporting
        cnt = subprocess.run(
            ["gsutil", "ls", f"{prefix}**/manifest.json"],
            capture_output=True, text=True, timeout=120,
        )
        manifests = [l for l in (cnt.stdout or "").splitlines() if l.strip().endswith("manifest.json")]
        print(f"  {slug}: {len(manifests)} manifests")
        if not apply:
            print(f"    [DRY] would delete {prefix}")
            continue
        # List individual objects + delete in batches via xargs-style.
        # gsutil -m rm -r can hang on subtrees with multi-level structure;
        # explicit per-file rm is more reliable in practice.
        ls = subprocess.run(
            ["gsutil", "ls", "-r", prefix],
            capture_output=True, text=True, timeout=120,
        )
        files = [l.strip() for l in (ls.stdout or "").splitlines()
                 if l.strip() and not l.strip().endswith(":") and not l.strip().endswith("/")]
        if not files:
            print(f"    {slug}: no files to delete")
            rm = subprocess.run(
                ["gsutil", "rm", "-r", prefix],
                capture_output=True, text=True, timeout=120,
            )
        else:
            # Single-threaded gsutil rm with file list piped via stdin
            rm = subprocess.run(
                ["gsutil", "rm"] + files,
                capture_output=True, text=True, timeout=600,
            )
        if rm.returncode == 0:
            print(f"    DELETED")
            deleted_count += len(manifests)
        else:
            print(f"    FAILED: {rm.stderr[:200]}")

    print(f"\nTotal manifests {'would delete' if not apply else 'deleted'}: "
          f"{deleted_count if apply else sum(len(subprocess.run(['gsutil', 'ls', f'{BANK_PREFIX}/{s}/**/manifest.json'], capture_output=True, text=True, timeout=120).stdout.splitlines()) for s in misrouted)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
