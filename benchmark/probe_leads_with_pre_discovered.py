#!/usr/bin/env python3
"""Probe Lead JSONL while preserving pre_discovered_pdf_urls."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from hoaware.discovery.leads import Lead  # noqa: E402
from hoaware.discovery.probe import probe  # noqa: E402


def _jsonl_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        print(json.dumps(payload, sort_keys=True), file=f)


def _probe_with_timeout(lead: Lead, pdf_urls: list[str], args: argparse.Namespace):
    if args.timeout <= 0:
        return probe(
            lead,
            bucket_name=args.bucket,
            max_pdfs=args.max_pdfs,
            pre_discovered_pdf_urls=pdf_urls,
        )

    def _handler(signum, frame):
        raise TimeoutError(f"probe exceeded {args.timeout}s wall-clock limit")

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(args.timeout)
    try:
        return probe(
            lead,
            bucket_name=args.bucket,
            max_pdfs=args.max_pdfs,
            pre_discovered_pdf_urls=pdf_urls,
        )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Lead JSONL with pre-discovered PDFs")
    parser.add_argument("input")
    parser.add_argument("--output", required=True)
    parser.add_argument("--bucket", default=os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank"))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--max-pdfs", type=int, default=10)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "hoaware"

    failures = 0
    with Path(args.input).open() as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            pdf_urls = list(row.pop("pre_discovered_pdf_urls", []) or [])
            row.pop("cleaning", None)
            row.pop("validation", None)
            lead = Lead(**row)
            try:
                result = _probe_with_timeout(lead, pdf_urls, args)
                payload = {"line": line_no, "lead": asdict(lead), "pdf_urls": pdf_urls, "result": asdict(result)}
                _jsonl_write(Path(args.output), payload)
                print(json.dumps({
                    "line": line_no,
                    "name": lead.name,
                    "banked": result.documents_banked,
                    "skipped": result.documents_skipped,
                    "manifest_uri": result.manifest_uri,
                }))
            except Exception as exc:
                failures += 1
                _jsonl_write(Path(args.output), {"line": line_no, "row": row, "pdf_urls": pdf_urls, "error": str(exc)})
                print(f"FAILED line {line_no} {row.get('name')}: {exc}", file=sys.stderr)
                if args.fail_fast:
                    return 2
            time.sleep(args.delay)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
