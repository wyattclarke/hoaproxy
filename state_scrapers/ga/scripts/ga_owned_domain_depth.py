#!/usr/bin/env python3
"""Owned-domain depth pass for GA HOAs already in the bank.

For every banked GA manifest that has `website` set and fewer than
TARGET_PDF_COUNT PDFs:

1. Fetch the homepage and a few /documents, /governing-documents,
   /covenants, /bylaws, /resources, /forms style sub-pages.
2. Extract every linked PDF URL.
3. Whitelist by URL + anchor text (must mention a governing-doc
   keyword; must NOT mention junk like newsletter/minutes/budget).
4. Build a Lead with website=None, pre_discovered_pdf_urls=<whitelisted>,
   and re-probe via hoaware.discovery.probe.probe(). Bank dedup is
   sha-keyed, so already-banked PDFs are no-ops; new ones land under
   the existing manifest path and increase the per-HOA depth.

No model calls — just deterministic URL/anchor filtering.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from google.cloud import storage as gcs  # noqa: E402

from hoaware.discovery.leads import Lead  # noqa: E402
from hoaware.discovery.probe import probe  # noqa: E402

BUCKET_NAME = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")
USER_AGENT = (
    os.environ.get("HOA_DISCOVERY_USER_AGENT")
    or "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
)
RESPECT_ROBOTS = os.environ.get("HOA_DISCOVERY_RESPECT_ROBOTS", "0") in {"1", "true", "True"}
PAGE_TIMEOUT = 12

GOVERNING_HINT_RE = re.compile(
    r"(declaration|covenants?|cc&?rs?|bylaws?|by-laws?|articles?\s+of\s+incorporation|"
    r"amendment|restated|rules?\s*(?:and|&)?\s*regulations?|"
    r"architectural\s+(?:guidelines?|standards?|control)|design\s+guidelines?|"
    r"governing\s+documents?|charter)",
    re.IGNORECASE,
)
JUNK_PDF_RE = re.compile(
    r"(newsletter|minutes?|agenda|budget|financial|audit|reserve\s+study|"
    r"rental|lease|pool[-_ ](pass|rules)|directory|roster|violation|estoppel|"
    r"closing|coupon|listing|reminder|history|estate[-_ ]sale|application[-_ ]form|"
    r"payment|invoice|fee[-_ ]schedule|election|nomination|welcome[-_ ]packet|"
    r"meeting|annual[-_ ]report|treasurer|notice|map|plat|brochure|flyer|forms?-?\d|"
    r"directory|membership[-_ ]list|ballot|proxy[-_ ]form|sign[-_ ]?up|registration)",
    re.IGNORECASE,
)
DOC_PAGE_HINTS = [
    "documents", "governing-documents", "governing_documents",
    "covenants", "bylaws", "by-laws", "ccrs", "ccr",
    "association-documents", "hoa-documents", "rules", "regulations",
    "architectural", "design-guidelines",
    "resources", "downloads",
]


def is_pdf(url: str) -> bool:
    clean = url.lower().split("?", 1)[0].split("#", 1)[0]
    return clean.endswith(".pdf") or "format=pdf" in url.lower()


def normalize_url(base: str, href: str) -> str | None:
    try:
        absolute = urljoin(base, href).split("#", 1)[0]
    except Exception:
        return None
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return absolute


def robots_allowed(session: requests.Session, url: str) -> bool:
    if not RESPECT_ROBOTS:
        return True
    parsed = urlparse(url)
    parser = RobotFileParser(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
    try:
        response = session.get(parser.url, headers={"User-Agent": USER_AGENT}, timeout=5, allow_redirects=True)
        if response.status_code >= 400:
            return True
        parser.parse(response.text.splitlines())
        return parser.can_fetch(USER_AGENT, url)
    except requests.RequestException:
        return True


def fetch_page(session: requests.Session, url: str) -> tuple[str, str] | None:
    if not robots_allowed(session, url):
        return None
    try:
        response = session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=PAGE_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    ctype = (response.headers.get("Content-Type") or "").lower()
    if "html" not in ctype and "text" not in ctype:
        return None
    return response.url, response.text[:600_000]


def extract_links(base_url: str, html: str) -> list[tuple[str, str]]:
    """Return [(absolute_url, anchor_text)] for every <a href> on the page."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        absolute = normalize_url(base_url, href)
        if not absolute:
            continue
        anchor = " ".join((a.get_text(" ") or "").split())[:200]
        out.append((absolute, anchor))
    return out


def find_doc_subpages(base_url: str, html: str) -> list[str]:
    """Find sub-pages on the same site whose path/anchor hints at a docs page."""
    parsed = urlparse(base_url)
    base_host = parsed.netloc.lower()
    candidates: list[str] = []
    for absolute, anchor in extract_links(base_url, html):
        ap = urlparse(absolute)
        if ap.netloc.lower() != base_host:
            continue
        if is_pdf(absolute):
            continue
        path_low = ap.path.lower()
        anchor_low = anchor.lower()
        if any(hint in path_low for hint in DOC_PAGE_HINTS) or any(hint in anchor_low for hint in DOC_PAGE_HINTS):
            candidates.append(absolute)
    seen: set[str] = set()
    deduped: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped[:8]  # cap so we never crawl the whole site


def whitelist_pdf(url: str, anchor: str) -> bool:
    if not is_pdf(url):
        return False
    blob = f"{url} {anchor}"
    if JUNK_PDF_RE.search(blob):
        return False
    return bool(GOVERNING_HINT_RE.search(blob))


def collect_governing_pdfs(session: requests.Session, website: str) -> tuple[list[str], dict]:
    """Return ([governing pdf urls], {audit info})."""
    audit: dict = {"website": website, "pages_fetched": [], "pdfs_seen": 0, "pdfs_kept": 0}
    pages_to_try: list[str] = [website]
    visited: set[str] = set()
    pdfs: dict[str, str] = {}  # url -> anchor

    home = fetch_page(session, website)
    if not home:
        audit["error"] = "homepage_fetch_failed"
        return [], audit
    final_url, html = home
    visited.add(final_url)
    audit["pages_fetched"].append(final_url)

    for link_url, anchor in extract_links(final_url, html):
        if is_pdf(link_url):
            pdfs.setdefault(link_url, anchor)
    pages_to_try.extend(find_doc_subpages(final_url, html))

    for sub in pages_to_try[1:]:
        if sub in visited:
            continue
        page = fetch_page(session, sub)
        if not page:
            continue
        sub_final, sub_html = page
        visited.add(sub_final)
        audit["pages_fetched"].append(sub_final)
        for link_url, anchor in extract_links(sub_final, sub_html):
            if is_pdf(link_url):
                pdfs.setdefault(link_url, anchor)

    audit["pdfs_seen"] = len(pdfs)
    kept = [u for u, a in pdfs.items() if whitelist_pdf(u, a)]
    audit["pdfs_kept"] = len(kept)
    audit["kept_urls"] = kept[:20]
    return kept[:25], audit


def list_ga_manifests(client: gcs.Client) -> list[gcs.Blob]:
    bucket = client.bucket(BUCKET_NAME)
    return [b for b in client.list_blobs(bucket, prefix="v1/GA/") if b.name.endswith("/manifest.json")]


def already_banked_urls(manifest: dict) -> set[str]:
    out: set[str] = set()
    for doc in manifest.get("documents") or []:
        u = doc.get("source_url")
        if u:
            out.add(u.strip())
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Owned-domain depth pass for GA HOAs")
    parser.add_argument("--target-pdfs", type=int, default=4,
                        help="Skip manifests that already have at least this many PDFs.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--per-lead-timeout", type=int, default=120)
    parser.add_argument("--probe-delay", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "hoaware"

    session = requests.Session()
    client = gcs.Client()
    manifests = list_ga_manifests(client)
    print(f"Found {len(manifests)} GA manifests", file=sys.stderr)

    summary: dict[str, int] = {}
    processed = 0
    for blob in manifests:
        if args.limit and processed >= args.limit:
            break
        try:
            data = json.loads(blob.download_as_bytes())
        except Exception:
            continue
        website = (data.get("website") or {}).get("homepage_url") or data.get("website_url")
        # Older manifests stored website at top-level "website" dict OR not at all.
        if not website:
            metadata_sources = data.get("metadata_sources") or []
            for s in metadata_sources:
                src_url = s.get("source_url")
                if src_url and not is_pdf(src_url):
                    parsed = urlparse(src_url)
                    if parsed.netloc and parsed.scheme in {"http", "https"}:
                        website = f"{parsed.scheme}://{parsed.netloc}/"
                        break
        if not website:
            summary["no_website"] = summary.get("no_website", 0) + 1
            continue
        existing_doc_count = len(data.get("documents") or [])
        if existing_doc_count >= args.target_pdfs:
            summary["already_full"] = summary.get("already_full", 0) + 1
            continue

        processed += 1
        kept_urls, audit = collect_governing_pdfs(session, website)
        new_urls = [u for u in kept_urls if u not in already_banked_urls(data)]
        if not new_urls:
            summary["no_new_pdfs"] = summary.get("no_new_pdfs", 0) + 1
            print(json.dumps({"slug": blob.name, "audit": audit, "status": "no_new"}))
            continue

        if args.dry_run:
            summary["dry_would_probe"] = summary.get("dry_would_probe", 0) + 1
            print(json.dumps({
                "slug": blob.name,
                "status": "dry_would_probe",
                "new_pdfs": new_urls[:6],
                "pdfs_seen": audit["pdfs_seen"],
            }))
            continue

        # Build a Lead pinned to the existing slug (use the manifest's name + state + county)
        addr = data.get("address") or {}
        lead = Lead(
            name=data.get("name") or "Unknown HOA",
            source="owned-domain-depth-ga",
            source_url=website,
            state=addr.get("state") or "GA",
            county=addr.get("county"),
            city=addr.get("city"),
            website=None,
        )
        # Per-lead timeout via SIGALRM
        def _handler(signum, frame):
            raise TimeoutError("probe timed out")
        old = signal.signal(signal.SIGALRM, _handler)
        signal.alarm(args.per_lead_timeout)
        try:
            result = probe(lead, max_pdfs=12, pre_discovered_pdf_urls=new_urls)
            summary["probed"] = summary.get("probed", 0) + 1
            print(json.dumps({
                "slug": blob.name,
                "status": "probed",
                "banked": result.documents_banked,
                "skipped": result.documents_skipped,
                "new_pdfs": new_urls[:6],
            }))
        except TimeoutError as exc:
            summary["timeout"] = summary.get("timeout", 0) + 1
            print(json.dumps({"slug": blob.name, "status": "timeout", "error": str(exc)}))
        except Exception as exc:
            summary["error"] = summary.get("error", 0) + 1
            print(json.dumps({"slug": blob.name, "status": "error", "error": str(exc)[:200]}))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        time.sleep(args.probe_delay)

    print(json.dumps({"summary": summary, "processed": processed}, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
