#!/usr/bin/env python3
"""Driver E: fetch ACRIS condo declarations and bank them.

Reads the ACRIS seed at ``state_scrapers/ny/leads/ny_acris_seed.jsonl``,
for each declaration fetches all pages from the public ACRIS image endpoint,
assembles them into a single PDF, and banks via ``hoaware.bank.bank_hoa()``
under ``gs://hoaproxy-bank/v1/NY/{borough-county}/{slug}/``.

## ACRIS image URL pattern

The public viewer at:
    https://a836-acris.nyc.gov/DS/DocumentSearch/DocumentImageView?doc_id=<id>
loads page images via:
    https://a836-acris.nyc.gov/DS/DocumentSearch/GetImage?doc_id=<id>&page=<N>

The GetImage endpoint **requires a Referer header pointing at the viewer
URL** — without it the server 307-redirects to a bandwidth-policy page.

Each page response is a **TIFF (Group 4 fax compression, ~2550x3300 pixels)**.

The total page count is in the viewer page's `searchCriteriaStringValue`
JSON (`hid_TotalPages` field).

## Usage

    # Smoke test: fetch 3 PDFs, don't bank.
    .venv/bin/python state_scrapers/ny/scripts/fetch_acris_pdf.py --limit 3

    # Smoke test with banking
    .venv/bin/python state_scrapers/ny/scripts/fetch_acris_pdf.py --limit 3 --apply

    # Full Driver E
    .venv/bin/python state_scrapers/ny/scripts/fetch_acris_pdf.py --apply

    # Resume after interruption
    .venv/bin/python state_scrapers/ny/scripts/fetch_acris_pdf.py --resume --apply
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import requests
from PIL import Image
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / "settings.env", override=False)
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "hoaware")

from hoaware.bank import bank_hoa, DocumentInput  # noqa: E402

# ---------------------------------------------------------------------------

SEED_PATH = ROOT / "state_scrapers" / "ny" / "leads" / "ny_acris_seed.jsonl"
PROGRESS_PATH = ROOT / "state_scrapers" / "ny" / "leads" / "ny_acris_fetch_progress.json"

ACRIS_BASE = "https://a836-acris.nyc.gov"
VIEWER_URL = ACRIS_BASE + "/DS/DocumentSearch/DocumentImageView?doc_id={doc_id}"
IMAGE_URL = ACRIS_BASE + "/DS/DocumentSearch/GetImage?doc_id={doc_id}&page={page}"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) hoaproxy-public-document-discovery/0.1"
)
PAGE_TIMEOUT_S = 60
MAX_PAGES = 80  # safety cap; most condo declarations are 10-50 pages

# Look for hid_TotalPages in the viewer iframe's searchCriteriaStringValue.
# The viewer embeds it both URL-encoded (%22...%22%3A) and plain ("...":); accept either.
TOTAL_PAGES_RE = re.compile(
    r'(?:"hid_TotalPages"\s*:\s*|%22hid_TotalPages%22%3A)\s*(\d+)'
)


def _session_with_referer(doc_id: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": VIEWER_URL.format(doc_id=doc_id),
        "Accept": "image/tiff,image/png,image/*;q=0.8,*/*;q=0.5",
    })
    return s


def _get_total_pages(doc_id: str, session: requests.Session) -> int:
    """Fetch the viewer page; extract hid_TotalPages."""
    r = session.get(VIEWER_URL.format(doc_id=doc_id), timeout=PAGE_TIMEOUT_S,
                    headers={"Accept": "text/html"})
    r.raise_for_status()
    m = TOTAL_PAGES_RE.search(r.text)
    if not m:
        return 0
    return int(m.group(1))


def _fetch_page_tiff(doc_id: str, page: int, session: requests.Session) -> bytes | None:
    """Fetch one page as TIFF. Returns None on 4xx/5xx."""
    url = IMAGE_URL.format(doc_id=doc_id, page=page)
    r = session.get(url, timeout=PAGE_TIMEOUT_S)
    if r.status_code != 200:
        return None
    ct = r.headers.get("Content-Type", "").lower()
    if "image/" not in ct:
        # Server may return HTML if bandwidth/quota exceeded.
        return None
    return r.content


def _tiffs_to_pdf(tiff_pages: list[bytes]) -> bytes:
    """Assemble multi-page TIFFs into a single PDF using PIL."""
    images = []
    for tiff in tiff_pages:
        img = Image.open(io.BytesIO(tiff))
        # PIL multi-page PDF save requires RGB or L mode; G4-fax TIFFs come in
        # as mode '1' (bilevel). Convert to L for compactness.
        if img.mode == "1":
            img = img.convert("L")
        elif img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
        images.append(img)
    out = io.BytesIO()
    if not images:
        return b""
    first, rest = images[0], images[1:]
    first.save(out, "PDF", resolution=300.0, save_all=True, append_images=rest)
    return out.getvalue()


def fetch_acris_pdf(doc_id: str, rate_limit_s: float = 1.0) -> tuple[bytes, int]:
    """Fetch one ACRIS document. Returns (pdf_bytes, page_count). Empty on failure."""
    session = _session_with_referer(doc_id)
    total = _get_total_pages(doc_id, session)
    if total <= 0:
        return b"", 0
    total = min(total, MAX_PAGES)
    pages = []
    for p in range(1, total + 1):
        tiff = _fetch_page_tiff(doc_id, p, session)
        if not tiff:
            # Stop on missing page; we can re-try later
            break
        pages.append(tiff)
        time.sleep(rate_limit_s)
    if not pages:
        return b"", 0
    pdf = _tiffs_to_pdf(pages)
    return pdf, len(pages)


def county_slug_for(county_name: str) -> str:
    """ACRIS legal county name → bank URL slug."""
    return county_name.lower().replace(" ", "-").strip()


def _load_progress() -> dict:
    if PROGRESS_PATH.exists():
        return json.loads(PROGRESS_PATH.read_text())
    return {"processed_doc_ids": [], "banked": 0, "rejected": 0, "failed": 0,
            "last_doc_id": None, "started_at": datetime.now(timezone.utc).isoformat()}


def _save_progress(progress: dict) -> None:
    PROGRESS_PATH.write_text(json.dumps(progress, indent=2))


def iter_seed_records(limit: int | None, skip_ids: set[str]) -> Iterator[dict]:
    if not SEED_PATH.exists():
        raise SystemExit(f"missing seed: {SEED_PATH}")
    emitted = 0
    with open(SEED_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("document_id") in skip_ids:
                continue
            yield rec
            emitted += 1
            if limit and emitted >= limit:
                break


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap number of docs to process this run (smoke testing).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually bank to GCS. Default is dry-run (just print).")
    ap.add_argument("--resume", action="store_true",
                    help="Resume from progress file (skip already-processed doc_ids).")
    ap.add_argument("--rate-limit-seconds", type=float, default=1.0,
                    help="Sleep between page fetches (default 1.0).")
    ap.add_argument("--state", default="NY")
    args = ap.parse_args()

    progress = _load_progress() if args.resume else {
        "processed_doc_ids": [], "banked": 0, "rejected": 0, "failed": 0,
        "last_doc_id": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    skip = set(progress["processed_doc_ids"])
    if args.resume:
        print(f"[resume] {len(skip)} doc_ids already processed", file=sys.stderr)

    counter = 0
    for rec in iter_seed_records(args.limit, skip):
        doc_id = rec["document_id"]
        name = rec.get("name") or ""
        county = rec.get("county") or "_unknown-county"
        street = rec.get("street_address") or ""
        postal = rec.get("postal_code") or ""
        city = rec.get("city") or ""
        bbl = rec.get("bbl_primary") or ""

        print(f"[{counter}] doc_id={doc_id} name={name[:50]}", file=sys.stderr)
        try:
            pdf_bytes, page_count = fetch_acris_pdf(doc_id, args.rate_limit_seconds)
        except Exception as e:
            print(f"  FAIL fetch: {type(e).__name__}: {e}", file=sys.stderr)
            progress["failed"] += 1
            progress["processed_doc_ids"].append(doc_id)
            progress["last_doc_id"] = doc_id
            _save_progress(progress)
            counter += 1
            continue

        if not pdf_bytes:
            print(f"  no PDF bytes (page_count={page_count})", file=sys.stderr)
            progress["rejected"] += 1
            progress["processed_doc_ids"].append(doc_id)
            progress["last_doc_id"] = doc_id
            _save_progress(progress)
            counter += 1
            continue

        print(f"  fetched {page_count} pages, {len(pdf_bytes):,} bytes", file=sys.stderr)

        if not args.apply:
            print(f"  DRY-RUN: would bank to gs://hoaproxy-bank/v1/{args.state}/{county_slug_for(county)}/<slug>/", file=sys.stderr)
            progress["processed_doc_ids"].append(doc_id)
            counter += 1
            continue

        # Build metadata_source + address + document_input
        address = {
            "state": args.state,
            "county": county,
            "city": city or None,
            "street": street or None,
            "postal_code": postal or None,
        }
        metadata_source = {
            "source": rec.get("source") or "ny-acris-decl-2026-05",
            "source_url": rec.get("source_url") or "",
            "extra": {
                "acris_document_id": doc_id,
                "acris_crfn": rec.get("crfn") or "",
                "acris_bbl_primary": bbl,
                "acris_bbls_all": rec.get("bbls_all") or [],
                "acris_property_type": rec.get("property_type") or "",
                "acris_recorded_date": rec.get("recorded_date") or "",
            },
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }
        doc_input = DocumentInput(
            pdf_bytes=pdf_bytes,
            source_url=rec.get("source_url") or "",
            filename=f"acris_{doc_id}.pdf",
            category_hint="ccr",  # ACRIS DECL → CCR (declaration of condominium = CCR equivalent)
            text_extractable_hint=False,  # TIFF page images, scanned
        )

        try:
            manifest_uri = bank_hoa(
                name=name,
                metadata_type=(
                    "condo" if rec.get("property_type", "").startswith("D") else "hoa"
                ),
                address=address,
                metadata_source=metadata_source,
                documents=[doc_input],
            )
            print(f"  BANKED → {manifest_uri}", file=sys.stderr)
            progress["banked"] += 1
        except Exception as e:
            print(f"  FAIL bank: {type(e).__name__}: {e}", file=sys.stderr)
            progress["failed"] += 1

        progress["processed_doc_ids"].append(doc_id)
        progress["last_doc_id"] = doc_id
        _save_progress(progress)
        counter += 1

        if counter % 50 == 0:
            print(
                f"== checkpoint: processed={counter} banked={progress['banked']} "
                f"rejected={progress['rejected']} failed={progress['failed']}",
                file=sys.stderr,
            )

    print(json.dumps({
        "summary": "ny_acris_pdf_fetch",
        "processed": counter,
        "banked": progress["banked"],
        "rejected": progress["rejected"],
        "failed": progress["failed"],
        "applied": args.apply,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
