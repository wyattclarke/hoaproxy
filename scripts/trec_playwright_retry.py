#!/usr/bin/env python3
"""
Playwright-based retry for HOAs on JS-rendered management portals.

These sites (TownSq, ConnectResident, AppFolio, etc.) load document lists
via JavaScript, so requests+BeautifulSoup sees nothing. This script uses
a headless browser to render the pages and find PDF download links.

Key patterns handled:
- TownSq: anchor text ends in .pdf, href is app.townsq.io/files-service?token=...
- General: any <a> whose text contains .pdf or whose href contains .pdf

Usage:
    python scripts/trec_playwright_retry.py [--limit N] [--workers 3] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote

import requests
from playwright.sync_api import sync_playwright, Browser

ROOT = Path(__file__).resolve().parent.parent
RETRY_PATH = ROOT / "data" / "trec_texas" / "playwright_retry.jsonl"
DOCS_DIR = ROOT / "scraped_hoa_docs" / "trec_texas"

# Subpage patterns to follow
SUBPAGE_KEYWORDS = re.compile(
    r'document|bylaw|by-law|legal|governing|cc&?r|covenant|'
    r'restriction|resource|library|file|download|'
    r'dedicatory|instrument|record|resolution|compliance',
    re.IGNORECASE,
)


def slugify(name: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")


def sanitize_filename(text: str) -> str:
    name = re.sub(r'[^\w.\-]', '_', text)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:200]


def find_pdf_links_playwright(page) -> list[dict]:
    """Extract PDF links from a rendered page.

    Handles both standard .pdf hrefs and tokenized download URLs where
    the anchor text contains .pdf.
    """
    links = page.eval_on_selector_all('a[href]', """els => els.map(e => ({
        href: e.href,
        text: (e.textContent || '').trim().substring(0, 300)
    }))""")

    found = []
    seen = set()
    for link in links:
        href = link["href"]
        text = link["text"]
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue

        # Check if it's a PDF: either .pdf in URL path, or .pdf in anchor text
        url_path = urlparse(href).path.lower()
        is_pdf_url = (url_path.endswith(".pdf")
                      or ".pdf?" in href.lower()
                      or ".pdf#" in href.lower())
        is_pdf_text = text.lower().rstrip().endswith(".pdf")

        # Also catch "files-service" / "download" style URLs with .pdf in text
        is_download_url = any(kw in href.lower() for kw in
                              ["files-service", "file-download", "download-file",
                               "getfile", "get-file", "filedownload"])

        if (is_pdf_url or is_pdf_text or (is_download_url and ".pdf" in text.lower())):
            if href not in seen:
                seen.add(href)
                # Use text as filename hint if it looks like a filename
                fname = text if text.lower().endswith(".pdf") else ""
                found.append({"url": href, "text": text, "filename_hint": fname})

    return found


def find_subpage_urls(page, base_url: str) -> list[str]:
    """Find internal links that likely lead to document pages."""
    base_host = urlparse(base_url).netloc.lower()
    links = page.eval_on_selector_all('a[href]', """els => els.map(e => ({
        href: e.href,
        text: (e.textContent || '').trim().substring(0, 200)
    }))""")

    subpages = []
    seen = set()
    for link in links:
        href = link["href"]
        text = link["text"]
        parsed = urlparse(href)

        # Same domain only
        if parsed.netloc.lower() != base_host:
            continue
        if parsed.path.lower().endswith(".pdf"):
            continue

        if SUBPAGE_KEYWORDS.search(text) or SUBPAGE_KEYWORDS.search(unquote(parsed.path)):
            if href not in seen:
                seen.add(href)
                subpages.append(href)

    # Also check for numbered pages (TownSq pattern: /0, /1, /2, /3...)
    # Look for nav links with small numbers
    for link in links:
        href = link["href"]
        parsed = urlparse(href)
        if parsed.netloc.lower() != base_host:
            continue
        # Match paths like /3, /documents, etc.
        path = parsed.path.rstrip("/")
        if path and href not in seen:
            # Follow any same-domain link that's in the nav and not already seen
            # But limit to paths that look like subpages
            if len(path.split("/")) <= 3:
                seen.add(href)
                subpages.append(href)

    return subpages


def scrape_with_playwright(record: dict, browser: Browser, session: requests.Session,
                           dry_run: bool) -> dict:
    """Scrape one HOA using Playwright for JS rendering."""
    name = record["name"]
    slug = slugify(name)
    urls = record.get("urls", [])
    result = {
        "name": name,
        "status": "no_urls",
        "doc_links": [],
        "downloaded": [],
        "errors": [],
    }

    if not urls:
        return result

    context = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    context.set_default_timeout(30000)

    try:
        page = context.new_page()
        all_pdf_links = []
        seen_urls = set()

        for start_url in urls:
            # Visit homepage
            try:
                page.goto(start_url, wait_until="networkidle", timeout=30000)
            except Exception as exc:
                result["errors"].append(f"Navigate failed: {start_url}: {exc}")
                continue

            # Find PDFs on homepage
            pdfs = find_pdf_links_playwright(page)
            for p in pdfs:
                if p["url"] not in seen_urls:
                    seen_urls.add(p["url"])
                    all_pdf_links.append(p)

            # Find and visit subpages
            subpages = find_subpage_urls(page, start_url)
            for sub_url in subpages[:10]:  # limit subpages
                try:
                    page.goto(sub_url, wait_until="networkidle", timeout=30000)
                    pdfs = find_pdf_links_playwright(page)
                    for p in pdfs:
                        if p["url"] not in seen_urls:
                            seen_urls.add(p["url"])
                            all_pdf_links.append(p)
                except Exception as exc:
                    result["errors"].append(f"Subpage failed: {sub_url}: {exc}")

        result["doc_links"] = all_pdf_links

        if not all_pdf_links:
            result["status"] = "no_docs_found"
            return result

        if dry_run:
            result["status"] = "dry_run"
            return result

        # Download PDFs
        target_dir = DOCS_DIR / slug
        target_dir.mkdir(parents=True, exist_ok=True)

        for link in all_pdf_links:
            doc_url = link["url"]
            # Use filename hint from anchor text, or derive from URL
            if link.get("filename_hint"):
                filename = sanitize_filename(link["filename_hint"])
            else:
                filename = sanitize_filename(unquote(urlparse(doc_url).path.rsplit("/", 1)[-1]))

            dest = target_dir / filename
            if dest.exists() and dest.stat().st_size > 0:
                result["downloaded"].append(str(dest))
                continue

            try:
                resp = session.get(doc_url, timeout=120)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "").lower()
                if "pdf" not in content_type and "octet" not in content_type:
                    result["errors"].append(f"Not a PDF: {doc_url} ({content_type})")
                    continue
                dest.write_bytes(resp.content)
                result["downloaded"].append(str(dest))
            except Exception as exc:
                result["errors"].append(f"Download failed: {doc_url}: {exc}")

        result["status"] = "ok" if result["downloaded"] else "download_failed"

    finally:
        context.close()

    return result


def main():
    parser = argparse.ArgumentParser(description="Playwright retry for JS-rendered portal sites")
    parser.add_argument("--limit", type=int, default=0, help="Process only first N HOAs (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="Find links but don't download")
    args = parser.parse_args()

    if not RETRY_PATH.exists():
        print(f"ERROR: {RETRY_PATH} not found", file=sys.stderr)
        sys.exit(1)

    with open(RETRY_PATH) as f:
        records = [json.loads(l) for l in f if l.strip()]

    if args.limit > 0:
        records = records[:args.limit]

    print(f"Loaded {len(records)} HOAs for Playwright retry")

    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    done = 0
    total = len(records)
    docs_total = 0
    all_results = {}

    # Playwright sync API must run on one thread — process sequentially
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for record in records:
            name = record["name"]
            try:
                result = scrape_with_playwright(record, browser, session, args.dry_run)
            except Exception as exc:
                result = {"name": name, "status": "error", "doc_links": [],
                          "downloaded": [], "errors": [str(exc)]}

            all_results[name] = result
            done += 1
            n_docs = len(result.get("downloaded", []))
            docs_total += n_docs
            if n_docs > 0:
                print(f"  [{done}/{total}] {name}: {n_docs} docs")
            elif done % 50 == 0:
                print(f"  [{done}/{total}] progress...")

        browser.close()

    # Summary
    statuses = {}
    for r in all_results.values():
        s = r["status"]
        statuses[s] = statuses.get(s, 0) + 1

    print(f"\n=== Playwright Retry Summary ===")
    print(f"Total: {total}")
    for s, c in sorted(statuses.items()):
        print(f"  {s}: {c}")
    print(f"Documents downloaded: {docs_total}")

    # Save report
    report_path = ROOT / "data" / "trec_texas" / "playwright_retry_report.json"
    with open(report_path, "w") as f:
        json.dump({"total": total, "statuses": statuses, "results": list(all_results.values())}, f, indent=2)
    print(f"Report → {report_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        import traceback; traceback.print_exc()
        sys.exit(1)
