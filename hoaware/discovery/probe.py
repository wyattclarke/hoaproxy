"""Probe an HOA lead: fetch homepage, harvest governing-doc PDFs, bank everything found.

Usage::

    from hoaware.discovery import Lead, probe

    uri = probe(Lead(name="Hampton Meadow Cluster",
                     state="VA",
                     city="Reston",
                     website="https://www.hamptonmeadowcluster.org/",
                     source="manual",
                     source_url="local-test"))
    # -> "gs://hoaproxy-bank/v1/VA/.../manifest.json"
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from ..bank import DocumentInput, bank_hoa
from .fingerprint import fingerprint
from .leads import Lead
from .state_verify import verify_state

log = logging.getLogger(__name__)

USER_AGENT = (
    os.environ.get("HOA_DISCOVERY_USER_AGENT")
    or "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
)
HTML_TIMEOUT = 20
PDF_HEAD_TIMEOUT = 15
PDF_GET_TIMEOUT = 60
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_PDFS_PER_PROBE = 30
PDF_MAGIC = b"%PDF-"
ROBOTS_TIMEOUT = 10

_LAST_REQUEST_BY_HOST: dict[str, float] = {}
_ROBOTS_BY_ORIGIN: dict[str, RobotFileParser | None] = {}

# URL or link-text patterns that signal a governing document
_GOVDOC_KEYWORDS = re.compile(
    r"\b(declaration|covenants?|cc&?rs?|c\.c\.&r\.s?|bylaws?|by-laws?|"
    r"articles?(?:\s+of\s+(?:incorporation|organization))?|charter|"
    r"restrictions?|rules?(?:\s+(?:&|and)\s+regulations?)?|regulations?|guidelines?|"
    r"resolutions?|amendments?|policies|policy)\b",
    re.IGNORECASE,
)
# URL path / link text indicating a documents/library subpage worth crawling 1 level deep
_DOCSPAGE_KEYWORDS = re.compile(
    r"\b(documents?|governing|government|library|downloads?|forms?|"
    r"resources?|files?|publications?|rules|policies|covenants?|restrictions?)\b",
    re.IGNORECASE,
)
MAX_DOC_SUBPAGES = 5
# URLs we won't even try (login walls, virus-scan walls, dynamic apps)
_SKIP_URL_PATTERNS = re.compile(
    r"(townsq\.com|townsq\.io|frontsteps\.com|frontsteps\.io|connectresident|"
    r"cinc\.io|cincsystems|cincweb|caliber\.cloud|calibersoftware|enumerateengage|"
    r"appfolio\.com|buildium\.com|"
    r"drive\.google\.com|docs\.google\.com|"
    r"/login|/signin|/sign-in|/password|/account/)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    manifest_uri: str
    documents_banked: int
    documents_skipped: int
    homepage_fetched: bool
    platform: str
    is_walled: bool


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) in {"1", "true", "True"}


def _request_delay_seconds() -> float:
    raw = os.environ.get("HOA_DISCOVERY_REQUEST_DELAY_SECONDS", "0")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _polite_wait(url: str) -> None:
    delay = _request_delay_seconds()
    if delay <= 0:
        return
    host = urlparse(url).netloc
    last = _LAST_REQUEST_BY_HOST.get(host)
    now = time.monotonic()
    if last is not None:
        sleep_for = delay - (now - last)
        if sleep_for > 0:
            time.sleep(sleep_for)
    _LAST_REQUEST_BY_HOST[host] = time.monotonic()


def _robots_parser(session: requests.Session, url: str) -> RobotFileParser | None:
    origin = _origin(url)
    if origin in _ROBOTS_BY_ORIGIN:
        return _ROBOTS_BY_ORIGIN[origin]

    robots_url = urljoin(origin + "/", "robots.txt")
    parser = RobotFileParser(robots_url)
    try:
        _polite_wait(robots_url)
        response = session.get(robots_url, timeout=ROBOTS_TIMEOUT, allow_redirects=True)
        if response.status_code >= 400:
            _ROBOTS_BY_ORIGIN[origin] = None
            return None
        parser.parse(response.text.splitlines())
        _ROBOTS_BY_ORIGIN[origin] = parser
        return parser
    except requests.RequestException as exc:
        log.info("robots.txt fetch failed %s: %s", robots_url, exc)
        _ROBOTS_BY_ORIGIN[origin] = None
        return None


def _allowed_by_robots(session: requests.Session, url: str) -> bool:
    if not _env_bool("HOA_DISCOVERY_RESPECT_ROBOTS", "0"):
        return True
    parser = _robots_parser(session, url)
    if parser is None:
        return True
    allowed = parser.can_fetch(USER_AGENT, url)
    if not allowed:
        log.info("robots.txt disallows %s", url)
    return allowed


def _fetch_homepage(session: requests.Session, url: str) -> str | None:
    if not _allowed_by_robots(session, url):
        return None
    try:
        _polite_wait(url)
        r = session.get(url, timeout=HTML_TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            log.info("homepage %s returned %s", url, r.status_code)
            return None
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            log.info("homepage %s served non-HTML content-type %s", url, ctype)
            return None
        return r.text
    except requests.RequestException as exc:
        log.info("homepage fetch failed %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Link harvesting
# ---------------------------------------------------------------------------

def _normalize_url(base: str, href: str) -> str | None:
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return None
    abs_url = urljoin(base, href)
    parsed = urlparse(abs_url)
    if parsed.scheme not in ("http", "https"):
        return None
    return abs_url


def _looks_like_pdf_url(url: str) -> bool:
    return url.lower().split("?")[0].rsplit("#", 1)[0].endswith(".pdf")


def _looks_like_govdoc(url: str, link_text: str) -> bool:
    return bool(_GOVDOC_KEYWORDS.search(url) or _GOVDOC_KEYWORDS.search(link_text or ""))


def _should_try_lead_url_as_pdf(lead: Lead, html: str | None, is_walled: bool) -> bool:
    """Validated discovery leads sometimes point directly at a PDF-serving URL.

    Some public document-center URLs do not end in .pdf, so harvesting HTML
    misses them. Trying the lead URL as a PDF is cheap because _fetch_pdf()
    verifies PDF magic bytes before banking anything.
    """
    if not lead.website or is_walled or _SKIP_URL_PATTERNS.search(lead.website):
        return False
    if _looks_like_pdf_url(lead.website) or _looks_like_govdoc(lead.website, lead.name):
        return True
    if html is None and "docpages" in (lead.source or "").lower():
        return True
    return False


def _harvest_pdf_candidates(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return list of (absolute_url, link_text) for PDF candidates."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        url = _normalize_url(base_url, a.get("href"))
        if not url or url in seen:
            continue
        text = (a.get_text() or "").strip()
        if _looks_like_pdf_url(url) or _looks_like_govdoc(url, text):
            if _SKIP_URL_PATTERNS.search(url):
                continue
            seen.add(url)
            out.append((url, text))
    return out


def _harvest_doc_subpages(html: str, base_url: str) -> list[str]:
    """Find same-origin links to documents/library/forms pages."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc
    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        url = _normalize_url(base_url, a.get("href"))
        if not url or url in seen or _looks_like_pdf_url(url):
            continue
        if urlparse(url).netloc != base_host:
            continue
        if _SKIP_URL_PATTERNS.search(url):
            continue
        text = (a.get_text() or "").strip()
        path = urlparse(url).path or "/"
        if _DOCSPAGE_KEYWORDS.search(path) or _DOCSPAGE_KEYWORDS.search(text):
            seen.add(url)
            out.append(url)
        if len(out) >= MAX_DOC_SUBPAGES:
            break
    return out


# ---------------------------------------------------------------------------
# PDF fetching
# ---------------------------------------------------------------------------

@dataclass
class _PdfFetch:
    url: str
    link_text: str
    pdf_bytes: bytes | None = None
    skip_reason: str | None = None


def _fetch_pdf(session: requests.Session, url: str, link_text: str) -> _PdfFetch:
    """HEAD-check then GET the URL; return bytes or skip reason."""
    fetch = _PdfFetch(url=url, link_text=link_text)
    if not _allowed_by_robots(session, url):
        fetch.skip_reason = "robots_disallowed"
        return fetch
    try:
        _polite_wait(url)
        head = session.head(url, timeout=PDF_HEAD_TIMEOUT, allow_redirects=True)
        if head.status_code >= 400:
            # Some servers don't support HEAD — fall through to GET
            if head.status_code not in (403, 404, 405, 501):
                fetch.skip_reason = f"head_status_{head.status_code}"
                return fetch
        size = int(head.headers.get("Content-Length") or 0)
        if size > MAX_PDF_BYTES:
            fetch.skip_reason = f"too_large_{size}"
            return fetch
    except requests.RequestException:
        pass  # HEAD often fails; fall through to GET

    try:
        _polite_wait(url)
        r = session.get(url, timeout=PDF_GET_TIMEOUT, stream=True, allow_redirects=True)
    except requests.RequestException as exc:
        fetch.skip_reason = f"get_failed:{type(exc).__name__}"
        return fetch
    if r.status_code != 200:
        fetch.skip_reason = f"get_status_{r.status_code}"
        r.close()
        return fetch

    # Read into memory with size cap
    buf = bytearray()
    cap = MAX_PDF_BYTES + 1
    for chunk in r.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > cap:
            fetch.skip_reason = "too_large_streamed"
            r.close()
            return fetch
    r.close()

    if not buf.startswith(PDF_MAGIC):
        # Common: server returned an HTML login page despite a .pdf URL
        head_preview = bytes(buf[:200]).decode("utf-8", errors="replace")
        if "<html" in head_preview.lower() or "<!doctype html" in head_preview.lower():
            fetch.skip_reason = "html_disguised_as_pdf"
        else:
            fetch.skip_reason = "not_a_pdf"
        return fetch

    fetch.pdf_bytes = bytes(buf)
    return fetch


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def probe(
    lead: Lead,
    *,
    http_session: requests.Session | None = None,
    bucket_name: str | None = None,
    max_pdfs: int = MAX_PDFS_PER_PROBE,
    pre_discovered_pdf_urls: list[str] | None = None,
) -> ProbeResult:
    """Probe a single Lead end-to-end and return the bank manifest URI.

    Idempotent: re-running with the same lead is safe (bank dedups on sha256
    + slug). Network is the bottleneck, not bank operations.
    """
    if not lead.name:
        raise ValueError("lead.name is required")

    session = http_session or _make_session()

    # ---- Step 1: bank a stub immediately so we capture the lead even if
    # everything downstream fails. metadata_sources will accumulate.
    address = {
        "city": lead.city,
        "state": lead.state,
        "county": lead.county,
    }
    metadata_source = {
        "source": lead.source,
        "source_url": lead.source_url,
        "fields_provided": [
            f for f in ("name", "state", "city", "county", "website")
            if getattr(lead, f, None)
        ],
    }

    # ---- Step 2: fetch homepage if we have a URL, fingerprint platform.
    html: str | None = None
    platform_name = "unknown"
    is_walled = False
    website_dict: dict = {}
    if lead.website:
        if _SKIP_URL_PATTERNS.search(lead.website):
            # Known walled platform — don't even try to fetch
            platform_name = "walled-known"
            is_walled = True
            website_dict = {"url": lead.website, "platform": platform_name, "is_walled": True}
        else:
            html = _fetch_homepage(session, lead.website)
            if html:
                fp = fingerprint(html)
                platform_name = fp.name
                is_walled = fp.is_walled
                website_dict = {
                    "url": lead.website,
                    "platform": platform_name,
                    "is_walled": is_walled,
                }
            else:
                website_dict = {"url": lead.website, "platform": "unreachable", "is_walled": False}

    # ---- Step 3: harvest candidate PDF links from homepage HTML.
    # Also crawl 1 level deep into Documents/Library/Forms subpages.
    # Pre-discovered URLs from aggregator scrapers are seeded in first.
    candidates: list[tuple[str, str]] = []
    if pre_discovered_pdf_urls:
        candidates = [(u, "") for u in pre_discovered_pdf_urls]
    if _should_try_lead_url_as_pdf(lead, html, is_walled):
        candidates.append((lead.website, lead.name))
    if html and not is_walled:
        homepage_candidates = _harvest_pdf_candidates(html, lead.website)
        for sub_url in _harvest_doc_subpages(html, lead.website):
            sub_html = _fetch_homepage(session, sub_url)
            if sub_html:
                for url, text in _harvest_pdf_candidates(sub_html, sub_url):
                    if not any(url == c[0] for c in homepage_candidates):
                        homepage_candidates.append((url, text))
        # Merge, deduplicating against pre-discovered
        pre_urls = {c[0] for c in candidates}
        for url, text in homepage_candidates:
            if url not in pre_urls:
                candidates.append((url, text))
    candidates = candidates[:max_pdfs]

    # ---- Step 4: fetch each PDF, build DocumentInput / skipped_documents.
    documents: list[DocumentInput] = []
    skipped: list[dict] = []
    for url, link_text in candidates:
        fetch = _fetch_pdf(session, url, link_text)
        if fetch.skip_reason:
            skipped.append({"source_url": url, "reason": fetch.skip_reason, "link_text": link_text})
            continue
        # Optional: state verification via page-1 text.
        if lead.state and fetch.pdf_bytes:
            try:
                page1 = _extract_page1_text(fetch.pdf_bytes)
                sv = verify_state(page1, lead.state)
                if sv.state and sv.state != lead.state and sv.confidence == "high":
                    skipped.append({
                        "source_url": url,
                        "reason": f"state_mismatch_doc={sv.state}_lead={lead.state}",
                        "link_text": link_text,
                    })
                    continue
            except Exception:
                pass  # page-1 extract failure → still bank the doc

        filename = url.rsplit("/", 1)[-1].split("?")[0] or "document.pdf"
        documents.append(DocumentInput(
            pdf_bytes=fetch.pdf_bytes,
            source_url=url,
            filename=filename,
            text_extractable_hint=None,  # bank will inspect
        ))

    # ---- Step 5: bank with everything we found.
    kwargs = {"name": lead.name, "metadata_source": metadata_source}
    if any(address.values()):
        kwargs["address"] = {k: v for k, v in address.items() if v}
    if website_dict:
        kwargs["website"] = website_dict
    if documents:
        kwargs["documents"] = documents
    if skipped:
        kwargs["skipped_documents"] = skipped
    if bucket_name:
        kwargs["bucket_name"] = bucket_name

    uri = bank_hoa(**kwargs)
    return ProbeResult(
        manifest_uri=uri,
        documents_banked=len(documents),
        documents_skipped=len(skipped),
        homepage_fetched=html is not None,
        platform=platform_name,
        is_walled=is_walled,
    )


def _extract_page1_text(pdf_bytes: bytes) -> str:
    """Read first page as text; returns "" on failure."""
    try:
        import io
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        if reader.pages:
            return reader.pages[0].extract_text() or ""
    except Exception:
        return ""
    return ""


def probe_many(leads: Iterable[Lead], **kwargs) -> list[ProbeResult]:
    """Probe each lead serially. Returns ProbeResult per lead in order."""
    session = _make_session()
    return [probe(lead, http_session=session, **kwargs) for lead in leads]
