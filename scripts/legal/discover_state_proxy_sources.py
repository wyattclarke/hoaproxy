#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import deque
from html import unescape
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests

import sys
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.legal.build_source_map import US_STATES


STATE_NAMES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}
STATE_ABBR_BY_NAME = {name.lower(): abbr for abbr, name in STATE_NAMES.items()}

USER_AGENT = "Mozilla/5.0 (hoaproxy legal source discovery)"
ITEP_STATE_STATUTES_URL = "https://itep.org/state-statutes/"

ANCHOR_RE = re.compile(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
ITEP_SECTION_RE = re.compile(r"<h5>\s*<strong>\s*([^<]+?)\s*</strong>\s*</h5>\s*<ul>(.*?)</ul>", re.IGNORECASE | re.DOTALL)

BUCKET_KEYWORDS = {
    "proxy_voting": (
        "proxy",
        "proxies",
        "vote",
        "voting",
        "ballot",
        "meeting",
        "member meeting",
        "election",
        "homeowners",
        "planned community",
        "condominium",
        "cooperative",
        "association",
    ),
    "nonprofit_corp_overlay": (
        "nonprofit",
        "non-profit",
        "not-for-profit",
        "corporation",
        "corporations",
        "corporate",
        "member",
        "shareholder",
        "proxy",
    ),
    "electronic_transactions_overlay": (
        "electronic",
        "electronic record",
        "electronic signature",
        "digital signature",
        "uniform electronic transactions act",
        "ueta",
        "remote communication",
        "facsimile",
    ),
}

FOLLOW_HINTS = (
    "title",
    "chapter",
    "article",
    "part",
    "section",
    "statute",
    "code",
    "laws",
    "homeowners",
    "planned community",
    "condominium",
    "cooperative",
    "corporation",
    "nonprofit",
    "electronic",
    "proxy",
)

STATUTE_LINK_HINTS = (
    "statute",
    "statutes",
    "code",
    "codes",
    "law",
    "laws",
    "title",
    "chapter",
    "article",
    "section",
    "constitution",
    "display_index",
    "title_request",
    "app_mode",
    "proxy",
    "homeowners",
    "condominium",
    "cooperative",
    "corporation",
    "electronic",
    "ueta",
    "voting",
    "meeting",
    "homeowner",
    "association",
    "nonprofit",
)

STATUTE_SEED_HINTS = (
    "statute",
    "code",
    "constitution",
    "laws",
    "general statutes",
    "revised statutes",
    "compiled laws",
)

NON_STATUTE_SEED_HINTS = (
    "bill",
    "legislation",
    "session",
    "council",
)

COMMUNITY_HINTS = {
    "condo": ("condominium", "condo"),
    "coop": ("cooperative", "co-op", "coop"),
}

BUCKET_LABELS = {
    "proxy_voting": "proxy voting",
    "nonprofit_corp_overlay": "nonprofit corporation overlay",
    "electronic_transactions_overlay": "electronic transactions overlay",
}

BUCKET_FALLBACK_HINTS = {
    "proxy_voting": ("proxy", "vote", "election", "meeting", "member"),
    "nonprofit_corp_overlay": ("nonprofit", "corporation", "business", "corp"),
    "electronic_transactions_overlay": ("electronic", "ueta", "signature", "record", "transaction"),
}

MIN_BUCKET_SCORE = {
    "proxy_voting": 10,
    "nonprofit_corp_overlay": 10,
    "electronic_transactions_overlay": 10,
}

SEED_OVERRIDES: dict[str, list[str]] = {
    "AK": ["https://www.akleg.gov/basis/statutes.asp"],
    "AL": ["https://alison.legislature.state.al.us/"],
    "AZ": ["https://www.azleg.gov/arsDetail/?title=33"],
    "CA": ["https://leginfo.legislature.ca.gov/faces/codes.xhtml"],
    "CO": ["https://www.leg.colorado.gov/colorado-revised-statutes"],
    "CT": ["https://www.cga.ct.gov/current/pub/"],
    "FL": ["https://www.flsenate.gov/Laws/Statutes/"],
    "GA": ["https://www.legis.ga.gov/legislation/georgia-code"],
    "IN": ["https://iga.in.gov/laws/2025/ic"],
    "KY": ["https://apps.legislature.ky.gov/law/statutes/"],
    "LA": ["https://legis.la.gov/Legis/Laws_Toc.aspx"],
    "MA": ["https://malegislature.gov/Laws/GeneralLaws"],
    "MD": ["https://mgaleg.maryland.gov/mgawebsite/Laws/StatuteText"],
    "MN": ["https://www.revisor.mn.gov/statutes/"],
    "MO": ["https://revisor.mo.gov/main/Home.aspx"],
    "MS": [
        "https://billstatus.ls.state.ms.us/",
        "https://advance.lexis.com/container?config=00JAA1MDBjZGIxNy1kNjY5LTQ4YWUtYjM4Zi02NTBkMDE4OTViOWQKAFBvZENhdGFsb2e1ieXxDjC0h9FA7xV8",
    ],
    "NJ": ["https://www.njleg.state.nj.us/statutes"],
    "NM": ["https://nmonesource.com/nmos/nmsa/en/nav_date.do"],
    "NC": ["https://www.ncleg.gov/Laws/GeneralStatutes"],
    "NY": ["http://public.leginfo.state.ny.us/navigate.cgi"],
    "OK": ["https://www.oscn.net/applications/oscn/DeliverDocument.asp?CiteID=69789"],
    "SD": ["https://sdlegislature.gov/Statutes"],
    "TN": ["https://www.capitol.tn.gov/"],
    "TX": ["https://statutes.capitol.texas.gov/"],
    "UT": ["https://le.utah.gov/xcode/code.html"],
    "VA": ["https://law.lis.virginia.gov/vacode/"],
    "WI": ["https://docs.legis.wisconsin.gov/statutes/statutes"],
    "WY": ["https://wyoleg.gov/Legislation/Statutes"],
}

SIGNAL_TERMS = tuple(
    sorted(
        {
            term
            for terms in BUCKET_KEYWORDS.values()
            for term in terms
        }
        | {
            "homeowners",
            "association",
            "member",
            "meeting",
            "voting",
            "proxy",
            "electronic",
            "signature",
            "facsimile",
            "corporation",
            "nonprofit",
        },
        key=len,
        reverse=True,
    )
)


def _state_slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


def _clean_html_text(raw: str) -> str:
    text = unescape(TAG_RE.sub(" ", raw))
    text = WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    canonical = parsed._replace(fragment="")
    path = canonical.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    canonical = canonical._replace(path=path)
    return urlunparse(canonical)


def _extract_links(html: str, base_url: str, state_slug: str | None = None) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in ANCHOR_RE.finditer(html):
        href, label_html = match.group(1), match.group(2)
        href = unescape(href).strip()
        if not href:
            continue
        lowered = href.lower()
        if lowered.startswith(("javascript:", "mailto:", "tel:")):
            continue
        absolute = _canonicalize_url(urljoin(base_url, href))
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        if state_slug and not parsed.path.lower().startswith(f"/codes/{state_slug}/"):
            continue
        label = _clean_html_text(label_html)
        if not label:
            label = parsed.path.strip("/").split("/")[-1]
        context_start = max(0, match.start() - 220)
        context_end = min(len(html), match.end() + 220)
        context_text = _clean_html_text(html[context_start:context_end])
        if context_text and context_text.lower() != label.lower():
            label = f"{label} {context_text}"
        key = f"{absolute}|{label}"
        if key in seen:
            continue
        seen.add(key)
        links.append((absolute, label))
    return links


def _score_bucket(bucket: str, url: str, text: str, *, state_slug: str | None = None) -> int:
    context = f"{url.lower()} {text.lower()}"
    terms = BUCKET_KEYWORDS[bucket]
    score = 0
    for term in terms:
        if term in context:
            score += 6 if " " in term else 3

    if bucket == "proxy_voting":
        if "proxy" in context:
            score += 20
        if "vote" in context or "voting" in context:
            score += 8
    elif bucket == "nonprofit_corp_overlay":
        if "nonprofit" in context or "not-for-profit" in context:
            score += 20
        if "corporation" in context:
            score += 8
        if "proxy" in context:
            score += 6
    elif bucket == "electronic_transactions_overlay":
        if "electronic signature" in context:
            score += 22
        if "transactions act" in context or "ueta" in context:
            score += 16
        if "facsimile" in context or "remote communication" in context:
            score += 8

    parsed = urlparse(url)
    path = parsed.path.lower()
    if state_slug and path.endswith(f"/codes/{state_slug}/"):
        score -= 20
    if path.count("/") <= 2:
        score -= 6
    if "/chapter" in path or "/article" in path:
        score += 3
    if "/section" in path or "section-" in path:
        score += 6
    return score


def _detect_community_type(text: str) -> str:
    lowered = text.lower()
    for community_type, hints in COMMUNITY_HINTS.items():
        if any(hint in lowered for hint in hints):
            return community_type
    return "hoa"


def _is_probably_official_host(host: str) -> bool:
    lowered = host.lower()
    return lowered.endswith(".gov") or ".state." in lowered or "leg" in lowered or "statutes" in lowered or "code" in lowered


def _publisher_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host or "discovered"


def _looks_like_statute_seed(label: str, url: str) -> bool:
    context = f"{label.lower()} {url.lower()}"
    if any(h in context for h in NON_STATUTE_SEED_HINTS):
        return False
    return any(h in context for h in STATUTE_SEED_HINTS)


def _load_itep_seed_map(*, timeout: int) -> dict[str, list[dict]]:
    try:
        resp = requests.get(ITEP_STATE_STATUTES_URL, timeout=timeout, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
    except Exception:
        return {}

    seed_map: dict[str, list[dict]] = {}
    for state_raw, section_html in ITEP_SECTION_RE.findall(resp.text):
        state_name = _clean_html_text(state_raw)
        abbr = STATE_ABBR_BY_NAME.get(state_name.lower())
        if not abbr:
            continue
        links: list[dict] = []
        for href, label_html in ANCHOR_RE.findall(section_html):
            url = _canonicalize_url(urljoin(ITEP_STATE_STATUTES_URL, unescape(href).strip()))
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                continue
            label = _clean_html_text(label_html)
            if not label:
                continue
            links.append({"url": url, "label": label})
        if links:
            seed_map[abbr] = links
    return seed_map


def _seed_urls_for_state(abbr: str, state_links: list[dict]) -> list[str]:
    statute_links = [row["url"] for row in state_links if _looks_like_statute_seed(str(row.get("label") or ""), str(row.get("url") or ""))]
    seed_candidates = statute_links[:4]
    if not seed_candidates:
        seed_candidates = [str(row.get("url") or "") for row in state_links if str(row.get("url") or "")][:2]

    expanded: list[str] = []
    # Prefer deterministic overrides first so crawler/fallback selects maintained official paths.
    expanded.extend(SEED_OVERRIDES.get(abbr, []))
    for seed in seed_candidates:
        if not seed:
            continue
        expanded.append(seed)
        parsed = urlparse(seed)
        if parsed.scheme and parsed.netloc:
            root = f"{parsed.scheme}://{parsed.netloc}/"
            expanded.append(root)
            if parsed.scheme == "http":
                expanded.append(f"https://{parsed.netloc}{parsed.path or '/'}")
            if parsed.netloc.endswith(".net"):
                gov_host = parsed.netloc[:-4] + ".gov"
                expanded.append(f"{parsed.scheme}://{gov_host}{parsed.path or '/'}")

    deduped = []
    seen: set[str] = set()
    for url in expanded:
        canon = _canonicalize_url(url)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        deduped.append(canon)
    return deduped[:12]


def _should_follow_link(url: str, text: str, *, depth: int, max_depth: int) -> bool:
    if depth >= max_depth:
        return False
    parsed = urlparse(url)
    path = parsed.path.lower()
    query = (parsed.query or "").lower()
    if path.endswith(".pdf") or path.endswith(".doc") or path.endswith(".docx"):
        return False
    context = f"{path} {query} {text.lower()}"
    if any(hint in context for hint in FOLLOW_HINTS):
        return True
    if depth == 0 and path.count("/") <= 5:
        return True
    return False


def _is_statute_like_link(url: str, text: str) -> bool:
    context = f"{url.lower()} {text.lower()}"
    return any(hint in context for hint in STATUTE_LINK_HINTS)


def _extract_page_signal_text(html: str) -> str:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = _clean_html_text(title_match.group(1)) if title_match else ""
    headings = re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", html, re.IGNORECASE | re.DOTALL)
    heading_text = " ".join(_clean_html_text(chunk) for chunk in headings[:6] if chunk)
    body_text = _clean_html_text(html)
    lowered = body_text.lower()
    snippets: list[str] = []
    seen: set[str] = set()
    for term in SIGNAL_TERMS:
        start = 0
        while len(snippets) < 14:
            idx = lowered.find(term, start)
            if idx < 0:
                break
            left = max(0, idx - 90)
            right = min(len(body_text), idx + len(term) + 90)
            snippet = body_text[left:right].strip()
            key = snippet.lower()
            if snippet and key not in seen:
                seen.add(key)
                snippets.append(snippet)
            start = idx + len(term)
        if len(snippets) >= 14:
            break
    keyword_excerpt = " ".join(snippets) if snippets else body_text[:1200]
    merged = " ".join(part for part in [title, heading_text, keyword_excerpt] if part)
    return WHITESPACE_RE.sub(" ", merged).strip()


def _follow_priority(url: str, text: str) -> int:
    context = f"{url.lower()} {text.lower()}"
    priority = 0
    if "association" in context:
        priority += 30
    if "homeowner" in context or "condominium" in context or "cooperative" in context:
        priority += 35
    if "proxy" in context:
        priority += 40
    if "electronic" in context or "ueta" in context:
        priority += 40
    if "nonprofit" in context or "corporation" in context:
        priority += 30
    if "property" in context:
        priority += 25
    if "commercial" in context:
        priority += 12
    if "business" in context:
        priority += 12
    if "section" in context:
        priority += 20
    if "chapter" in context:
        priority += 15
    if "title" in context:
        priority += 10
    if "statute" in context or "code" in context or "laws" in context:
        priority += 10
    return priority


def _crawl_state(
    abbr: str,
    seed_urls: list[str],
    *,
    timeout: int,
    max_pages: int,
    max_depth: int,
) -> tuple[list[dict], int]:
    queue: deque[tuple[str, int]] = deque((url, 0) for url in seed_urls)
    queued: set[str] = set(seed_urls)
    visited: set[str] = set()
    candidates: dict[str, dict] = {}
    allowed_hosts: set[str] = {urlparse(url).netloc.lower() for url in seed_urls if urlparse(url).netloc}

    while queue and len(visited) < max_pages:
        url, depth = queue.popleft()
        queued.discard(url)
        canon = _canonicalize_url(url)
        if canon in visited:
            continue

        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
            resp.raise_for_status()
        except Exception:
            continue

        final_url = _canonicalize_url(resp.url)
        visited.add(canon)
        visited.add(final_url)
        final_host = urlparse(final_url).netloc.lower()
        if final_host:
            allowed_hosts.add(final_host)

        content_type = str(resp.headers.get("Content-Type") or "").lower()
        if "html" not in content_type and "<html" not in resp.text.lower()[:2000]:
            continue

        signal_text = _extract_page_signal_text(resp.text)
        if signal_text and _is_statute_like_link(final_url, signal_text):
            existing = candidates.get(final_url)
            if not existing or len(signal_text) > len(str(existing.get("text") or "")):
                candidates[final_url] = {
                    "jurisdiction": abbr,
                    "url": final_url,
                    "text": signal_text,
                    "source_page": final_url,
                    "depth": depth,
                }

        links = _extract_links(resp.text, resp.url, None)
        links.sort(key=lambda item: _follow_priority(item[0], item[1]), reverse=True)

        for link, text in links:
            link_host = urlparse(link).netloc.lower()
            if link_host not in allowed_hosts:
                continue
            if not link_host:
                continue
            statute_like = _is_statute_like_link(link, text)
            if depth == 0 and not statute_like:
                continue
            row = candidates.get(link)
            if not row or len(text) > len(str(row.get("text") or "")):
                candidates[link] = {
                    "jurisdiction": abbr,
                    "url": link,
                    "text": text,
                    "source_page": final_url,
                    "depth": depth + 1,
                }
            if _should_follow_link(link, text, depth=depth, max_depth=max_depth):
                link_canon = _canonicalize_url(link)
                if link_canon not in visited and link not in queued and len(queue) < (max_pages * 3):
                    if depth == 0 and _follow_priority(link, text) >= 20:
                        queue.appendleft((link, depth + 1))
                    else:
                        queue.append((link, depth + 1))
                    queued.add(link)

    return list(candidates.values()), len(visited)


def _discover_for_state(
    abbr: str,
    state_name: str,
    seed_map: dict[str, list[dict]],
    *,
    timeout: int,
    max_pages: int,
    max_depth: int,
    max_per_bucket: int,
) -> tuple[list[dict], int, int]:
    state_slug = _state_slug(state_name)
    seed_urls = _seed_urls_for_state(abbr, seed_map.get(abbr, []))
    if not seed_urls:
        return [], 0, 0

    candidates, pages_crawled = _crawl_state(
        abbr,
        seed_urls,
        timeout=timeout,
        max_pages=max_pages,
        max_depth=max_depth,
    )

    discovered: list[dict] = []
    for bucket in ("proxy_voting", "nonprofit_corp_overlay", "electronic_transactions_overlay"):
        ranked: list[tuple[int, dict]] = []
        for candidate in candidates:
            score = _score_bucket(
                bucket,
                str(candidate.get("url") or ""),
                str(candidate.get("text") or ""),
                state_slug=state_slug,
            )
            if score < MIN_BUCKET_SCORE.get(bucket, 1):
                continue
            ranked.append((score, candidate))

        ranked.sort(key=lambda item: (-item[0], str(item[1].get("url") or "")))
        seen_links: set[str] = set()
        selected = 0
        selected_has_official = False
        for score, candidate in ranked:
            if selected >= max_per_bucket:
                break
            source_url = str(candidate.get("url") or "").strip()
            if not source_url or source_url in seen_links:
                continue
            seen_links.add(source_url)
            selected += 1

            link_text = str(candidate.get("text") or "").strip()
            community_type = _detect_community_type(f"{link_text} {source_url}")
            host = urlparse(source_url).netloc.lower()
            source_type = "statute" if _is_probably_official_host(host) else "secondary_aggregator"
            if source_type == "statute":
                selected_has_official = True
            label = BUCKET_LABELS.get(bucket, bucket.replace("_", " "))
            discovered.append(
                {
                    "jurisdiction": abbr,
                    "community_type": community_type,
                    "entity_form": "unknown",
                    "governing_law_bucket": bucket,
                    "source_type": source_type,
                    "citation": f"{state_name} {label} candidate (discovered)",
                    "source_url": source_url,
                    "publisher": _publisher_from_url(source_url),
                    "priority": 70 + selected,
                    "retrieval_status": "seeded",
                    "verification_status": "discovered_unverified",
                    "notes": (
                        "Automatically discovered candidate from state statute crawl; "
                        f"score={score}; label={link_text[:120]}"
                    ),
                }
            )

        if selected <= 0 or not selected_has_official:
            fallback_url = _fallback_seed_url_for_bucket(seed_urls, bucket=bucket, prefer_official=True)
            if fallback_url:
                host = urlparse(fallback_url).netloc.lower()
                source_type = "statute" if _is_probably_official_host(host) else "secondary_aggregator"
                label = BUCKET_LABELS.get(bucket, bucket.replace("_", " "))
                fallback_terms = ", ".join(BUCKET_FALLBACK_HINTS.get(bucket, ()))
                discovered.append(
                    {
                        "jurisdiction": abbr,
                        "community_type": "hoa",
                        "entity_form": "unknown",
                        "governing_law_bucket": bucket,
                        "source_type": source_type,
                        "citation": f"{state_name} {label} fallback (seed)",
                        "source_url": fallback_url,
                        "publisher": _publisher_from_url(fallback_url),
                        "priority": 96,
                        "retrieval_status": "seeded",
                        "verification_status": "discovered_unverified",
                        "notes": (
                            "Fallback emitted from seed URL because bucket discovery returned no ranked candidates; "
                            f"bucket_hint_terms={fallback_terms}"
                        ),
                    }
                )

    return discovered, pages_crawled, len(seed_urls)


def _fallback_seed_url_for_bucket(seed_urls: list[str], *, bucket: str, prefer_official: bool = False) -> str | None:
    if not seed_urls:
        return None
    hints = tuple(BUCKET_FALLBACK_HINTS.get(bucket, ()))
    statute_hints = ("statute", "statutes", "code", "codes", "law", "laws", "title", "chapter", "section")
    ranked: list[tuple[int, str]] = []
    for url in seed_urls:
        lowered = str(url).lower()
        host = urlparse(lowered).netloc.lower()
        score = 0
        if prefer_official and _is_probably_official_host(host):
            score += 6
        if any(h in lowered for h in hints):
            score += 5
        if any(h in lowered for h in statute_hints):
            score += 3
        if lowered.endswith((".pdf", ".html", ".htm")):
            score += 1
        ranked.append((score, url))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked[0][1] if ranked else None


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    deduped: dict[tuple[str, str, str, str], dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = (
            str(row.get("jurisdiction") or ""),
            str(row.get("community_type") or ""),
            str(row.get("governing_law_bucket") or ""),
            str(row.get("source_url") or ""),
        )
        if not key[0] or not key[2] or not key[3]:
            continue
        deduped[key] = row
    return sorted(
        deduped.values(),
        key=lambda row: (
            str(row.get("jurisdiction") or ""),
            str(row.get("community_type") or ""),
            str(row.get("governing_law_bucket") or ""),
            str(row.get("source_url") or ""),
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover state-level proxy/e-signature candidate statute links from crawlable state statute sources.")
    parser.add_argument("--out", type=Path, default=Path("data/legal/discovered_seeds.json"))
    parser.add_argument("--state", type=str, default=None, help="Optional single state code filter")
    parser.add_argument("--timeout", type=int, default=10, help="HTTP timeout in seconds")
    parser.add_argument("--max-pages-per-state", type=int, default=10, help="Maximum pages to crawl per state")
    parser.add_argument("--max-depth", type=int, default=2, help="Maximum link-follow depth from seed pages")
    parser.add_argument("--max-per-bucket", type=int, default=3, help="Maximum discovered links per bucket and state")
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append/merge into existing output file instead of replacing it.",
    )
    args = parser.parse_args()

    states = US_STATES
    if args.state:
        state = args.state.strip().upper()
        states = [state] if state in STATE_NAMES else []

    seed_map = _load_itep_seed_map(timeout=args.timeout)
    print(f"Loaded seed map for {len(seed_map)} states from {ITEP_STATE_STATUTES_URL}", flush=True)

    rows: list[dict] = []
    for abbr in states:
        state_name = STATE_NAMES.get(abbr)
        if not state_name:
            continue
        found, pages, seed_count = _discover_for_state(
            abbr,
            state_name,
            seed_map,
            timeout=args.timeout,
            max_pages=args.max_pages_per_state,
            max_depth=args.max_depth,
            max_per_bucket=args.max_per_bucket,
        )
        rows.extend(found)
        print(f"{abbr} seeds={seed_count} pages={pages} discovered={len(found)}", flush=True)

    if args.append and args.out.exists():
        try:
            existing = json.loads(args.out.read_text(encoding="utf-8"))
        except Exception:
            existing = []
        if isinstance(existing, list):
            rows.extend(existing)

    rows = _dedupe_rows(rows)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.out} rows={len(rows)}", flush=True)


if __name__ == "__main__":
    main()
