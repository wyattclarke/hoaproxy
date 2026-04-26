#!/usr/bin/env python3
"""
Add HOAs to the ingest queue.

Can be called directly or imported by scraper scripts.

Usage:
    # Single HOA
    python scripts/queue_hoa.py --name "Sunset Hills HOA" --state CA --city "Los Angeles" \
        --source california_sos --files docs/sunset_hills/ccr.pdf docs/sunset_hills/bylaws.pdf

    # From a manifest file (bulk)
    python scripts/queue_hoa.py --from-manifest data/trec_texas/upload_manifest.json \
        --import-json data/trec_texas/import.json --source trec_texas

    # From scrapers (Python API)
    from scripts.queue_hoa import enqueue
    enqueue(name="Sunset Hills HOA", state="CA", city="Los Angeles",
            source="california_sos", files=["path/to/ccr.pdf"])
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUEUE_DIR = ROOT / "data" / "ingest_queue"
PENDING = QUEUE_DIR / "pending"


def slugify(name: str) -> str:
    """Match the slugify used by scraper scripts (char-by-char, preserves double underscores)."""
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")[:120]


def enqueue(
    *,
    name: str,
    state: str,
    source: str,
    files: list[str | Path],
    city: str = "",
    postal_code: str = "",
    metadata_type: str = "hoa",
    website_url: str = "",
) -> Path:
    """Add an HOA to the ingest queue. Returns the path to the queue entry."""
    PENDING.mkdir(parents=True, exist_ok=True)

    slug = slugify(name)
    source_slug = slugify(source)
    filename = f"{source_slug}__{slug}.json"
    entry_path = PENDING / filename

    # Don't overwrite if already queued
    if entry_path.exists():
        return entry_path

    entry = {
        "name": name,
        "state": state,
        "city": city,
        "postal_code": postal_code,
        "metadata_type": metadata_type,
        "website_url": website_url,
        "source": source,
        "files": [str(Path(f).resolve()) for f in files if Path(f).exists()],
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }

    entry_path.write_text(json.dumps(entry, indent=2))
    return entry_path


def from_manifest(manifest_path: Path, import_json_path: Path, source: str) -> int:
    """Bulk-enqueue from an upload_manifest.json + import.json pair.

    This is the bridge from the old per-state format to the universal queue.
    """
    with open(manifest_path) as f:
        manifest = json.load(f)

    with open(import_json_path) as f:
        import_data = json.load(f)

    # Build name→record lookup from import.json
    records_by_name = {}
    for rec in import_data.get("records", []):
        records_by_name[rec["name"]] = rec

    # Group manifest entries by HOA slug
    from collections import defaultdict
    by_slug: dict[str, list[str]] = defaultdict(list)
    slug_to_name: dict[str, str] = {}
    for entry in manifest:
        if entry.get("action") == "upload":
            by_slug[entry["hoa_slug"]].append(entry["path"])
            # Reverse-lookup the original name from import records
            # (slugs lose info, so we match by checking all records)

    # Build slug→name mapping from import records
    for rec in import_data.get("records", []):
        s = slugify(rec["name"])
        slug_to_name[s] = rec["name"]

    queued = 0
    for slug, paths in by_slug.items():
        name = slug_to_name.get(slug)
        if not name:
            continue
        rec = records_by_name.get(name, {})
        enqueue(
            name=name,
            state=rec.get("state", ""),
            city=rec.get("city", ""),
            postal_code=rec.get("postal_code", ""),
            metadata_type=rec.get("metadata_type", "hoa"),
            website_url=rec.get("website_url", ""),
            source=source,
            files=paths,
        )
        queued += 1

    return queued


def main():
    parser = argparse.ArgumentParser(description="Add HOAs to the ingest queue")
    sub = parser.add_subparsers(dest="cmd")

    # Single HOA
    single = sub.add_parser("add", help="Queue a single HOA")
    single.add_argument("--name", required=True)
    single.add_argument("--state", required=True)
    single.add_argument("--source", required=True)
    single.add_argument("--city", default="")
    single.add_argument("--postal-code", default="")
    single.add_argument("--metadata-type", default="hoa")
    single.add_argument("--website-url", default="")
    single.add_argument("--files", nargs="+", required=True)

    # Bulk from manifest
    bulk = sub.add_parser("from-manifest", help="Bulk-queue from manifest + import.json")
    bulk.add_argument("--manifest", required=True, type=Path)
    bulk.add_argument("--import-json", required=True, type=Path)
    bulk.add_argument("--source", required=True)

    args = parser.parse_args()

    if args.cmd == "add":
        path = enqueue(
            name=args.name, state=args.state, source=args.source,
            city=args.city, postal_code=args.postal_code,
            metadata_type=args.metadata_type, website_url=args.website_url,
            files=args.files,
        )
        print(f"Queued: {path}")

    elif args.cmd == "from-manifest":
        count = from_manifest(args.manifest, args.import_json, args.source)
        print(f"Queued {count} HOAs from {args.manifest}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
