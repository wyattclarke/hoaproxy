#!/usr/bin/env python3
"""Expand discovered Lead JSONL to one row per pre-discovered PDF URL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Explode Lead JSONL to one row per pre_discovered_pdf_urls entry")
    parser.add_argument("input")
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", required=True)
    args = parser.parse_args()

    counts = {"input": 0, "output": 0, "without_pdfs": 0}
    seen: set[str] = set()
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.input).open() as src, Path(args.output).open("w") as out:
        for line in src:
            line = line.strip()
            if not line:
                continue
            counts["input"] += 1
            row = json.loads(line)
            pdfs = [str(url).split("#", 1)[0] for url in (row.get("pre_discovered_pdf_urls") or []) if url]
            if not pdfs:
                counts["without_pdfs"] += 1
                continue
            for url in pdfs:
                if url in seen:
                    continue
                seen.add(url)
                expanded = dict(row)
                expanded["source_url"] = url
                expanded["pre_discovered_pdf_urls"] = [url]
                print(json.dumps(expanded, sort_keys=True), file=out)
                counts["output"] += 1

    Path(args.summary).write_text(json.dumps(counts, indent=2, sort_keys=True))
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
