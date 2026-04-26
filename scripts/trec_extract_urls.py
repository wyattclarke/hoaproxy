#!/usr/bin/env python3
"""
Download TREC HOA management certificate PDFs and extract website URLs via OCR.

Processes in batches to avoid filling disk: downloads a batch, OCRs + extracts
URLs, deletes the PDFs, then moves to the next batch. Appends results to a
JSONL file so progress is preserved across interruptions.

Usage:
    python scripts/trec_extract_urls.py [--limit N] [--batch-size 500] [--workers 10]
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, quote

import fitz  # PyMuPDF
import pytesseract
import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "data" / "TREC_HOA_Management_Certificates_20260329.csv"
CERTS_DIR = ROOT / "scraped_hoa_docs" / "trec_texas_certs"
OUTPUT_PATH = ROOT / "data" / "trec_texas" / "extracted_urls.jsonl"

URL_RE = re.compile(r'https?://[^\s"\'<>)\]]+')
WWW_RE = re.compile(r'(?<!\S)(www\.[^\s"\'<>)\]]+)')


def slugify(name: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")


def sanitize_filename(url: str) -> str:
    name = unquote(url.rsplit("/", 1)[-1])
    name = re.sub(r'[^\w.\-]', '_', name)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:200]


def read_csv() -> list[dict]:
    rows = []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def load_already_processed() -> set[str]:
    """Load HOA names already in the output JSONL to support resuming."""
    done = set()
    if OUTPUT_PATH.exists():
        with open(OUTPUT_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        done.add(json.loads(line)["name"])
                    except (json.JSONDecodeError, KeyError):
                        pass
    return done


def fix_cert_url(raw_url: str) -> str:
    """Fix HTML-entity-encoded URLs: unescape entities, then re-encode special chars."""
    url = html.unescape(raw_url)
    # Re-encode characters that need percent-encoding in URL paths
    # Split on the path portion and re-encode only that
    parts = url.split("/sites/default/files/", 1)
    if len(parts) == 2:
        path = parts[1]
        # Decode any existing percent-encoding, then re-encode properly
        path = quote(unquote(path), safe="/-_.~")
        url = parts[0] + "/sites/default/files/" + path
    return url


def download_cert(row: dict, session: requests.Session) -> tuple[str, Path | None]:
    slug = slugify(row["Name"])[:100]  # cap slug length
    cert_url = fix_cert_url(row["Certificate"])
    filename = sanitize_filename(cert_url)[:80]  # cap filename length
    dest = CERTS_DIR / f"{slug}__{filename}"
    if dest.exists() and dest.stat().st_size > 0:
        return row["Name"], dest
    try:
        resp = session.get(cert_url, timeout=60)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return row["Name"], dest
    except Exception as exc:
        print(f"  DOWNLOAD FAIL: {row['Name']}: {exc}", file=sys.stderr)
        return row["Name"], None


def download_batch(rows: list[dict], workers: int) -> dict[str, Path]:
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path] = {}
    session = requests.Session()
    session.headers["User-Agent"] = "HOAproxy-scraper/1.0"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(download_cert, row, session) for row in rows]
        for future in as_completed(futures):
            name, path = future.result()
            if path:
                results[name] = path

    return results


def ocr_extract_urls(pdf_path: Path) -> list[str]:
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return []

    urls: list[str] = []
    for i in range(min(2, len(doc))):
        page = doc[i]
        pix = page.get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        try:
            text = pytesseract.image_to_string(img)
        except Exception:
            continue

        for match in URL_RE.findall(text):
            cleaned = match.rstrip(".,;:)")
            if "@" not in cleaned:
                urls.append(cleaned)
        for match in WWW_RE.findall(text):
            cleaned = match.rstrip(".,;:)")
            if "@" not in cleaned:
                urls.append(f"https://{cleaned}")

    doc.close()
    seen = set()
    unique = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def process_batch(batch_rows: list[dict], batch_num: int, total_batches: int,
                  workers: int, ocr_workers: int) -> tuple[int, int]:
    """Download, OCR, extract, delete. Returns (processed, with_urls)."""
    print(f"\n--- Batch {batch_num}/{total_batches} ({len(batch_rows)} rows) ---")

    # Download
    print(f"  Downloading ({workers} threads)...")
    cert_paths = download_batch(batch_rows, workers)
    print(f"  Downloaded {len(cert_paths)}/{len(batch_rows)}")

    # OCR + extract in parallel
    print(f"  OCR'ing ({ocr_workers} workers)...")
    ocr_results: dict[str, list[str]] = {}

    def _ocr(name_and_path):
        name, path = name_and_path
        return name, ocr_extract_urls(path) if path else []

    items = [(row["Name"], cert_paths.get(row["Name"])) for row in batch_rows]
    with ThreadPoolExecutor(max_workers=ocr_workers) as pool:
        for name, urls in pool.map(_ocr, items):
            ocr_results[name] = urls

    # Write results to JSONL
    urls_found = 0
    with open(OUTPUT_PATH, "a") as f:
        for row in batch_rows:
            name = row["Name"]
            urls = ocr_results.get(name, [])
            if urls:
                urls_found += 1

            record = {
                "name": name,
                "city": row.get("City", "").strip(),
                "state": "TX",
                "zip": row.get("Zip", "").strip(),
                "type": row.get("Type", "").strip(),
                "urls": urls,
                "certificate_url": html.unescape(row.get("Certificate", "")),
            }
            f.write(json.dumps(record) + "\n")

    print(f"  OCR done: {urls_found}/{len(batch_rows)} with URLs")

    # Delete downloaded PDFs to free disk
    deleted = 0
    for path in cert_paths.values():
        try:
            path.unlink()
            deleted += 1
        except OSError:
            pass
    print(f"  Cleaned up {deleted} PDFs")

    return len(batch_rows), urls_found


def main():
    parser = argparse.ArgumentParser(description="Download TREC certs and extract URLs via OCR")
    parser.add_argument("--limit", type=int, default=0, help="Process only first N rows (0=all)")
    parser.add_argument("--batch-size", type=int, default=500, help="Rows per batch (default: 500)")
    parser.add_argument("--workers", type=int, default=10, help="Download concurrency (default: 10)")
    parser.add_argument("--ocr-workers", type=int, default=0,
                        help="OCR concurrency (default: CPU count)")
    args = parser.parse_args()
    if args.ocr_workers <= 0:
        import os as _os
        args.ocr_workers = min(_os.cpu_count() or 4, 8)

    rows = read_csv()
    if args.limit > 0:
        rows = rows[:args.limit]
    print(f"CSV loaded: {len(rows)} rows")

    # Resume support: skip already-processed HOAs
    already_done = load_already_processed()
    if already_done:
        before = len(rows)
        rows = [r for r in rows if r["Name"] not in already_done]
        print(f"Resuming: {before - len(rows)} already processed, {len(rows)} remaining")

    if not rows:
        print("Nothing to do.")
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    total_processed = 0
    total_urls = 0
    total_batches = (len(rows) + args.batch_size - 1) // args.batch_size

    for i in range(0, len(rows), args.batch_size):
        batch = rows[i:i + args.batch_size]
        batch_num = i // args.batch_size + 1
        processed, with_urls = process_batch(batch, batch_num, total_batches,
                                            args.workers, args.ocr_workers)
        total_processed += processed
        total_urls += with_urls

    # Clean up empty certs dir
    try:
        CERTS_DIR.rmdir()
    except OSError:
        pass

    print(f"\n=== Done ===")
    print(f"Total processed: {total_processed + len(already_done)}")
    print(f"URLs found this run: {total_urls}")
    print(f"Output: {OUTPUT_PATH}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted (progress saved, re-run to resume)", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
