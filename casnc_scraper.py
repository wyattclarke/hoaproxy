#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
import traceback

BASE = "https://www.casnc.com"
INDEX = "https://www.casnc.com/communities/"
OUTDIR = Path("casnc_hoa_docs")
REQUEST_DELAY_SEC = 1.0  # be polite
TIMEOUT = 20
HEADERS = {
    # Use only ASCII characters in header values to avoid encoding errors
    "User-Agent": "Research script (contact: you@example.com) - respectful single-thread fetcher"
}

# ---- helpers --------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Remove fragments, normalize scheme/host, and strip trailing slashes."""
    u = urlparse(url)
    u = u._replace(fragment="", query=u.query)  # keep query (taxonomy pages may use it)
    # normalize to https and casnc.com host if relative later
    return urlunparse(u).rstrip("/")

def is_pdf_link(href: str) -> bool:
    if not href:
        return False
    return href.lower().split("?", 1)[0].endswith(".pdf")

def safe_name(name: str) -> str:
    """Make a safe directory/file stem from arbitrary text."""
    name = name.strip()
    # collapse whitespace
    name = re.sub(r"\s+", " ", name)
    # remove filesystem-unfriendly chars
    name = re.sub(r'[<>:"/\\|?*]+', " ", name)
    # trim length
    return name[:120].strip()

def get_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    # Some sites use odd encodings; decode bytes with apparent_encoding or utf-8
    encoding = resp.encoding or resp.apparent_encoding or "utf-8"
    text = resp.content.decode(encoding, errors="replace")
    return BeautifulSoup(text, "html.parser")

def unique(seq):
    seen = set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            yield x

def filename_from_url(url: str) -> str:
    """Derive a safe filename from URL; fall back to hash if needed."""
    path = urlparse(url).path
    name = os.path.basename(path) or "document.pdf"
    # ensure .pdf extension present
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    name = safe_name(name)
    if not name:
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        name = f"file-{h}.pdf"
    return name

def resolve(url_or_path: str, base: str) -> str:
    if not url_or_path:
        return ""
    if url_or_path.startswith("mailto:") or url_or_path.startswith("tel:"):
        return ""
    return normalize_url(urljoin(base, url_or_path))

# ---- crawl communities ----------------------------------------------------

def collect_community_pages(index_url: str):
    """
    From the index, collect links that look like community pages.
    WordPress often uses /communities/<slug>/.
    """
    soup = get_soup(index_url)
    links = []
    for a in soup.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
        abs_url = resolve(href, index_url)
        # keep /communities/* but not the index page itself
        if abs_url.startswith(f"{BASE}/communities/") and abs_url != INDEX.rstrip("/"):
            links.append(abs_url)
    # Some sites paginate the communities list. Try to find pagination and follow it.
    pagination = [resolve(a.get("href"), index_url) for a in soup.select("a[href*='page/']")]
    for p in unique([u for u in pagination if u]):
        try:
            time.sleep(REQUEST_DELAY_SEC)
            psoup = get_soup(p)
        except Exception:
            continue
        for a in psoup.select("a[href]"):
            href = a.get("href")
            if not href:
                continue
            abs_url = resolve(href, p)
            if abs_url.startswith(f"{BASE}/communities/") and abs_url != INDEX.rstrip("/"):
                links.append(abs_url)

    # de-dup and sort
    return sorted(unique(links))

def community_name_from_page(soup: BeautifulSoup) -> str:
    # Prefer the on-page H1, then title tag
    h1 = soup.find(["h1", "h2"])
    if h1 and h1.get_text(strip=True):
        return safe_name(h1.get_text(strip=True))
    title = soup.title.get_text(strip=True) if soup.title else "Unnamed Community"
    # Often formatted like "Landings at Pine Creek – CAS"
    title = title.split("–")[0].split("|")[0]
    return safe_name(title or "Unnamed Community")

def collect_pdfs_on_page(page_url: str, soup: BeautifulSoup):
    pdfs = []
    for a in soup.select("a[href]"):
        href = a.get("href")
        abs_url = resolve(href, page_url)
        if is_pdf_link(abs_url):
            pdfs.append(abs_url)
    # Also scan for <embed> or <iframe> PDF sources
    for tag in soup.select("embed[src], iframe[src]"):
        src = tag.get("src")
        abs_url = resolve(src, page_url)
        if is_pdf_link(abs_url):
            pdfs.append(abs_url)
    return list(unique(pdfs))

def download_pdf(url: str, dest_dir: Path):
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = filename_from_url(url)
    outpath = dest_dir / filename
    if outpath.exists() and outpath.stat().st_size > 0:
        print(f"  - Already have: {filename}")
        return
    try:
        with requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True) as r:
            r.raise_for_status()
            # basic content-type sanity check (many servers return octet-stream)
            if "application/pdf" not in r.headers.get("Content-Type", "").lower():
                # still proceed if it endswith .pdf — some servers mislabel
                pass
            with open(outpath, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        print(f"  + Saved: {filename}")
    except Exception as e:
        print(f"  ! Failed {url} -> {e}")

# ---- main -----------------------------------------------------------------

def main():
    OUTDIR.mkdir(parents=True, exist_ok=True)
    print(f"Index: {INDEX}")
    print("Collecting community pages…")
    try:
        communities = collect_community_pages(INDEX)
    except Exception as e:
        print("Failed to load index; traceback:")
        traceback.print_exc()
        return

    # De-dup hosts that are not CAS (rare, but filter anyway)
    communities = [u for u in communities if urlparse(u).netloc.endswith("casnc.com")]
    if not communities:
        print("No community pages found. The site layout may have changed.")
        return

    print(f"Found {len(communities)} community pages.\n")
    for i, url in enumerate(communities, 1):
        print(f"[{i}/{len(communities)}] {url}")
        time.sleep(REQUEST_DELAY_SEC)
        try:
            soup = get_soup(url)
        except Exception as e:
            print(f"  ! Skipping (fetch error): {e}")
            continue

        name = community_name_from_page(soup) or f"community_{i:04d}"
        dest = OUTDIR / name
        print(f"  Community: {name}")

        # collect PDFs on this page
        pdfs = collect_pdfs_on_page(url, soup)
        if not pdfs:
            print("  (No PDFs found on this page)")
            continue

        # download
        for pdf_url in pdfs:
            time.sleep(REQUEST_DELAY_SEC)
            download_pdf(pdf_url, dest)

    print("\nDone.")

if __name__ == "__main__":
    main()
