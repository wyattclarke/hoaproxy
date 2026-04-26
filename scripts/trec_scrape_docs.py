#!/usr/bin/env python3
"""
Scrape CC&Rs, bylaws, and governing documents from HOA websites discovered
by trec_extract_urls.py.

Reads extracted_urls.jsonl, visits each HOA website, finds PDF links matching
governing document patterns, downloads them, and generates an import.json
manifest for bulk upload.

Usage:
    python scripts/trec_scrape_docs.py [--limit N] [--dry-run] [--workers 5]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

# Optional: Playwright for JS-rendered pages
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

ROOT = Path(__file__).resolve().parent.parent
URLS_PATH = ROOT / "data" / "trec_texas" / "extracted_urls.jsonl"
DOCS_DIR = ROOT / "scraped_hoa_docs" / "trec_texas"
IMPORT_PATH = ROOT / "data" / "trec_texas" / "import.json"
REPORT_PATH = ROOT / "data" / "trec_texas" / "scrape_report.json"

# Patterns for governing document links (in anchor text or href)
DOC_KEYWORDS = re.compile(
    r'cc&?r|covenant|bylaw|by-law|declaration|governing|'
    r'rules?\s*(?:and|&)\s*reg|amendment|restriction|article|'
    r'resolution|deed.?restriction|supplement|'
    r'incorporat|plat|policy|policies|procedur|budget|'
    r'financial|assessment|minutes|management.?cert|'
    r'architectural|guideline|master.?plan|insurance|'
    r'annual.?report|reserve.?study|disclosure',
    re.IGNORECASE,
)

# Patterns for subpages likely to contain document links
SUBPAGE_KEYWORDS = re.compile(
    r'document|bylaw|by-law|legal|governing|cc&?r|covenant|'
    r'restriction|resource|library|file|download|'
    r'dedicatory|instrument|record|resolution|compliance',
    re.IGNORECASE,
)

# Management portals that require login or don't host docs publicly
LOGIN_PORTALS = {
    "appfolio.com", "buildium.com", "buildinglink.com", "townsq.io",
    "connectresident.com", "frontsteps.com", "pilera.com", "caliber.com",
    "portal.associaonline.com", "myccmc.com", "ciraconnect.com",
    "envoyhoa.com", "smartwebs.com", "spectrumam.com",
    # Texas management companies (portals, not doc hosts)
    "cmamanagement.com", "houstonhoa.net", "pamcotx.com",
    "crest-management.com", "homewisedocs.com", "ciranet.com",
    "amghoa.com", "alamomanagementgroup.com", "goodwintx.com",
    "4sightpm.com", "globolink.com", "genesiscommunity.com",
    "rowcal.com", "sbbmanagement.com", "comwebportal.com",
    "secure-mgmt.com", "chaparralmanagement.com", "junctionproperty.com",
    "frontsteps.cloud",
}

TYPE_MAP = {"POA": "hoa", "COA": "condo"}


def slugify(name: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")


def sanitize_filename(url: str) -> str:
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    name = re.sub(r'[^\w.\-]', '_', name)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:200]


def is_login_portal(url: str) -> bool:
    """Block base portal domains but allow community-specific subdomains.

    e.g. block www.townsq.io but allow acypf.sites.townsq.io
    """
    host = urlparse(url).netloc.lower()
    for portal in LOGIN_PORTALS:
        if portal not in host:
            continue
        # Host matches portal — block only if it's the bare domain or www.
        stripped = host.removesuffix(portal).rstrip(".")
        if stripped == "" or stripped == "www":
            return True
    return False


def find_doc_links(base_url: str, html: str) -> list[dict]:
    """Find PDF links to governing documents in HTML."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue

        full_url = urljoin(base_url, href)
        text = (a.get_text(strip=True) or "").lower()
        href_lower = href.lower()
        url_path = urlparse(full_url).path.lower()

        is_pdf = (url_path.endswith(".pdf")
                  or ".pdf?" in href_lower or ".pdf#" in href_lower
                  or "application/pdf" in href_lower)
        has_keyword = bool(DOC_KEYWORDS.search(text) or DOC_KEYWORDS.search(unquote(url_path)))

        if is_pdf and full_url not in seen_urls:
            seen_urls.add(full_url)
            found.append({
                "url": full_url,
                "text": a.get_text(strip=True)[:200],
                "keyword_match": bool(has_keyword),
            })

    # Sort: keyword matches first
    found.sort(key=lambda x: (not x["keyword_match"], x["url"]))
    return found


def find_subpage_links(base_url: str, html: str) -> list[str]:
    """Find internal links that likely lead to document pages."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.lower()
    subpages = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue

        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        # Only follow same-domain links
        if parsed.netloc.lower() != base_host:
            continue
        # Skip if it's a PDF (already captured by find_doc_links)
        if parsed.path.lower().endswith(".pdf"):
            continue

        text = (a.get_text(strip=True) or "").lower()
        href_path = unquote(parsed.path).lower()

        if SUBPAGE_KEYWORDS.search(text) or SUBPAGE_KEYWORDS.search(href_path):
            if full_url not in seen:
                seen.add(full_url)
                subpages.append(full_url)

    return subpages


def fetch_with_playwright(url: str) -> str | None:
    """Fetch a page using Playwright (JS rendering). Returns HTML or None."""
    if not HAS_PLAYWRIGHT:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=30000, wait_until="networkidle")
            html = page.content()
            browser.close()
            return html
    except Exception:
        return None


def crawl_for_docs(start_url: str, session: requests.Session,
                    max_depth: int = 2) -> tuple[list[dict], list[str]]:
    """Crawl a site up to max_depth levels deep looking for PDF doc links.

    Uses requests first, then falls back to Playwright for subpages that
    look like they should have docs but don't (JS-rendered content).

    Returns (doc_links, errors).
    """
    doc_links: list[dict] = []
    errors: list[str] = []
    seen_doc_urls: set[str] = set()
    visited: set[str] = set()
    # Track subpages that had no PDFs (candidates for Playwright retry)
    js_retry_candidates: list[str] = []

    # BFS queue: (url, depth)
    queue: list[tuple[str, int]] = [(start_url, 0)]

    while queue:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        try:
            resp = session.get(url, timeout=30, allow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            errors.append(f"Fetch failed: {url}: {exc}")
            continue

        content_type = resp.headers.get("content-type", "").lower()
        if "html" not in content_type:
            if "pdf" in content_type and url not in seen_doc_urls:
                seen_doc_urls.add(url)
                doc_links.append({"url": url, "text": "direct PDF", "keyword_match": False})
            continue

        # Find PDFs on this page
        page_pdfs = find_doc_links(url, resp.text)
        new_on_page = 0
        for link in page_pdfs:
            if link["url"] not in seen_doc_urls:
                seen_doc_urls.add(link["url"])
                doc_links.append(link)
                new_on_page += 1

        # Follow subpage links if we haven't hit max depth
        if depth < max_depth:
            subpages = find_subpage_links(url, resp.text)
            for subpage_url in subpages:
                if subpage_url not in visited:
                    queue.append((subpage_url, depth + 1))

        # If this is a doc-related subpage but found no NEW PDFs,
        # it's probably JS-rendered — queue for Playwright retry
        if depth > 0 and new_on_page == 0:
            url_path = unquote(urlparse(url).path).lower()
            if SUBPAGE_KEYWORDS.search(url_path):
                js_retry_candidates.append(url)

    # Playwright fallback for JS-rendered doc pages
    if js_retry_candidates and HAS_PLAYWRIGHT:
        for url in js_retry_candidates[:3]:  # limit retries
            html = fetch_with_playwright(url)
            if html:
                for link in find_doc_links(url, html):
                    if link["url"] not in seen_doc_urls:
                        seen_doc_urls.add(link["url"])
                        doc_links.append(link)

    return doc_links, errors


def scrape_hoa(record: dict, session: requests.Session, dry_run: bool) -> dict:
    """Scrape one HOA's website(s) for governing documents."""
    name = record["name"]
    slug = slugify(name)
    urls = record.get("urls", [])
    result = {
        "name": name,
        "slug": slug,
        "urls_tried": urls,
        "status": "no_urls",
        "doc_links": [],
        "downloaded": [],
        "errors": [],
    }

    if not urls:
        return result

    for url in urls:
        if is_login_portal(url):
            result["status"] = "login_required"
            result["errors"].append(f"Login portal: {url}")
            continue

        links, errors = crawl_for_docs(url, session, max_depth=2)
        result["doc_links"].extend(links)
        result["errors"].extend(errors)

    if not result["doc_links"]:
        if result["status"] != "login_required":
            result["status"] = "no_docs_found"
        return result

    if dry_run:
        result["status"] = "dry_run"
        return result

    # Download PDFs
    target_dir = DOCS_DIR / slug
    target_dir.mkdir(parents=True, exist_ok=True)

    for link in result["doc_links"]:
        doc_url = link["url"]
        filename = sanitize_filename(doc_url)
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
    return result


def generate_import(records: list[dict], scrape_results: dict[str, dict]) -> None:
    """Generate import.json for bulk upload."""
    import_records = []

    for record in records:
        name = record["name"]
        metadata_type = TYPE_MAP.get(record.get("type", ""), "hoa")
        urls = record.get("urls", [])
        website_url = urls[0] if urls else None

        import_rec = {
            "name": name,
            "metadata_type": metadata_type,
            "city": record.get("city", ""),
            "state": "TX",
            "postal_code": record.get("zip", ""),
            "source": "trec_hoa_management_certificates",
        }
        if website_url:
            import_rec["website_url"] = website_url

        import_records.append(import_rec)

    IMPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "trec_hoa_management_certificates",
        "records": import_records,
    }
    with open(IMPORT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(import_records)} records → {IMPORT_PATH}")


def main():
    parser = argparse.ArgumentParser(description="Scrape governing docs from TREC HOA websites")
    parser.add_argument("--limit", type=int, default=0, help="Process only first N HOAs (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="Find links but don't download")
    parser.add_argument("--workers", type=int, default=5, help="Scraping concurrency (default: 5)")
    parser.add_argument("--only-with-urls", action="store_true",
                        help="Only process HOAs that have extracted URLs")
    args = parser.parse_args()

    if not URLS_PATH.exists():
        print(f"ERROR: {URLS_PATH} not found. Run trec_extract_urls.py first.", file=sys.stderr)
        sys.exit(1)

    records = []
    with open(URLS_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    if args.only_with_urls:
        records = [r for r in records if r.get("urls")]

    if args.limit > 0:
        records = records[:args.limit]

    print(f"Loaded {len(records)} HOAs")
    with_urls = sum(1 for r in records if r.get("urls"))
    print(f"  {with_urls} have website URLs")

    session = requests.Session()
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (compatible; HOAproxy-scraper/1.0; +https://hoaproxy.org)"
    )

    scrape_results: dict[str, dict] = {}
    done = 0
    total = len(records)
    docs_total = 0

    def _scrape(record):
        return record["name"], scrape_hoa(record, session, args.dry_run)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_scrape, r): r for r in records}
        for future in as_completed(futures):
            name, result = future.result()
            scrape_results[name] = result
            done += 1
            n_docs = len(result.get("downloaded", []))
            docs_total += n_docs
            if n_docs > 0:
                print(f"  [{done}/{total}] {name}: {n_docs} docs")
            elif done % 100 == 0:
                print(f"  [{done}/{total}] progress...")

    # Summary
    statuses = {}
    for r in scrape_results.values():
        s = r["status"]
        statuses[s] = statuses.get(s, 0) + 1

    print(f"\n=== Summary ===")
    print(f"Total HOAs: {total}")
    for s, c in sorted(statuses.items()):
        print(f"  {s}: {c}")
    print(f"Total documents downloaded: {docs_total}")

    # Write scrape report
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w") as f:
        json.dump(
            {"total": total, "statuses": statuses, "results": list(scrape_results.values())},
            f, indent=2,
        )
    print(f"Report → {REPORT_PATH}")

    # Generate import manifest
    generate_import(records, scrape_results)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
