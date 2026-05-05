#!/usr/bin/env python3
"""Filter discovery Lead JSONL to new public URLs.

This performs only local checks: exact URL dedupe against a caller-provided
known URL list, within-run source URL dedupe, and signed/credentialed URL
rejection before any model or bank step sees the lead.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlparse


SIGNED_QUERY_KEYS = {
    "awsaccesskeyid",
    "signature",
    "x-amz-algorithm",
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-signature",
}


def _jsonl_rows(path: Path):
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _urls(row: dict) -> list[str]:
    urls = [row.get("source_url")]
    urls.extend(row.get("pre_discovered_pdf_urls") or [])
    return [str(url) for url in urls if url]


def _has_signed_query(urls: list[str]) -> bool:
    for url in urls:
        keys = {key.lower() for key, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)}
        if keys & SIGNED_QUERY_KEYS:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Filter Lead JSONL to new public, non-signed URLs")
    parser.add_argument("input")
    parser.add_argument("--known-urls", required=True, help="Newline-delimited already-banked source URLs")
    parser.add_argument("--output", required=True)
    parser.add_argument("--duplicates", required=True)
    parser.add_argument("--signed-rejects", required=True)
    parser.add_argument("--summary", required=True)
    args = parser.parse_args()

    known = {line.strip() for line in Path(args.known_urls).read_text().splitlines() if line.strip()}
    seen_sources: set[str] = set()
    counts = {"input": 0, "known_url": 0, "duplicate_in_run": 0, "signed_url": 0, "kept": 0}

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.output).open("w") as out, Path(args.duplicates).open("w") as dup, Path(args.signed_rejects).open("w") as signed:
        for row in _jsonl_rows(Path(args.input)):
            counts["input"] += 1
            urls = _urls(row)
            source = str(row.get("source_url") or (urls[0] if urls else ""))
            if _has_signed_query(urls):
                counts["signed_url"] += 1
                print(json.dumps(row, sort_keys=True), file=signed)
                continue
            if any(url in known for url in urls):
                counts["known_url"] += 1
                print(json.dumps(row, sort_keys=True), file=dup)
                continue
            if source in seen_sources:
                counts["duplicate_in_run"] += 1
                print(json.dumps(row, sort_keys=True), file=dup)
                continue
            seen_sources.add(source)
            counts["kept"] += 1
            print(json.dumps(row, sort_keys=True), file=out)

    Path(args.summary).write_text(json.dumps(counts, indent=2, sort_keys=True))
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
