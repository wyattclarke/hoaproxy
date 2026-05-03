"""CLI: probe a single HOA lead.

Usage::

    python -m hoaware.discovery probe \
        --name "Foo HOA" \
        --state VA \
        --city Reston \
        --website https://example.org/ \
        --source manual \
        --source-url https://example.org/

Or batch from a JSONL file (one Lead per line)::

    python -m hoaware.discovery probe-batch leads.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Iterator

from .leads import Lead
from .probe import probe
from .sources.nc_aggregators import ALL_SOURCES, nc_leads


def _print_result(lead: Lead, result, *, json_out: bool) -> None:
    payload = {
        "name": lead.name,
        "state": lead.state,
        "manifest_uri": result.manifest_uri,
        "homepage_fetched": result.homepage_fetched,
        "platform": result.platform,
        "is_walled": result.is_walled,
        "documents_banked": result.documents_banked,
        "documents_skipped": result.documents_skipped,
    }
    if json_out:
        print(json.dumps(payload))
    else:
        print(
            f"[{result.manifest_uri}] "
            f"banked={result.documents_banked} skipped={result.documents_skipped} "
            f"platform={result.platform} walled={result.is_walled}"
        )


def _iter_jsonl(path: str) -> Iterator[Lead]:
    with open(path) as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"line {line_no}: invalid JSON: {exc}", file=sys.stderr)
                continue
            try:
                yield Lead(**d)
            except TypeError as exc:
                print(f"line {line_no}: {exc}", file=sys.stderr)


def cmd_probe(args) -> int:
    lead = Lead(
        name=args.name,
        source=args.source,
        source_url=args.source_url or args.website or "",
        state=args.state,
        city=args.city,
        county=args.county,
        website=args.website,
    )
    result = probe(lead, bucket_name=args.bucket)
    _print_result(lead, result, json_out=args.json)
    return 0


def cmd_probe_batch(args) -> int:
    failures = 0
    for lead in _iter_jsonl(args.path):
        try:
            result = probe(lead, bucket_name=args.bucket)
            _print_result(lead, result, json_out=args.json)
        except Exception as exc:
            failures += 1
            print(f"FAILED {lead.name}: {exc}", file=sys.stderr)
            if args.fail_fast:
                return 2
    return 1 if failures else 0


def cmd_scrape_leads(args) -> int:
    """Scrape leads from an aggregator source and emit JSONL."""
    if args.region == "nc":
        sources = args.source or None
        leads = nc_leads(sources)
    else:
        print(f"Unknown region: {args.region}", file=sys.stderr)
        return 2

    out = open(args.output, "w") if args.output else sys.stdout
    count = 0
    try:
        for lead in leads:
            print(json.dumps(asdict(lead)), file=out)
            count += 1
    finally:
        if args.output:
            out.close()

    print(f"Scraped {count} leads.", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hoaware.discovery")
    p.add_argument("--bucket", default=None, help="Override default bank bucket")
    p.add_argument("--json", action="store_true", help="Output JSON per result")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("probe", help="Probe one HOA")
    pp.add_argument("--name", required=True)
    pp.add_argument("--source", default="manual")
    pp.add_argument("--source-url", default=None)
    pp.add_argument("--state", default=None)
    pp.add_argument("--city", default=None)
    pp.add_argument("--county", default=None)
    pp.add_argument("--website", default=None)
    pp.set_defaults(func=cmd_probe)

    pb = sub.add_parser("probe-batch", help="Probe leads from a JSONL file")
    pb.add_argument("path")
    pb.add_argument("--fail-fast", action="store_true")
    pb.set_defaults(func=cmd_probe_batch)

    sl = sub.add_parser("scrape-leads", help="Scrape leads from an aggregator region")
    sl.add_argument("region", choices=["nc"], help="Region to scrape")
    sl.add_argument(
        "--source",
        action="append",
        metavar="NAME",
        help=f"Specific source(s) to include (repeatable). Choices: {', '.join(ALL_SOURCES)}",
    )
    sl.add_argument("--output", "-o", default=None, metavar="FILE", help="Write JSONL to file (default: stdout)")
    sl.set_defaults(func=cmd_scrape_leads)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
