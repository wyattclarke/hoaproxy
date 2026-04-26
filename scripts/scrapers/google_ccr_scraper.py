"""Search for CC&R PDFs hosted on public websites for HOAs in any supported state.

Uses the Serper.dev Google Search API to find governing document PDFs for each
HOA, then downloads matches. Post-download validation extracts first-page text
and rejects junk PDFs (court filings, IRS 990s, government docs, newspapers, etc.).

Supported states:
    ca  — reads data/california/hoa_details.jsonl (from scrape_california_hoa.py)
    co  — reads data/colorado_hoa_active.csv (SoS registry, deduped by Credential Number)

Setup:
    1. Get a free API key at https://serper.dev/ (no credit card required)
    2. Add to settings.env: SERPER_API_KEY=...

Usage:
    python scripts/scrapers/google_ccr_scraper.py --state ca                # all CA
    python scripts/scrapers/google_ccr_scraper.py --state co                # all CO
    python scripts/scrapers/google_ccr_scraper.py --state co --limit 50     # first 50
    python scripts/scrapers/google_ccr_scraper.py --state ca --dry-run      # find links only

Resumable: skips HOAs already in data/{state}/google_scrape_report.json on restart.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests

# Reuse doc-finding patterns from the Texas scraper
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trec_scrape_docs import DOC_KEYWORDS, sanitize_filename, find_doc_links

from scrapers.base import title_case_name, write_import_file

ROOT = Path(__file__).resolve().parent.parent.parent

MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB — matches upload pipeline limit

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Domains to skip entirely — login portals, generic info, paywalled
SKIP_DOMAINS = {
    # Management portals (login-walled)
    "appfolio.com", "buildium.com", "townsq.io", "connectresident.com",
    "frontsteps.com", "pilera.com", "fsresidential.com", "managementtrust.com",
    "ciraconnect.com", "homewisedocs.com", "ciranet.com",
    # Real estate / social
    "youtube.com", "facebook.com", "linkedin.com", "yelp.com",
    "nextdoor.com", "zillow.com", "realtor.com", "redfin.com",
    "trulia.com", "loopnet.com",
    # Generic HOA info (not actual CC&R documents)
    "nolo.com", "findhoalaw.com", "hoamanagement.com", "spectrumam.com",
    "condocontrol.com", "elireport.com", "uslegalforms.com",
    "hopb.co", "freemansconstruction.com", "allcounty.com",
    # Court / legal databases
    "courtlistener.com", "law.justia.com", "casetext.com",
    "uscourts.gov", "pacer.gov",
    # IRS / tax filings
    "propublica.org",
    # Government sites that host irrelevant PDFs
    "waterboards.ca.gov", "ca.gov/eir", "sdcounty.ca.gov",
    # MLS / listing services
    "crmls.org",
    # Newspapers
    "newspapers.com", "latimes.com",
}

# Junk indicators in first-page text — if any match, reject the PDF
JUNK_PATTERNS = re.compile(
    r'bankruptcy\s+court|united\s+states\s+district\s+court|'
    r'office\s+of\s+administrative\s+hearings|'
    r'plaintiff|defendant|docket\s+no|case\s+no\.?\s*\d|'
    r'return\s+of\s+organization\s+exempt|form\s+990|'
    r'internal\s+revenue\s+service|exempt\s+organization|'
    r'tax\s+default.*delinquent|treasurer-tax\s+collector|'
    r'notice\s+of\s+tax\s+default|'
    r'planning\s+commission\s+meeting|city\s+council\s+agenda|'
    r'urban\s+water\s+management\s+plan|water\s+district|'
    r'hazard\s+mitigation\s+plan|'
    r'environmental\s+impact\s+report|'
    r'senate\s+third\s+reading\s+packet|senate\s+floor\s+analyses|'
    r'registrar\s+of\s+voters|'
    r'educational\s+foundation|'
    r'neighborhood\s+organization\s+directory|'
    r'land\s+input\s+form.*crmls|'
    r'millcreek\s+planning\s+commission|'
    r'division\s+of\s+water\s+rights|'
    r'city\s+council\s+\w+day|agenda\s+\w+\s+city\s+council|'
    r'public\s+hearing\s+may|appeal\s+of\s+the\s+planning\s+commission|'
    r'zoning\s+code\s+update|'
    r'department\s+of\s+housing\s+and\s+community\s+development|'
    r'serving\s+\w+.*since\s+\d{4}.*community\s+weekl',
    re.IGNORECASE,
)

# Governing doc indicators — if any match, definitely keep
GOOD_PATTERNS = re.compile(
    r'declaration\s+of\s+(?:protective\s+)?covenants|'
    r'covenants,?\s+conditions,?\s+and\s+restrictions|'
    r'cc\s*&\s*r|ccr|'
    r'deed\s+restrict|restrictive\s+covenant|'
    r'by-?laws?\s+of|articles\s+of\s+incorporation|'
    r'rules\s+and\s+regulations|'
    r'architectural\s+(?:guidelines?|rules?|standards?|review)|'
    r'design\s+guidelines?|construction\s+manual|'
    r'community\s+guidelines?|'
    r'supplemental\s+declaration|amended\s+and\s+restated|'
    r'management\s+certif|'
    r'resolution\s+(?:no\.?|of\s+the\s+board)',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# State-specific loaders
# ---------------------------------------------------------------------------
# Each loader returns a list of dicts with at least: id, legal_name, city, county
# and optionally: pm_company, aka, zip, units, state

def _load_california(limit: int | None = None) -> tuple[list[dict], str]:
    """Load CA HOA records from hoa_details.jsonl."""
    details_path = ROOT / "data" / "california" / "hoa_details.jsonl"
    if not details_path.exists():
        print(f"Error: {details_path} not found. Run scrape_california_hoa.py first.")
        sys.exit(1)

    records = []
    with open(details_path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    print(f"Loaded {len(records)} CA HOA records")
    if limit:
        records = records[:limit]
    return records, "CA"


def _load_colorado(limit: int | None = None) -> tuple[list[dict], str]:
    """Load CO HOA records from colorado_hoa_active.csv, deduplicated by Credential Number."""
    csv_path = ROOT / "data" / "colorado_hoa_active.csv"
    if not csv_path.exists():
        print(f"Error: {csv_path} not found.")
        sys.exit(1)

    seen_ids: dict[str, dict] = {}  # credential_number -> record
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cred = row.get("Credential Number", "").strip()
            if not cred:
                continue
            # Keep first occurrence per credential number
            if cred in seen_ids:
                continue
            name = row.get("BusinessName", "").strip()
            if not name:
                continue
            seen_ids[cred] = {
                "id": f"CO-{cred}",
                "legal_name": name,
                "city": row.get("City", "").strip(),
                "county": row.get("County", "").strip(),
                "zip": row.get("ZipCode", "").strip(),
                "pm_company": row.get("Management Company", "").strip(),
                "units": row.get("Units", "").strip(),
            }

    records = list(seen_ids.values())
    print(f"Loaded {len(records)} unique CO HOAs (deduped from CSV by Credential Number)")
    if limit:
        records = records[:limit]
    return records, "CO"


STATE_LOADERS = {
    "ca": _load_california,
    "co": _load_colorado,
}


# ---------------------------------------------------------------------------
# Core logic (state-agnostic)
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")


def should_skip_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    for domain in SKIP_DOMAINS:
        if domain in host:
            return True
    return False


def is_likely_ccr(url: str, snippet: str) -> bool:
    url_lower = unquote(urlparse(url).path).lower()
    text = (snippet + " " + url_lower).lower()
    return bool(DOC_KEYWORDS.search(text))


class _PdfTimeout(Exception):
    pass


def _timeout_handler(signum, frame):
    raise _PdfTimeout()


def validate_pdf(pdf_path: Path, hoa_name: str) -> str | None:
    """Extract first-page text and classify. Returns rejection reason or None if OK."""
    try:
        from pdfminer.high_level import extract_text
        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(30)  # 30s max for pdfminer
        try:
            text = extract_text(str(pdf_path), page_numbers=[0], maxpages=1)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
    except _PdfTimeout:
        return "pdfminer timeout (>30s)"
    except Exception:
        text = ""

    if not text or not text.strip():
        return None

    text = text.strip()

    if GOOD_PATTERNS.search(text):
        return None

    m = JUNK_PATTERNS.search(text)
    if m:
        return f"junk: {m.group()[:50]}"

    return None


def build_search_query(name: str) -> str:
    clean = name.strip()
    for suffix in [", INC", ", INC.", " INC", " INC."]:
        if clean.upper().endswith(suffix):
            clean = clean[: -len(suffix)].strip()
    return f'"{clean}" filetype:pdf CC&R OR declaration OR covenant OR bylaws'


def serper_search(query: str, api_key: str, session: requests.Session) -> list[dict]:
    last_exc = None
    for attempt in range(3):
        try:
            resp = session.post(
                "https://google.serper.dev/search",
                json={"q": query, "num": 20},
                headers={
                    "X-API-KEY": api_key,
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            if resp.status_code == 429:
                return [{"error": "rate_limited"}]
            if resp.status_code in (401, 403):
                return [{"error": "invalid_api_key"}]
            resp.raise_for_status()
            break  # success
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError) as exc:
            last_exc = exc
            # Reset the session's connection pool and retry with backoff
            session.close()
            time.sleep(2 ** attempt)  # 1s, 2s, 4s
            continue
        except Exception as exc:
            return [{"error": str(exc)}]
    else:
        return [{"error": str(last_exc)}]

    data = resp.json()

    try:
        from hoaware.cost_tracker import log_serper_usage
        log_serper_usage(queries=1)
    except Exception:
        pass

    results = []
    for item in data.get("organic", []):
        results.append({
            "url": item.get("link", ""),
            "title": item.get("title", ""),
            "snippet": item.get("snippet", ""),
        })

    return results


def scrape_hoa(record: dict, api_key: str, http_session: requests.Session,
               dry_run: bool, docs_dir: Path) -> dict:
    """Search for one HOA's CC&R documents and download matches."""
    hoa_id = record["id"]
    name = record.get("legal_name", record.get("aka", f"HOA_{hoa_id}"))
    slug = slugify(name)

    result = {
        "id": hoa_id,
        "name": name,
        "slug": slug,
        "query": "",
        "status": "pending",
        "search_results": [],
        "pdf_links": [],
        "downloaded": [],
        "rejected": [],
        "errors": [],
    }

    query = build_search_query(name)
    result["query"] = query

    time.sleep(0.5)

    search_results = serper_search(query, api_key, http_session)

    if search_results and "error" in search_results[0]:
        error = search_results[0]["error"]
        result["errors"].append(f"Search error: {error}")
        result["status"] = "rate_limited" if "rate_limited" in error else "search_error"
        return result

    result["search_results"] = search_results

    pdf_links = []
    seen_pdf_urls = set()
    doc_pages_to_crawl = []

    for sr in search_results:
        url = sr.get("url", "")
        if not url or should_skip_url(url):
            continue

        url_lower = url.lower()
        snippet = sr.get("snippet", "") + " " + sr.get("title", "")

        if ".pdf" in url_lower:
            if url not in seen_pdf_urls:
                seen_pdf_urls.add(url)
                pdf_links.append({
                    "url": url,
                    "title": sr.get("title", ""),
                    "keyword_match": is_likely_ccr(url, snippet),
                })
        else:
            combined = (snippet + " " + url_lower).lower()
            if DOC_KEYWORDS.search(combined) or "hoa" in combined:
                doc_pages_to_crawl.append(url)

    for page_url in doc_pages_to_crawl[:3]:
        try:
            resp = http_session.get(page_url, timeout=30, allow_redirects=True)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "").lower()
            if "html" not in ct:
                continue
            page_pdfs = find_doc_links(page_url, resp.text)
            for link in page_pdfs:
                if link["url"] not in seen_pdf_urls:
                    seen_pdf_urls.add(link["url"])
                    pdf_links.append(link)
        except Exception as exc:
            result["errors"].append(f"Page crawl failed: {page_url}: {exc}")

    pdf_links.sort(key=lambda x: (not x.get("keyword_match", False), x["url"]))
    result["pdf_links"] = pdf_links

    if not pdf_links:
        result["status"] = "no_docs_found"
        return result

    if dry_run:
        result["status"] = "dry_run"
        return result

    target_dir = docs_dir / slug
    target_dir.mkdir(parents=True, exist_ok=True)

    for link in pdf_links:
        doc_url = link["url"]
        filename = sanitize_filename(doc_url)
        dest = target_dir / filename

        if dest.exists() and dest.stat().st_size > 0:
            if dest.stat().st_size > MAX_PDF_BYTES:
                result["rejected"].append(f"{filename}: too large ({dest.stat().st_size // 1024 // 1024}MB)")
                dest.unlink()
                continue
            reason = validate_pdf(dest, name)
            if reason:
                result["rejected"].append(f"{filename}: {reason}")
                dest.unlink()
                continue
            result["downloaded"].append(str(dest))
            continue

        try:
            # Check Content-Length before downloading
            try:
                head = http_session.head(doc_url, timeout=15, allow_redirects=True)
                cl = int(head.headers.get("content-length", 0))
                if cl > MAX_PDF_BYTES:
                    result["errors"].append(f"Too large ({cl // 1024 // 1024}MB): {doc_url}")
                    continue
            except Exception:
                pass  # HEAD failed — proceed with GET

            resp = http_session.get(doc_url, timeout=120, allow_redirects=True)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").lower()
            if "pdf" not in content_type and "octet" not in content_type:
                result["errors"].append(f"Not a PDF: {doc_url} ({content_type})")
                continue
            if len(resp.content) < 1000:
                result["errors"].append(f"Too small ({len(resp.content)} bytes): {doc_url}")
                continue
            if len(resp.content) > MAX_PDF_BYTES:
                result["errors"].append(f"Too large ({len(resp.content) // 1024 // 1024}MB): {doc_url}")
                continue
            dest.write_bytes(resp.content)

            reason = validate_pdf(dest, name)
            if reason:
                result["rejected"].append(f"{filename}: {reason}")
                dest.unlink()
                continue

            result["downloaded"].append(str(dest))
        except Exception as exc:
            result["errors"].append(f"Download failed: {doc_url}: {exc}")

    if target_dir.exists() and not any(target_dir.iterdir()):
        target_dir.rmdir()

    result["status"] = "ok" if result["downloaded"] else "download_failed"
    return result


def generate_import(records: list[dict], scrape_results: dict[str, dict],
                    state_code: str, import_path: Path) -> None:
    """Generate import.json for bulk upload to the site."""
    source = f"{state_code.lower()}_google_ccr_scrape"
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
            "state": state_code,
            "county": record.get("county", ""),
            "source": source,
            "documents": [str(p) for p in result["downloaded"]],
        }

        pm = record.get("pm_company", "")
        if pm:
            import_rec["management_company"] = pm

        import_records.append(import_rec)

    write_import_file(import_records, source, import_path)


def _save_report(results: dict[str, dict], report_path: Path) -> None:
    report = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "results": list(results.values()),
    }
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Search for HOA CC&R PDFs via Serper.dev (multi-state)"
    )
    parser.add_argument("--state", required=True, choices=sorted(STATE_LOADERS.keys()),
                        help="State to scrape (e.g. ca, co)")
    parser.add_argument("--limit", type=int, help="Limit to first N HOAs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Find doc links but don't download")
    args = parser.parse_args()

    state_key = args.state.lower()
    state_code = state_key.upper()

    # Paths scoped to state
    data_dir = ROOT / "data" / {
        "ca": "california",
        "co": "colorado",
    }.get(state_key, state_key)
    docs_dir = ROOT / "scraped_hoa_docs" / {
        "ca": "california",
        "co": "colorado",
    }.get(state_key, state_key)
    report_path = data_dir / "google_scrape_report.json"
    import_path = data_dir / "google_import.json"

    # Load API key
    from dotenv import load_dotenv
    load_dotenv(ROOT / "settings.env")
    api_key = os.environ.get("SERPER_API_KEY", "")
    if not api_key:
        print("Error: SERPER_API_KEY not set in settings.env")
        print("Get a free key at https://serper.dev/ (no credit card)")
        print("Then add to settings.env: SERPER_API_KEY=...")
        sys.exit(1)

    data_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    # Load HOA records for this state
    loader = STATE_LOADERS[state_key]
    records, state_code = loader(limit=args.limit)

    # Load existing report for resumability
    existing_results: dict[str, dict] = {}
    if report_path.exists():
        with open(report_path) as f:
            report = json.load(f)
            for r in report.get("results", []):
                existing_results[r["id"]] = r
        print(f"  Loaded {len(existing_results)} existing results from report")

    todo = [r for r in records if r["id"] not in existing_results]
    print(f"  {len(todo)} remaining to search")

    if not todo:
        print("  Nothing to do.")
        return

    all_results = dict(existing_results)
    completed = len(existing_results)

    http_session = requests.Session()
    http_session.headers.update(HEADERS)

    for record in todo:
        try:
            result = scrape_hoa(record, api_key, http_session, args.dry_run, docs_dir)
            all_results[result["id"]] = result
            completed += 1
            status = result["status"]
            n_docs = len(result.get("downloaded", []) or result.get("pdf_links", []))
            n_rejected = len(result.get("rejected", []))
            extra = f", {n_rejected} rejected" if n_rejected else ""
            print(f"  [{completed}/{len(records)}] {result['name'][:50]}: "
                  f"{status} ({n_docs} docs{extra})")

            if status == "rate_limited":
                print("\n  Rate limited — stopping. Re-run to resume.")
                break

            if status == "search_error":
                err = result["errors"][0] if result["errors"] else ""
                if "invalid_api_key" in err:
                    print("\n  Invalid API key — check SERPER_API_KEY in settings.env")
                    break

        except Exception as e:
            print(f"  {record['id']} failed: {e}")

        if completed % 25 == 0:
            _save_report(all_results, report_path)

    _save_report(all_results, report_path)

    # Generate import file
    generate_import(records, all_results, state_code, import_path)

    # Summary
    statuses = {}
    total_rejected = 0
    for r in all_results.values():
        s = r.get("status", "unknown")
        statuses[s] = statuses.get(s, 0) + 1
        total_rejected += len(r.get("rejected", []))
    print(f"\nDone. {len(all_results)} {state_code} HOAs processed:")
    for status, count in sorted(statuses.items(), key=lambda x: -x[1]):
        print(f"  {status}: {count}")
    if total_rejected:
        print(f"  ({total_rejected} junk PDFs rejected)")


if __name__ == "__main__":
    main()
