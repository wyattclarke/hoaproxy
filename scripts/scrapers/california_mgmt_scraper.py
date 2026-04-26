"""Scrape CC&Rs from California HOA management company websites.

Reads hoa_details.jsonl (from scrape_california_hoa.py), filters to HOAs
with a management company website, crawls each site for governing doc PDFs.

Usage:
    python scripts/scrapers/california_mgmt_scraper.py                  # all
    python scripts/scrapers/california_mgmt_scraper.py --limit 20       # first 20
    python scripts/scrapers/california_mgmt_scraper.py --dry-run        # find links only
    python scripts/scrapers/california_mgmt_scraper.py --workers 3      # concurrency

Resumable: skips HOAs already in scrape_report.json on restart.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

# Reuse doc-finding patterns from the Texas scraper
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trec_scrape_docs import (
    DOC_KEYWORDS,
    SUBPAGE_KEYWORDS,
    LOGIN_PORTALS,
    find_doc_links,
    find_subpage_links,
    crawl_for_docs,
    is_login_portal as _is_login_portal,
    sanitize_filename,
)

# California-specific management portals that don't host public docs
CA_LOGIN_PORTALS = {
    "fsresidential.com",
    "managementtrust.com",
}


def is_login_portal(url: str) -> bool:
    """Check against both Texas and California portal blocklists."""
    if _is_login_portal(url):
        return True
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    for portal in CA_LOGIN_PORTALS:
        if portal in host:
            stripped = host.removesuffix(portal).rstrip(".")
            if stripped == "" or stripped == "www":
                return True
    return False
from scrapers.base import title_case_name, write_import_file

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data" / "california"
DETAILS_PATH = DATA_DIR / "hoa_details.jsonl"
DOCS_DIR = ROOT / "scraped_hoa_docs" / "california"
REPORT_PATH = DATA_DIR / "scrape_report.json"
IMPORT_PATH = DATA_DIR / "import.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}


def slugify(name: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")


def normalize_url(raw: str) -> str | None:
    """Turn 'Https://www.fsresidential.com' into a proper URL."""
    raw = raw.strip()
    if not raw:
        return None
    if not raw.lower().startswith(("http://", "https://")):
        raw = "http://" + raw
    # Fix mixed case scheme
    if raw.startswith("Https://"):
        raw = "https://" + raw[8:]
    elif raw.startswith("Http://"):
        raw = "http://" + raw[7:]
    return raw


def load_hoa_records(limit: int | None = None) -> list[dict]:
    """Load HOA records that have a management company website."""
    if not DETAILS_PATH.exists():
        print(f"Error: {DETAILS_PATH} not found. Run scrape_california_hoa.py first.")
        sys.exit(1)

    records = []
    with open(DETAILS_PATH) as f:
        for line in f:
            rec = json.loads(line)
            url = normalize_url(rec.get("pm_website", ""))
            if url:
                rec["_pm_url"] = url
                records.append(rec)

    print(f"Loaded {len(records)} HOAs with management company websites")
    if limit:
        records = records[:limit]
    return records


def scrape_hoa(record: dict, session: requests.Session, dry_run: bool) -> dict:
    """Scrape one HOA's management company website for governing documents."""
    hoa_id = record["id"]
    name = record.get("legal_name", record.get("aka", f"HOA_{hoa_id}"))
    slug = slugify(name)
    pm_url = record["_pm_url"]
    pm_company = record.get("pm_company", "")

    result = {
        "id": hoa_id,
        "name": name,
        "slug": slug,
        "pm_company": pm_company,
        "pm_url": pm_url,
        "status": "pending",
        "doc_links": [],
        "downloaded": [],
        "errors": [],
    }

    if is_login_portal(pm_url):
        result["status"] = "login_required"
        result["errors"].append(f"Login portal: {pm_url}")
        return result

    # Crawl the management company website for doc PDFs
    links, errors = crawl_for_docs(pm_url, session, max_depth=2)
    result["doc_links"] = links
    result["errors"] = errors

    if not links:
        result["status"] = "no_docs_found"
        return result

    if dry_run:
        result["status"] = "dry_run"
        return result

    # Download PDFs
    target_dir = DOCS_DIR / slug
    target_dir.mkdir(parents=True, exist_ok=True)

    for link in links:
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
    """Generate import.json for bulk upload to the site."""
    import_records = []

    for record in records:
        hoa_id = record["id"]
        result = scrape_results.get(hoa_id)
        if not result or result["status"] != "ok":
            continue

        name = record.get("legal_name", record.get("aka", ""))
        import_rec = {
            "name": title_case_name(name) if name == name.upper() else name,
            "metadata_type": "hoa",
            "city": record.get("city", ""),
            "state": "CA",
            "county": record.get("county", ""),
            "source": "california_hoa_mgmt_scrape",
            "documents": [str(p) for p in result["downloaded"]],
        }

        pm = record.get("pm_company", "")
        if pm:
            import_rec["management_company"] = pm
        pm_url = record.get("_pm_url", "")
        if pm_url:
            import_rec["website_url"] = pm_url

        import_records.append(import_rec)

    write_import_file(import_records, "california_hoa_mgmt_scrape", IMPORT_PATH)


def main():
    parser = argparse.ArgumentParser(
        description="Scrape CC&Rs from California HOA management company websites"
    )
    parser.add_argument("--limit", type=int, help="Limit to first N HOAs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Find doc links but don't download")
    parser.add_argument("--workers", type=int, default=3,
                        help="Number of concurrent workers")
    args = parser.parse_args()

    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    records = load_hoa_records(limit=args.limit)

    # Load existing report for resumability
    existing_results: dict[str, dict] = {}
    if REPORT_PATH.exists():
        with open(REPORT_PATH) as f:
            report = json.load(f)
            for r in report.get("results", []):
                existing_results[r["id"]] = r
        print(f"  Loaded {len(existing_results)} existing results from report")

    todo = [r for r in records if r["id"] not in existing_results]
    print(f"  {len(todo)} remaining to scrape")

    if not todo:
        print("  Nothing to do.")
        return

    all_results = dict(existing_results)
    completed = len(existing_results)

    def process(record: dict) -> dict:
        session = requests.Session()
        session.headers.update(HEADERS)
        return scrape_hoa(record, session, args.dry_run)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process, r): r for r in todo}
        for future in as_completed(futures):
            record = futures[future]
            try:
                result = future.result()
                all_results[result["id"]] = result
                completed += 1
                status = result["status"]
                n_docs = len(result.get("downloaded", []) or result.get("doc_links", []))
                print(f"  [{completed}/{len(records)}] {result['name'][:50]}: "
                      f"{status} ({n_docs} docs)")
            except Exception as e:
                print(f"  {record['id']} failed: {e}")

            # Save report periodically
            if completed % 50 == 0:
                _save_report(all_results)

    _save_report(all_results)

    # Generate import file
    generate_import(records, all_results)

    # Summary
    statuses = {}
    for r in all_results.values():
        s = r.get("status", "unknown")
        statuses[s] = statuses.get(s, 0) + 1
    print(f"\nDone. {len(all_results)} HOAs processed:")
    for status, count in sorted(statuses.items(), key=lambda x: -x[1]):
        print(f"  {status}: {count}")


def _save_report(results: dict[str, dict]) -> None:
    report = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "results": list(results.values()),
    }
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)


if __name__ == "__main__":
    main()
