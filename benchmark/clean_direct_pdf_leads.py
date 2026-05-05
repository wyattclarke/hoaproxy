#!/usr/bin/env python3
"""Clean direct-PDF discovery leads before banking.

The Serper collector is intentionally broad. This script downloads only direct
PDF candidates, extracts a short local text sample, rejects non-governing/junk
documents deterministically, repairs HOA names, and writes bank-safe Lead JSONL.
It does not call any model.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.robotparser import RobotFileParser

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from hoaware.doc_classifier import classify_from_filename, classify_from_text  # noqa: E402
from hoaware.discovery.leads import Lead  # noqa: E402


USER_AGENT = (
    os.environ.get("HOA_DISCOVERY_USER_AGENT")
    or "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
)
MAX_PDF_BYTES = 25 * 1024 * 1024
REQUEST_TIMEOUT = 20
PDF_TIMEOUT = 60
BANKABLE_CATEGORIES = {"ccr", "bylaws", "articles", "rules", "amendment", "resolution"}

# Hosts to always block regardless of which state we're running. Things that
# are clearly not a public HOA governing-document source — real estate, social,
# document-share sites, scrape farms.
_UNIVERSAL_BLOCKED_HOSTS = [
    "caionline", "landhub", "showcase", "chicagotitle", "era", "realestate",
    "parksauction", "auction", "zillow", "redfin", "realtor", "trulia",
    "apartments", "rent", "scribd", "issuu", "pdfcoffee", "uml\\.edu\\.ni",
]

# Per-state block lists: legislative + agenda/planning sites that consistently
# return statute text or government-meeting PDFs instead of HOA documents.
# Add entries when you discover a state-specific noisy host. The current
# state's entries are *included* (we always want to filter our own state's
# legislature/court out of the results).
_STATE_BLOCKED_HOSTS: dict[str, list[str]] = {
    "TN": [
        "tn\\.gov", "legmt", "pbcgov", "tpucdockets", "rutherfordcountytn",
        "nashville\\.gov",
    ],
    "GA": [
        "legis\\.ga\\.gov", "ecorp\\.sos\\.ga", "luederlaw",
    ],
    "KS": [
        "kslegislature", "ksrevenue\\.gov",
    ],
}


def _build_blocked_host_re(args: argparse.Namespace) -> re.Pattern[str]:
    """Universal hosts plus this run's state-specific blocked-host list."""
    parts = list(_UNIVERSAL_BLOCKED_HOSTS)
    parts.extend(_STATE_BLOCKED_HOSTS.get((args.state or "").upper(), []))
    return re.compile(r"(^|\.)(" + "|".join(parts) + r")\b", re.IGNORECASE)
JUNK_RE = re.compile(
    r"\b(minutes?|agenda|newsletter|budget|financial|audit|reserve study|"
    r"rental|lease|pool|welcome package|application|form|directory|roster|"
    r"violation|estoppel|closing|court|lawsuit|docket|bankruptcy|"
    r"legislative session report|overview of concerns|case\s+\d|"
    r"property information packet|listing|mls|for sale|handbook)\b",
    re.IGNORECASE,
)
ASSOC_RE = re.compile(
    r"([A-Z][A-Za-z0-9&'., -]{2,100}?\s+(?:Homeowners?|Home Owners?|Homes|Property Owners?|Owners|Condominium|Townhomes?)\s+Association(?:,\s*Inc\.?)?)",
    re.IGNORECASE,
)
HOA_RE = re.compile(r"\b(hoa|homeowners?|home owners?|owners association|homes association|association)\b", re.IGNORECASE)
COMMUNITY_RE = re.compile(
    r"\b(estates?|creek|lakes?|hills?|ridge|woods?|villas?|village|place|park|"
    r"trails?|crossing|landing|farms?|meadows?|point|reserve|springs?|bend|"
    r"run|grove|harbor|plantation|preserve|commons|square|trace|bay|fields?)\b",
    re.IGNORECASE,
)


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) in {"1", "true", "True", "yes", "YES"}


def _jsonl_rows(path: Path) -> list[dict]:
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _audit_by_url(path: Path | None) -> dict[str, dict]:
    if not path or not path.exists():
        return {}
    out: dict[str, dict] = {}
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") != "lead":
                continue
            lead = row.get("lead") or {}
            url = lead.get("source_url")
            if url:
                out[url] = row
    return out


def _robots_allowed(session: requests.Session, url: str) -> bool:
    if not _env_bool("HOA_DISCOVERY_RESPECT_ROBOTS", "0"):
        return True
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    parser = RobotFileParser(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
    try:
        response = session.get(parser.url, headers={"User-Agent": USER_AGENT}, timeout=5, allow_redirects=True)
        if response.status_code >= 400:
            return True
        parser.parse(response.text.splitlines())
        return parser.can_fetch(USER_AGENT, url)
    except requests.RequestException:
        return True


def _download_pdf(session: requests.Session, url: str) -> tuple[bytes | None, str | None]:
    if not _robots_allowed(session, url):
        return None, "robots_disallowed"
    try:
        head = session.head(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        size = int(head.headers.get("Content-Length") or 0)
        if size > MAX_PDF_BYTES:
            return None, f"too_large_{size}"
    except requests.RequestException:
        pass
    try:
        response = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=PDF_TIMEOUT, stream=True, allow_redirects=True)
        if response.status_code != 200:
            return None, f"status_{response.status_code}"
        buf = bytearray()
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > MAX_PDF_BYTES:
                response.close()
                return None, "too_large_streamed"
        response.close()
        data = bytes(buf)
        if not data.startswith(b"%PDF-"):
            return None, "not_pdf"
        return data, None
    except requests.RequestException as exc:
        return None, f"request_{type(exc).__name__}"


def _extract_text(pdf_bytes: bytes, max_pages: int) -> str:
    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for page in reader.pages[:max_pages]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                pass
        return "\n".join(parts)
    except Exception:
        return ""


def _filename(url: str) -> str:
    path = urlparse(url).path.rsplit("/", 1)[-1] or "document.pdf"
    return unquote(path)[:180]


def _clean_name(raw: str, state_name: str | None = None) -> str | None:
    name = raw or ""
    name = re.sub(r"\s+", " ", name.replace("\n", " "))
    name = re.sub(r"(?i)^\s*\[?pdf\]?\s*", "", name)
    name = re.sub(r"(?i)^(?:untitled|this instrument prepared from|recorded instrument)\s*[-:]*\s*", "", name)
    name = re.sub(r"(?i)^(?:article\s+[ivx\d]+\.?|revised\s+[a-z]+\s+\d+\s*,?\s*\d{4}\s*\d*)\s+", "", name)
    name = re.sub(r"(?i)^(?:shall\s+mean(?:\s+and\s+refer\s+to)?(?:\s+the)?|means(?:\s+the)?|by|known\s+and\s+identified\s+as|"
                  r"name\s+of\s+(?:this\s+)?(?:non-profit\s+)?corporation\s+shall\s+be|"
                  r"is\s+made\s+this.*?\bby|profit\s+corporation\s+known\s+and\s+identified\s+as)\s+", "", name)
    name = re.sub(r"(?i)^community\s+understand\s+the\s+rules\s+of\s+the\s+", "", name)
    if re.search(r"(?i)\b(initial text of which|membership in the association|welcome package|our community is proud)\b", name):
        return None
    name = re.sub(r"(?i)\b(?:incorporated|inc\.?|llc|l\.l\.c\.)\b", "", name)
    name = re.sub(r"(?i)\bhome\s*owner'?s?\s+association\b", "Homeowners Association", name)
    name = re.sub(r"(?i)\bhomes\s+association\b", "Homes Association", name)
    name = re.sub(r"(?i)\bhomeowners?\s+association\b", "Homeowners Association", name)
    # Strip trailing "<HOA>, a <state> nonprofit corporation ..." boilerplate
    # for whichever state we're running. Skip when state_name is unset so
    # callers that don't pass it (legacy sites, tests) don't trip.
    if state_name:
        s = re.escape(state_name)
        name = re.sub(rf"(?i)\s+(?:a|an)\s+{s}\s+(?:nonprofit|not[- ]for[- ]profit|corporation).*$", "", name)
        name = re.sub(rf"(?i),?\s+(?:a\s+)?{s}\s+(?:nonprofit|not[- ]for[- ]profit|corporation).*$", "", name)
    name = re.sub(r"(?i)\b(?:the\s+)?undersigned.*$", "", name)
    name = re.sub(r"(?i)^(?:of|for|and|the|charter|bylaws?|declaration|amended|restated|restrictive|covenants?|conditions|restrictions|rules|regulations)\s+", "", name)
    name = re.sub(r"(?i)\b(?:declaration|covenants?|conditions|restrictions?|bylaws?|rules?|regulations?|architectural|guidelines?|amendments?|recorded|final|searchable|pdf)\b", " ", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(" .,-:;()[]")
    if not name or len(name) < 5 or len(name) > 120:
        return None
    if re.search(r"(?i)\b(godaddy|rackcdn|wordpress|county register|suite|street|overview|document|untitled)\b", name):
        return None
    if not HOA_RE.search(name):
        if not COMMUNITY_RE.search(name):
            return None
        name = f"{name} HOA"
    return name


def _name_from_filename(filename: str, state_name: str | None = None) -> str | None:
    stem = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    stem = unquote(stem)
    stem = re.sub(r"[_+%-]+", " ", stem)
    stem = re.split(
        r"(?i)\b(declaration|covenants?|conditions|restrictions?|bylaws?|rules?|regulations?|architectural|guidelines?|amendments?|restated|recorded|signed|final|searchable)\b",
        stem,
        maxsplit=1,
    )[0]
    return _clean_name(stem, state_name=state_name)


def infer_name(row: dict, audit: dict | None, text: str, url: str, args: argparse.Namespace | None = None) -> str | None:
    candidates: list[str] = []
    if audit:
        snippet = str(audit.get("snippet") or "")
        title = str(audit.get("title") or "")
        for pattern in [
            r"([A-Z][A-Za-z0-9&'., -]{2,80}?\s+(?:Homeowners?|Home Owners?|Homes|Property Owners?|Owners|Condominium|Townhomes?)\s+Association)",
            r"([A-Z][A-Za-z0-9&'., -]{2,80}?\s+HOA)",
        ]:
            candidates.extend(m.group(1) for m in re.finditer(pattern, f"{title} {snippet}", re.IGNORECASE))
        candidates.append(title)
        candidates.append(snippet)
    for match in ASSOC_RE.finditer(text[:8000]):
        candidates.append(match.group(1))
    for pattern in [
        r"(?:Association[\"”']?\s*(?:shall mean|means)\s+(?:the\s+)?)([A-Z][A-Za-z0-9&'., -]{3,100})",
        r"(?:Declaration .*? for|Restrictions .*? for|Bylaws? of)\s+(?:the\s+)?([A-Z][A-Za-z0-9&'., -]{3,100})",
        r"(?:known as|commonly known as)\s+(?:the\s+)?([A-Z][A-Za-z0-9&'., -]{3,80})",
    ]:
        for match in re.finditer(pattern, text[:8000], re.IGNORECASE | re.DOTALL):
            candidates.append(match.group(1))
    state_name = (args.state_name if args is not None else None) or None
    state_abbrev = (args.state if args is not None else None) or ""
    candidates.append(str(row.get("name") or ""))
    candidates.append(_name_from_filename(_filename(url), state_name=state_name) or "")
    host = re.sub(r"^www\.", "", urlparse(url).netloc.lower()).split(".", 1)[0]
    # Strip trailing "hoa" and the running state's two-letter abbrev (e.g. "tn")
    # from hostname slugs so e.g. "fooga.com" -> "foo" for a GA run.
    host_strip = "hoa"
    if state_abbrev and len(state_abbrev) == 2:
        host_strip = f"hoa|{state_abbrev.lower()}"
    host = re.sub(rf"({host_strip})$", "", host)
    candidates.append(host.replace("-", " "))

    best: tuple[int, str] | None = None
    for candidate in candidates:
        cleaned = _clean_name(candidate, state_name=state_name)
        if not cleaned:
            continue
        score = 0
        if "association" in cleaned.lower():
            score += 5
        if COMMUNITY_RE.search(cleaned):
            score += 2
        if re.search(r"(?i)\b(document|untitled|godaddy|rackcdn|wordpress|county register|suite|street|overview)\b", cleaned):
            score -= 6
        if best is None or score > best[0]:
            best = (score, cleaned)
    if best and best[0] >= 2:
        return best[1]
    return None


def _state_ok(text: str, metadata: str, args: argparse.Namespace) -> bool:
    hay = f"{metadata}\n{text[:5000]}"
    state_terms = [args.state_name, args.state]
    state_terms.extend(args.state_hint)
    return any(re.search(rf"\b{re.escape(term)}\b", hay, re.IGNORECASE) for term in state_terms if term)


# US state name -> two-letter abbreviation. Used by detect_state_county()
# to re-route a lead to the actual state/county shown by the PDF text,
# even if the sweep targeted a different state. Per the playbook,
# out-of-state and out-of-county hits are free wins, not rejects.
_US_STATES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}

# Sorted by length desc so "north carolina" matches before "carolina".
_US_STATE_NAMES_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in sorted(_US_STATES.keys(), key=lambda s: -len(s))) + r")\b",
    re.IGNORECASE,
)
# "<County> County, <state>" — captures both county name and state. Requires
# Title Case so we don't pick up "Mentions Fulton County, Georgia" as a
# county named "Mentions Fulton". Allows 1-3 word counties (Jeff Davis,
# St. Marys, Prince George's). Case-insensitive on the state name only.
_COUNTY_STATE_RE = re.compile(
    r"\b((?:[A-Z][A-Za-z'.]+\s+){0,2}[A-Z][A-Za-z'.]+)\s+County,?\s+(?:Georgia|Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia)\b",
)
# Re-extract the state separately because the long alternation above is
# anchored via a non-capturing group; we need a paired re for state too.
_COUNTY_STATE_PAIR_RE = re.compile(
    r"\b((?:[A-Z][A-Za-z'.]+\s+){0,2}[A-Z][A-Za-z'.]+)\s+County,?\s+("
    + "|".join(re.escape(n.title()) for n in sorted(_US_STATES.keys(), key=lambda s: -len(s)))
    + r")\b",
)


def detect_state_county(text: str) -> tuple[str | None, str | None]:
    """Best-effort (state_abbrev, county) inference from PDF text.

    Strategy:
      1. Look for "<County> County, <State>" patterns first — strongest
         signal, gives both fields at once. Pick the most-mentioned pair.
      2. Fall back to bare state-name mentions; pick the most-mentioned.
      3. Returns (None, None) if nothing distinctive found — caller
         should keep its original sweep-driven state/county.

    Both returns are *suggestions*; the caller may still prefer its
    sweep's --default-county if the hint is weaker than the lead's
    pre-existing county evidence.
    """
    if not text:
        return None, None

    # Pair-based: "<County> County, <State>".
    pair_counts: dict[tuple[str, str], int] = {}
    for match in _COUNTY_STATE_PAIR_RE.finditer(text):
        county_raw = match.group(1).strip()
        state_raw = match.group(2).strip().lower()
        # Reject pseudo-counties from PDF noise ("Of County", "This County").
        if not re.search(r"[A-Za-z]{3,}", county_raw) or county_raw.lower() in {
            "the", "this", "of", "and", "for", "in", "by", "on",
        }:
            continue
        county = county_raw
        state = _US_STATES.get(state_raw)
        if not state:
            continue
        key = (state, county)
        pair_counts[key] = pair_counts.get(key, 0) + 1
    if pair_counts:
        (state, county), _ = max(pair_counts.items(), key=lambda kv: kv[1])
        return state, county

    # State-only fallback: which state name is mentioned most?
    state_counts: dict[str, int] = {}
    for match in _US_STATE_NAMES_RE.finditer(text):
        state = _US_STATES.get(match.group(1).lower())
        if state:
            state_counts[state] = state_counts.get(state, 0) + 1
    if state_counts:
        return max(state_counts.items(), key=lambda kv: kv[1])[0], None

    return None, None


def clean(args: argparse.Namespace) -> int:
    rows = _jsonl_rows(Path(args.input))
    audit_map = _audit_by_url(Path(args.audit)) if args.audit else {}
    session = requests.Session()
    accepted: list[dict] = []
    seen_urls: set[str] = set()
    reject_path = Path(args.rejects)
    reject_path.parent.mkdir(parents=True, exist_ok=True)
    blocked_host_re = _build_blocked_host_re(args)
    target_state = (args.state or "").upper()
    with reject_path.open("w") as rejects:
        for row in rows:
            pdfs = row.get("pre_discovered_pdf_urls") or []
            if len(pdfs) != 1:
                print(json.dumps({"row": row, "reason": "not_single_direct_pdf"}, sort_keys=True), file=rejects)
                continue
            url = str(pdfs[0]).split("#", 1)[0]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            host = urlparse(url).netloc.lower()
            audit = audit_map.get(str(row.get("source_url") or url), {})
            metadata = " ".join([
                str(row.get("name") or ""),
                str(audit.get("title") or ""),
                str(audit.get("snippet") or ""),
                url,
            ])
            # Note: blocked_host_re still hard-rejects scrape farms, real estate
            # sites, etc.; JUNK_RE still hard-rejects newsletters/minutes/etc.
            # Out-of-state was *previously* a hard reject — now we route the
            # lead to the actual state we detect from the PDF text instead.
            if blocked_host_re.search(host) or JUNK_RE.search(metadata):
                print(json.dumps({"url": url, "reason": "metadata_reject", "metadata": metadata[:300]}, sort_keys=True), file=rejects)
                continue
            pdf_bytes, skip = _download_pdf(session, url)
            if skip or not pdf_bytes:
                print(json.dumps({"url": url, "reason": skip or "download_failed"}, sort_keys=True), file=rejects)
                continue
            text = _extract_text(pdf_bytes, args.max_pages)

            # Determine the lead's actual (state, county) from the PDF.
            # Priority order:
            #   1. PDF-detected state+county (strongest signal — the
            #      recorded declaration usually says "X County, <State>").
            #   2. Sweep's --default-county + --state (fallback).
            # If the detected state is *different* from the sweep's
            # target, we re-route. Per the playbook, a TN HOA found by a
            # GA sweep banks under v1/TN/<county>/<slug>/ and contributes
            # to the eventual TN pass.
            detected_state, detected_county = detect_state_county(text[:25000])
            if detected_state:
                final_state = detected_state
                # If we detected a county, use it. Otherwise: when the
                # detected state matches the sweep target, fall back to
                # the sweep's --default-county (it was a hint about
                # which county we expected). When the detected state is
                # *different* from the target, drop the sweep's county
                # — it's a GA county and doesn't apply to the new state.
                # Leave county null so a future state-specific backfill
                # can route within that state.
                if detected_county:
                    final_county = detected_county
                elif final_state == target_state:
                    final_county = row.get("county")
                else:
                    final_county = None
            else:
                # No PDF state evidence at all. Skip — banking under the
                # sweep's target state with zero corroboration would be
                # unsafe (the lead might be from anywhere).
                if not _state_ok(text, metadata, args):
                    print(json.dumps({"url": url, "reason": "no_state_evidence"}, sort_keys=True), file=rejects)
                    continue
                final_state = target_state
                final_county = row.get("county")

            clf = classify_from_text(text, str(row.get("name") or "")) if text else None
            if not clf:
                clf = classify_from_filename(_filename(url))
            category = str((clf or {}).get("category") or "")
            if category not in BANKABLE_CATEGORIES:
                print(json.dumps({"url": url, "reason": "category_reject", "category": category, "metadata": metadata[:300]}, sort_keys=True), file=rejects)
                continue
            name = infer_name(row, audit, text, url, args)
            if not name:
                print(json.dumps({"url": url, "reason": "name_unresolved", "metadata": metadata[:300]}, sort_keys=True), file=rejects)
                continue
            lead = Lead(
                name=name,
                source=f"clean-direct-pdf-{target_state.lower()}",
                source_url=url,
                state=final_state,
                city=row.get("city"),
                county=final_county,
                website=None,
            )
            payload = asdict(lead)
            payload["pre_discovered_pdf_urls"] = [url]
            payload["cleaning"] = {
                "category": category,
                "method": (clf or {}).get("method"),
                "confidence": (clf or {}).get("confidence"),
                "rerouted": final_state != target_state,
                "detected_state": detected_state,
                "detected_county": detected_county,
            }
            accepted.append(payload)
            if args.delay:
                time.sleep(args.delay)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.output).open("w") as f:
        for row in accepted[: args.max_output]:
            print(json.dumps(row, sort_keys=True), file=f)
    print(json.dumps({"input": len(rows), "accepted": min(len(accepted), args.max_output), "output": args.output, "rejects": args.rejects}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean direct-PDF HOA leads without model calls")
    parser.add_argument("input")
    parser.add_argument("--audit")
    parser.add_argument("--output", required=True)
    parser.add_argument("--rejects", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--state-name", required=True)
    parser.add_argument("--state-hint", action="append", default=[])
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--max-output", type=int, default=1000)
    parser.add_argument("--delay", type=float, default=0.5)
    args = parser.parse_args()
    args.state = args.state.upper()
    return clean(args)


if __name__ == "__main__":
    raise SystemExit(main())
