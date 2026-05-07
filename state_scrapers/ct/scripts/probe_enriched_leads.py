#!/usr/bin/env python3
"""Probe CT SoS-derived leads (enriched with `pre_discovered_pdf_urls`) and
bank into gs://hoaproxy-bank/v1/CT/. The stock `python -m hoaware.discovery
probe-batch` constructs `Lead` from JSON and ignores extra keys, which drops
the `pre_discovered_pdf_urls` we built up via Serper enrichment. This driver
keeps both.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from hoaware.discovery.leads import Lead  # noqa: E402
from hoaware.discovery.probe import probe  # noqa: E402

LEAD_FIELDS = {"name", "source", "source_url", "state", "city", "website", "county", "postal_code"}


def _probe_with_timeout(lead, pdf_urls, bucket, timeout, max_pdfs):
    if timeout <= 0:
        return probe(lead, bucket_name=bucket, max_pdfs=max_pdfs, pre_discovered_pdf_urls=pdf_urls)

    def handler(signum, frame):
        raise TimeoutError(f"probe exceeded {timeout}s")

    old = signal.signal(signal.SIGALRM, handler)
    signal.alarm(timeout)
    try:
        return probe(lead, bucket_name=bucket, max_pdfs=max_pdfs, pre_discovered_pdf_urls=pdf_urls)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=str(ROOT / "state_scrapers/ct/leads/ct_sos_associations_enriched.jsonl"))
    p.add_argument("--output", default=str(ROOT / "state_scrapers/ct/leads/ct_probe_results.jsonl"))
    p.add_argument("--bucket", default=os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank"))
    p.add_argument("--max-pdfs-per-lead", type=int, default=8)
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--probe-delay", type=float, default=0.5)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()

    inp = Path(args.input)
    outp = Path(args.output)
    outp.parent.mkdir(parents=True, exist_ok=True)

    done: set[str] = set()
    if args.resume and outp.exists():
        for line in outp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                done.add(rec.get("lead", {}).get("name") or rec.get("name") or "")
            except Exception:
                pass

    leads_in = [json.loads(l) for l in inp.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        leads_in = leads_in[: args.limit]

    out_mode = "a" if (args.resume and outp.exists()) else "w"
    n = 0
    banked_total = 0
    failures = 0
    skipped_resume = 0
    skipped_no_doc_or_site = 0
    with outp.open(out_mode, encoding="utf-8") as f:
        for raw in leads_in:
            name = raw.get("name") or ""
            if name in done:
                skipped_resume += 1
                continue
            pdf_urls = raw.get("pre_discovered_pdf_urls") or []
            website = raw.get("website")
            if not pdf_urls and not website:
                skipped_no_doc_or_site += 1
                continue
            lead_kwargs = {k: raw.get(k) for k in LEAD_FIELDS if k in raw}
            lead_kwargs.setdefault("source", "sos-ct")
            lead_kwargs.setdefault("source_url", raw.get("source_url") or "")
            lead = Lead(**lead_kwargs)
            try:
                result = _probe_with_timeout(lead, pdf_urls, args.bucket, args.timeout, args.max_pdfs_per_lead)
                rec = {
                    "lead": asdict(lead),
                    "pre_discovered_pdf_urls": pdf_urls,
                    "result": asdict(result),
                }
                banked_total += result.documents_banked
            except TimeoutError as exc:
                failures += 1
                rec = {"lead": asdict(lead), "pre_discovered_pdf_urls": pdf_urls, "error": f"timeout: {exc}"}
            except Exception as exc:
                failures += 1
                rec = {"lead": asdict(lead), "pre_discovered_pdf_urls": pdf_urls, "error": f"{type(exc).__name__}: {exc}"}
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            n += 1
            if n % 25 == 0:
                print(f"[ct-probe] processed={n} banked_docs={banked_total} failures={failures}", file=sys.stderr)
            time.sleep(args.probe_delay)

    summary = {
        "input": str(inp),
        "output": str(outp),
        "processed": n,
        "banked_documents": banked_total,
        "failures": failures,
        "skipped_resume": skipped_resume,
        "skipped_no_doc_or_site": skipped_no_doc_or_site,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
