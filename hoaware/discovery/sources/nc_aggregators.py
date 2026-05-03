"""NC HOA aggregator scrapers.

Each function yields Lead objects ready for probe().
Direct-PDF sources also yield pre-discovered PDF URLs via the second
element of each (Lead, list[str]) tuple — callers that understand this
can pass them to probe(pre_discovered_pdf_urls=...).

Simpler callers can just use nc_leads() which yields Lead objects only,
setting lead.website to the best available URL for probe() to crawl.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from ..leads import Lead

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Patterns to detect non-HOA (commercial/government) entries
_SKIP_NAMES = re.compile(
    r"\b(office\s+park|business\s+(park|center)|professional\s+(park|center)|"
    r"apartments?|marina|apartments?|commercial|industrial|"
    r"service\s+center|trade\s+center|retail|restaurant)\b",
    re.IGNORECASE,
)


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _get(session: requests.Session, url: str, **kwargs) -> requests.Response | None:
    try:
        r = session.get(url, timeout=20, allow_redirects=True, **kwargs)
        if r.status_code == 200:
            return r
        log.info("GET %s returned %s", url, r.status_code)
    except requests.RequestException as exc:
        log.info("GET %s failed: %s", url, exc)
    return None


def _soup(r: requests.Response) -> BeautifulSoup:
    return BeautifulSoup(r.text, "html.parser")


def _is_pdf(url: str) -> bool:
    return url.lower().split("?")[0].rstrip("#").endswith(".pdf")


# ---------------------------------------------------------------------------
# Closing Carolina — ~246 Charlotte-metro HOAs, each entry = direct PDF link
# ---------------------------------------------------------------------------

def closing_carolina_leads(
    *, session: requests.Session | None = None
) -> Iterator[tuple[Lead, list[str]]]:
    """Yield (Lead, [pdf_url]) for each HOA on closingcarolina.com/covenants/.

    Returns a tuple so callers can pass pdf_url to
    probe(pre_discovered_pdf_urls=[pdf_url]).
    """
    s = session or _make_session()
    url = "https://closingcarolina.com/covenants/"
    r = _get(s, url)
    if not r:
        log.warning("closing_carolina: could not fetch %s", url)
        return

    soup = _soup(r)
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not _is_pdf(href):
            continue
        pdf_url = urljoin(url, href)
        if pdf_url in seen:
            continue
        seen.add(pdf_url)

        # Name comes from link text; strip trailing county/year info
        name = a.get_text(strip=True)
        if not name:
            # Fall back to filename
            name = pdf_url.rsplit("/", 1)[-1].replace(".pdf", "").replace("-", " ").title()

        # Try to infer county from parent element or page context
        county = None
        parent = a.find_parent(["div", "section", "li"])
        if parent:
            header = parent.find(["h2", "h3", "h4"])
            if header:
                htext = header.get_text(strip=True).lower()
                for c in ("mecklenburg", "union", "cabarrus", "iredell", "gaston"):
                    if c in htext:
                        county = c.capitalize()
                        break

        yield (
            Lead(
                name=name,
                source="closing-carolina",
                source_url=pdf_url,
                state="NC",
                county=county,
                website=None,
            ),
            [pdf_url],
        )

    log.info("closing_carolina: yielded from %s", url)


# ---------------------------------------------------------------------------
# CASNC — 500+ Triangle/Sandhills HOAs, one page per community
# ---------------------------------------------------------------------------

def casnc_leads(*, session: requests.Session | None = None) -> Iterator[Lead]:
    """Yield one Lead per CASNC community at casnc.com/communities/."""
    s = session or _make_session()
    url = "https://casnc.com/communities/"
    r = _get(s, url)
    if not r:
        log.warning("casnc: could not fetch %s", url)
        return

    soup = _soup(r)
    base = "https://casnc.com"
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/communities/" not in href:
            continue
        community_url = urljoin(base, href)
        # Skip if it's the listing page itself
        if community_url.rstrip("/") == url.rstrip("/"):
            continue
        if community_url in seen:
            continue
        seen.add(community_url)

        name = a.get_text(strip=True)
        if not name or len(name) < 3:
            continue

        yield Lead(
            name=name,
            source="casnc",
            source_url=community_url,
            state="NC",
            website=community_url,
        )

    log.info("casnc: yielded %d leads from %s", len(seen), url)


# ---------------------------------------------------------------------------
# Seaside Management OBX — 60+ OBX HOAs
# ---------------------------------------------------------------------------

def seaside_leads(*, session: requests.Session | None = None) -> Iterator[Lead]:
    """Yield one Lead per Seaside Management community (OBX NC)."""
    s = session or _make_session()
    base = "https://www.seaside-management.com"
    seen_slugs: set[str] = set()

    for listing_path in ("/associations/", "/myassociations/"):
        listing_url = base + listing_path
        r = _get(s, listing_url)
        if not r:
            continue

        soup = _soup(r)
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # Match both /associations/<slug>/ and /myassociations/<slug>/
            m = re.search(r"/(my)?associations/([^/]+)/?$", href, re.IGNORECASE)
            if not m:
                continue
            slug = m.group(2)
            if slug in seen_slugs or slug in ("", "associations"):
                continue
            seen_slugs.add(slug)

            name = a.get_text(strip=True) or slug.replace("-", " ").title()
            community_url = urljoin(base, href)
            if not community_url.endswith("/"):
                community_url += "/"

            yield Lead(
                name=name,
                source="seaside-obx",
                source_url=community_url,
                state="NC",
                county="Dare",  # Mostly Dare County OBX
                website=community_url,
            )

    log.info("seaside: yielded %d HOAs", len(seen_slugs))


# ---------------------------------------------------------------------------
# Triad Community Management — 22 Greensboro/W-S/High Point HOAs
# ---------------------------------------------------------------------------

def triad_leads(
    *, session: requests.Session | None = None
) -> Iterator[tuple[Lead, list[str]]]:
    """Yield (Lead, [pdf_urls]) for each HOA section on triadcommunitymanagement.com/forms."""
    s = session or _make_session()
    url = "https://triadcommunitymanagement.com/forms"
    r = _get(s, url)
    if not r:
        log.warning("triad: could not fetch %s", url)
        return

    soup = _soup(r)
    # Triad page groups PDFs under heading elements (h2/h3) with the HOA name
    current_name: str | None = None
    current_pdfs: list[str] = []

    def _flush(name, pdfs):
        if name and pdfs:
            yield (
                Lead(
                    name=name,
                    source="triad-mgmt",
                    source_url=url,
                    state="NC",
                    # Greensboro / Winston-Salem / High Point area
                    county=None,
                    website=None,
                ),
                pdfs,
            )

    for tag in soup.find_all(["h2", "h3", "h4", "a"]):
        if tag.name in ("h2", "h3", "h4"):
            yield from _flush(current_name, current_pdfs)
            text = tag.get_text(strip=True)
            if text and len(text) > 3 and not _SKIP_NAMES.search(text):
                current_name = text
                current_pdfs = []
            else:
                current_name = None
                current_pdfs = []
        elif tag.name == "a" and current_name:
            href = tag.get("href", "")
            if _is_pdf(href):
                current_pdfs.append(urljoin(url, href))

    yield from _flush(current_name, current_pdfs)


# ---------------------------------------------------------------------------
# Wilson PM Raleigh — 11 Raleigh HOA bundles
# ---------------------------------------------------------------------------

def wilson_pm_leads(
    *, session: requests.Session | None = None
) -> Iterator[tuple[Lead, list[str]]]:
    """Yield (Lead, [pdf_urls]) for each HOA on wpminc.net/hoa-information."""
    s = session or _make_session()
    url = "https://www.wpminc.net/hoa-information"
    r = _get(s, url)
    if not r:
        log.warning("wilson_pm: could not fetch %s", url)
        return

    soup = _soup(r)
    current_name: str | None = None
    current_pdfs: list[str] = []

    def _flush(name, pdfs):
        if name and pdfs:
            yield (
                Lead(
                    name=name,
                    source="wilson-pm",
                    source_url=url,
                    state="NC",
                    county="Wake",
                    website=None,
                ),
                pdfs,
            )

    for tag in soup.find_all(["h2", "h3", "h4", "strong", "a"]):
        if tag.name in ("h2", "h3", "h4", "strong"):
            text = tag.get_text(strip=True)
            if not text or len(text) < 3:
                continue
            # Headings that look like HOA names (contain HOA/Community/etc.)
            if re.search(r"\b(hoa|homeowner|community|subdivision|association)\b", text, re.IGNORECASE):
                yield from _flush(current_name, current_pdfs)
                current_name = text
                current_pdfs = []
        elif tag.name == "a" and current_name:
            href = tag.get("href", "")
            if _is_pdf(href):
                current_pdfs.append(urljoin(url, href))

    yield from _flush(current_name, current_pdfs)


# ---------------------------------------------------------------------------
# Signature Management — 84+ Johnston County HOAs
# ---------------------------------------------------------------------------

def signature_leads(*, session: requests.Session | None = None) -> Iterator[Lead]:
    """Yield one Lead per Signature Management community (Johnston County NC)."""
    s = session or _make_session()
    url = "https://signaturemgt.com/communities/"
    r = _get(s, url)
    if not r:
        log.warning("signature: could not fetch %s", url)
        return

    soup = _soup(r)
    base = "https://signaturemgt.com"
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/communities/" not in href:
            continue
        community_url = urljoin(base, href)
        if community_url.rstrip("/") == url.rstrip("/"):
            continue
        if community_url in seen:
            continue

        name = a.get_text(strip=True)
        if not name or len(name) < 3:
            continue
        if _SKIP_NAMES.search(name):
            continue

        seen.add(community_url)
        yield Lead(
            name=name,
            source="signaturemgt",
            source_url=community_url,
            state="NC",
            county="Johnston",
            website=community_url,
        )

    log.info("signature: yielded %d leads", len(seen))


# ---------------------------------------------------------------------------
# Blue Atlantic Mgmt (BAM) Wilmington — 27+ HOAs
# ---------------------------------------------------------------------------

_BAM_SKIP = re.compile(
    r"\b(office\s+park|business\s+(park|center)|professional|marina|apartments?|"
    r"owner\s+forms?|tenant|arc\s+form|auto.?draft)\b",
    re.IGNORECASE,
)


def bam_leads(*, session: requests.Session | None = None) -> Iterator[Lead]:
    """Yield one Lead per BAM community (Wilmington NC area)."""
    s = session or _make_session()
    url = "https://bamgt.com/communities/"
    r = _get(s, url)
    if not r:
        log.warning("bam: could not fetch %s", url)
        return

    soup = _soup(r)
    base = "https://bamgt.com"
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/communities/" not in href:
            continue
        community_url = urljoin(base, href)
        if community_url.rstrip("/") == url.rstrip("/"):
            continue
        if community_url in seen:
            continue

        name = a.get_text(strip=True)
        if not name or len(name) < 3:
            continue
        if _BAM_SKIP.search(name) or _SKIP_NAMES.search(name):
            continue

        seen.add(community_url)
        yield Lead(
            name=name,
            source="bam-wilmington",
            source_url=community_url,
            state="NC",
            # Majority New Hanover; some Pender/Brunswick
            county="New Hanover",
            website=community_url,
        )

    log.info("bam: yielded %d leads", len(seen))


# ---------------------------------------------------------------------------
# Wake HOA Management — 50+ Wake County HOAs
# ---------------------------------------------------------------------------

_WAKE_SKIP = re.compile(
    r"\b(office\s+park|business\s+park|service\s+center|government|commercial)\b",
    re.IGNORECASE,
)


def wake_hoa_leads(*, session: requests.Session | None = None) -> Iterator[Lead]:
    """Yield one Lead per Wake HOA Mgmt community (Wake County NC)."""
    s = session or _make_session()
    listing_url = "https://wakehoa.com/"
    r = _get(s, listing_url)
    if not r:
        log.warning("wake_hoa: could not fetch %s", listing_url)
        return

    soup = _soup(r)
    base = "https://wakehoa.com"
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        abs_url = urljoin(base, href)
        # Community pages are /<slug>.html on the same host
        if urlparse(abs_url).netloc != "wakehoa.com":
            continue
        path = urlparse(abs_url).path
        if not path.endswith(".html") or path == "/index.html":
            continue
        if abs_url in seen:
            continue

        name = a.get_text(strip=True)
        if not name or len(name) < 3:
            continue
        if _WAKE_SKIP.search(name) or _SKIP_NAMES.search(name):
            continue

        seen.add(abs_url)
        yield Lead(
            name=name,
            source="wake-hoa-mgmt",
            source_url=abs_url,
            state="NC",
            county="Wake",
            website=abs_url,
        )

    log.info("wake_hoa: yielded %d leads", len(seen))


# ---------------------------------------------------------------------------
# HOA Management of Eastern NC — 35+ Pitt County HOAs
# ---------------------------------------------------------------------------

def hoamgt_eastern_leads(*, session: requests.Session | None = None) -> Iterator[Lead]:
    """Yield one Lead per HOAMGT Eastern NC community (Pitt County / Greenville)."""
    s = session or _make_session()
    url = "https://hoamgtcompany.com/communities/"
    r = _get(s, url)
    if not r:
        log.warning("hoamgt_eastern: could not fetch %s", url)
        return

    soup = _soup(r)
    base = "https://hoamgtcompany.com"
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/communities/" not in href:
            continue
        community_url = urljoin(base, href)
        if community_url.rstrip("/") == url.rstrip("/"):
            continue
        if community_url in seen:
            continue

        name = a.get_text(strip=True)
        if not name or len(name) < 3:
            continue
        if _SKIP_NAMES.search(name):
            continue

        seen.add(community_url)
        yield Lead(
            name=name,
            source="hoamgt-eastern",
            source_url=community_url,
            state="NC",
            county="Pitt",
            website=community_url,
        )

    log.info("hoamgt_eastern: yielded %d leads", len(seen))


# ---------------------------------------------------------------------------
# Combined iterator
# ---------------------------------------------------------------------------

# Sources that yield plain Lead objects
_WEBSITE_SOURCES: dict[str, callable] = {
    "casnc": casnc_leads,
    "seaside": seaside_leads,
    "signature": signature_leads,
    "bam": bam_leads,
    "wake-hoa": wake_hoa_leads,
    "hoamgt-eastern": hoamgt_eastern_leads,
}

# Sources that yield (Lead, list[str]) tuples
_DIRECT_PDF_SOURCES: dict[str, callable] = {
    "closing-carolina": closing_carolina_leads,
    "triad": triad_leads,
    "wilson-pm": wilson_pm_leads,
}

ALL_SOURCES = list(_WEBSITE_SOURCES.keys()) + list(_DIRECT_PDF_SOURCES.keys())


def nc_leads(
    sources: list[str] | None = None,
    *,
    session: requests.Session | None = None,
) -> Iterator[Lead]:
    """Yield Lead objects from NC aggregators.

    For direct-PDF sources, sets lead.source_url to the first PDF URL so
    the CLI can at least record the lead. Callers wanting the full PDF list
    should use nc_leads_with_pdfs() instead.

    sources: subset of ALL_SOURCES; defaults to all.
    """
    wanted = set(sources or ALL_SOURCES)
    s = session or _make_session()

    for name, fn in _WEBSITE_SOURCES.items():
        if name not in wanted:
            continue
        try:
            yield from fn(session=s)
        except Exception as exc:
            log.error("source %s failed: %s", name, exc)

    for name, fn in _DIRECT_PDF_SOURCES.items():
        if name not in wanted:
            continue
        try:
            for lead, _pdfs in fn(session=s):
                yield lead
        except Exception as exc:
            log.error("source %s failed: %s", name, exc)


def nc_leads_with_pdfs(
    sources: list[str] | None = None,
    *,
    session: requests.Session | None = None,
) -> Iterator[tuple[Lead, list[str]]]:
    """Like nc_leads() but yields (Lead, [pdf_urls]) for every source.

    Website-based sources yield (Lead, []) — probe() harvests PDFs itself.
    Direct-PDF sources yield (Lead, [url, ...]) — pass to
    probe(pre_discovered_pdf_urls=urls).
    """
    wanted = set(sources or ALL_SOURCES)
    s = session or _make_session()

    for name, fn in _WEBSITE_SOURCES.items():
        if name not in wanted:
            continue
        try:
            for lead in fn(session=s):
                yield lead, []
        except Exception as exc:
            log.error("source %s failed: %s", name, exc)

    for name, fn in _DIRECT_PDF_SOURCES.items():
        if name not in wanted:
            continue
        try:
            yield from fn(session=s)
        except Exception as exc:
            log.error("source %s failed: %s", name, exc)
