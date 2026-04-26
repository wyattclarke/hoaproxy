#!/usr/bin/env python3
"""
Push a bulk-import JSON file to the HOAware API.

Usage:
    python scripts/push_import.py <import_file.json> [--api-url URL] [--batch-size 500] [--dry-run]

Reads JWT_SECRET from settings.env for admin auth.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent


def load_jwt_secret() -> str:
    """Load JWT_SECRET from settings.env or environment."""
    secret = os.environ.get("JWT_SECRET")
    if secret:
        return secret

    env_file = ROOT / "settings.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("JWT_SECRET="):
                return line.split("=", 1)[1].strip().strip("'\"")

    print("ERROR: JWT_SECRET not found in environment or settings.env", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Push bulk-import JSON to HOAware API")
    parser.add_argument("import_file", help="Path to import JSON file")
    parser.add_argument("--api-url", default="https://hoaproxy-app.onrender.com",
                        help="API base URL (default: production)")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Records per batch (default: 500)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate file and print stats without POSTing")
    args = parser.parse_args()

    # Load import file
    import_path = Path(args.import_file)
    if not import_path.exists():
        print(f"ERROR: File not found: {import_path}", file=sys.stderr)
        sys.exit(1)

    with open(import_path) as f:
        data = json.load(f)

    source = data.get("source", "unknown")
    records = data.get("records", [])
    print(f"Source: {source}")
    print(f"Records: {len(records)}")
    print(f"Scraped at: {data.get('scraped_at', 'unknown')}")

    if args.dry_run:
        # Print stats
        with_coords = sum(1 for r in records if r.get("latitude") and r.get("longitude"))
        with_boundary = sum(1 for r in records if r.get("boundary_geojson"))
        states = set(r.get("state", "?") for r in records)
        cities = set(r.get("city", "?") for r in records)
        print(f"With coordinates: {with_coords}")
        print(f"With boundary: {with_boundary}")
        print(f"States: {', '.join(sorted(states))}")
        print(f"Cities: {', '.join(sorted(cities))}")
        print("\nDry run complete — no data pushed.")
        return

    # Load auth
    jwt_secret = load_jwt_secret()
    headers = {
        "Authorization": f"Bearer {jwt_secret}",
        "Content-Type": "application/json",
    }

    endpoint = f"{args.api_url.rstrip('/')}/admin/bulk-import"

    # Split into batches and push
    total_imported = 0
    total_skipped = 0
    total_errors = []

    for i in range(0, len(records), args.batch_size):
        batch = records[i:i + args.batch_size]
        batch_num = i // args.batch_size + 1
        total_batches = (len(records) + args.batch_size - 1) // args.batch_size

        print(f"\nBatch {batch_num}/{total_batches}: {len(batch)} records...", end=" ")

        payload = {"source": source, "records": batch}
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=120)

        if resp.status_code != 200:
            print(f"FAILED ({resp.status_code})")
            print(f"  {resp.text}")
            total_errors.append(f"Batch {batch_num}: HTTP {resp.status_code}")
            continue

        result = resp.json()
        print(f"OK (imported={result['imported']}, skipped={result['skipped']})")
        if result.get("errors"):
            for err in result["errors"]:
                print(f"  error: {err}")
            total_errors.extend(result["errors"])

        total_imported += result["imported"]
        total_skipped += result["skipped"]

    print(f"\n=== Summary ===")
    print(f"Total imported: {total_imported}")
    print(f"Total skipped:  {total_skipped}")
    if total_errors:
        print(f"Total errors:   {len(total_errors)}")


if __name__ == "__main__":
    main()
