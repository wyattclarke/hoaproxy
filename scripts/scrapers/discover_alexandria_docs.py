#!/usr/bin/env python3
"""
Check Alexandria HOA/condo websites for downloadable governing documents.

Inputs:
  data/alexandria_va/import.json

Outputs:
  scraped_hoa_docs/alexandria_va/<association-slug>/... downloaded docs
  data/alexandria_va/document_discovery_report.json

The crawler is intentionally shallow: it checks each association's website,
follows a limited number of same-domain HTML pages that look document-related,
and downloads files whose URLs or link text suggest governing documents.
"""

from __future__ import annotations

import json
import mimetypes
import re
import ssl
from http.client import InvalidURL
from http.client import RemoteDisconnected
from collections import defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

INPUT_PATH = Path("/Users/ngoshaliclarke/Documents/GitHub/hoaproxy/data/alexandria_va/import.json")
DOWNLOAD_ROOT = Path("/Users/ngoshaliclarke/Documents/GitHub/hoaproxy/scraped_hoa_docs/alexandria_va")
REPORT_PATH = Path("/Users/ngoshaliclarke/Documents/GitHub/hoaproxy/data/alexandria_va/document_discovery_report.json")

USER_AGENT = "HOAproxy-document-discovery/1.0"
HTML_EXTENSIONS = {"", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp"}
DOWNLOAD_EXTENSIONS = {".pdf", ".doc", ".docx", ".rtf", ".txt"}
DOC_KEYWORDS = {
    "cc&r",
    "ccrs",
    "cc&r's",
    "ccr",
    "covenant",
    "covenants",
    "bylaw",
    "bylaws",
    "declaration",
    "declarations",
    "governing",
    "governing document",
    "governing documents",
    "rules",
    "regulations",
    "articles of incorporation",
    "architectural guidelines",
    "community handbook",
    "resolution",
}
PAGE_HINT_KEYWORDS = {
    "document",
    "documents",
    "governing",
    "bylaws",
    "ccr",
    "cc&r",
    "rules",
    "regulations",
    "forms",
    "resources",
    "downloads",
}
SKIP_URL_KEYWORDS = {
    "mailto:",
    "tel:",
    "javascript:",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "twitter.com",
    "x.com",
}


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self._href = value
                self._text_parts = []
                break

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._href is None:
            return
        text = " ".join(" ".join(self._text_parts).split())
        self.links.append((self._href, text))
        self._href = None
        self._text_parts = []


@dataclass
class SiteResult:
    name: str
    website_url: str
    normalized_url: str | None
    status: str
    downloaded_files: list[str]
    notes: list[str]


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "association"


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    path = quote(parts.path or "/", safe="/%:@()+,;=-_.~")
    query = quote(parts.query, safe="=&?/%:+,;()-_.~")
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_website(raw: str | None) -> str | None:
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate or candidate.upper() == "N/A":
        return None
    if ";" in candidate:
        parts = [part.strip() for part in candidate.split(";") if "://" in part or "." in part]
        candidate = parts[0] if parts else candidate.split(";", 1)[0].strip()
    if "@" in candidate and "://" not in candidate:
        return None
    if not candidate.startswith(("http://", "https://")):
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    if parsed.username and not parsed.path.strip("/"):
        return None
    return canonicalize_url(candidate)


def fetch(url: str, *, timeout: int = 20) -> tuple[bytes, str]:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    context = ssl.create_default_context()
    with urlopen(req, timeout=timeout, context=context) as resp:
        content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        return resp.read(), content_type


def is_same_site(url: str, root_url: str) -> bool:
    url_host = urlparse(url).netloc.lower().removeprefix("www.")
    root_host = urlparse(root_url).netloc.lower().removeprefix("www.")
    return bool(url_host) and url_host == root_host


def file_extension_from_url(url: str) -> str:
    path = urlparse(url).path.lower()
    return Path(path).suffix.lower()


def looks_like_document(url: str, text: str) -> bool:
    haystack = f"{url} {text}".lower()
    return any(keyword in haystack for keyword in DOC_KEYWORDS)


def looks_like_document_page(url: str, text: str) -> bool:
    haystack = f"{url} {text}".lower()
    return any(keyword in haystack for keyword in PAGE_HINT_KEYWORDS)


def extract_links(base_url: str, html_bytes: bytes) -> list[tuple[str, str]]:
    parser = LinkParser()
    parser.feed(html_bytes.decode("utf-8", errors="ignore"))
    links: list[tuple[str, str]] = []
    for href, text in parser.links:
        href = href.strip()
        if not href:
            continue
        lowered = href.lower()
        if any(token in lowered for token in SKIP_URL_KEYWORDS):
            continue
        absolute = canonicalize_url(urljoin(base_url, href))
        links.append((absolute, text))
    return links


def unique_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def discover_candidate_files(root_url: str) -> tuple[list[str], list[str]]:
    visited_pages: set[str] = set()
    page_queue = [root_url]
    file_urls: list[str] = []
    notes: list[str] = []

    while page_queue and len(visited_pages) < 12:
        page_url = page_queue.pop(0)
        if page_url in visited_pages:
            continue
        visited_pages.add(page_url)
        try:
            body, content_type = fetch(page_url)
        except (HTTPError, URLError, TimeoutError, ssl.SSLError, RemoteDisconnected, InvalidURL) as exc:
            notes.append(f"page fetch failed: {page_url} ({exc})")
            continue

        ext = file_extension_from_url(page_url)
        if ext in DOWNLOAD_EXTENSIONS or content_type in {"application/pdf", "application/msword"}:
            if looks_like_document(page_url, ""):
                file_urls.append(page_url)
            continue

        if "html" not in content_type and ext not in HTML_EXTENSIONS:
            continue

        for link_url, link_text in extract_links(page_url, body):
            if not is_same_site(link_url, root_url):
                continue
            link_ext = file_extension_from_url(link_url)
            if link_ext in DOWNLOAD_EXTENSIONS:
                if looks_like_document(link_url, link_text):
                    file_urls.append(link_url)
                continue
            if looks_like_document_page(link_url, link_text):
                if link_url not in visited_pages and link_url not in page_queue:
                    page_queue.append(link_url)

    return unique_preserve_order(file_urls), notes


def filename_for_download(url: str, content_type: str, default_stem: str) -> str:
    path_name = Path(urlparse(url).path).name
    if path_name:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", path_name)
    guessed_ext = mimetypes.guess_extension(content_type) or ".bin"
    return f"{default_stem}{guessed_ext}"


def download_files(name: str, root_url: str, file_urls: list[str]) -> tuple[list[str], list[str]]:
    association_dir = DOWNLOAD_ROOT / slugify(name)
    association_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[str] = []
    notes: list[str] = []

    for idx, file_url in enumerate(file_urls, start=1):
        try:
            body, content_type = fetch(file_url, timeout=30)
        except (HTTPError, URLError, TimeoutError, ssl.SSLError, RemoteDisconnected, InvalidURL) as exc:
            notes.append(f"download failed: {file_url} ({exc})")
            continue

        ext = file_extension_from_url(file_url)
        if ext not in DOWNLOAD_EXTENSIONS and content_type not in {
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/plain",
            "application/rtf",
        }:
            notes.append(f"skipped non-doc content: {file_url} ({content_type or 'unknown'})")
            continue

        filename = filename_for_download(file_url, content_type, f"document_{idx}")
        out_path = association_dir / filename
        out_path.write_bytes(body)
        downloaded.append(str(out_path))

    return downloaded, notes


def main() -> None:
    data = json.loads(INPUT_PATH.read_text())
    records = [record for record in data["records"] if record.get("website_url")]
    results: list[SiteResult] = []
    summary = defaultdict(int)

    for record in records:
        name = record["name"]
        raw_website = record["website_url"]
        normalized = normalize_website(raw_website)
        if not normalized:
            results.append(
                SiteResult(
                    name=name,
                    website_url=raw_website,
                    normalized_url=None,
                    status="invalid_website",
                    downloaded_files=[],
                    notes=["website field could not be normalized to a usable URL"],
                )
            )
            summary["invalid_website"] += 1
            print(f"[invalid] {name}: {raw_website}")
            continue

        print(f"[check] {name}: {normalized}")
        candidates, notes = discover_candidate_files(normalized)
        downloaded, download_notes = download_files(name, normalized, candidates)
        notes.extend(download_notes)

        if downloaded:
            status = "downloaded"
        elif candidates:
            status = "candidates_not_downloaded"
        else:
            status = "no_documents_found"
        summary[status] += 1
        results.append(
            SiteResult(
                name=name,
                website_url=raw_website,
                normalized_url=normalized,
                status=status,
                downloaded_files=downloaded,
                notes=notes,
            )
        )

    report = {
        "source": data.get("source"),
        "websites_checked": len(records),
        "status_counts": dict(summary),
        "results": [result.__dict__ for result in results],
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2))
    print(f"\nWrote report -> {REPORT_PATH}")
    print(json.dumps(report["status_counts"], indent=2))


if __name__ == "__main__":
    main()
