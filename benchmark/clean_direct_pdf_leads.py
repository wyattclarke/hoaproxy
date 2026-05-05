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

BLOCKED_HOST_RE = re.compile(
    r"(^|\.)("
    r"tn\.gov|caionline|legmt|pbcgov|tpucdockets|rutherfordcountytn|nashville\.gov|"
    r"landhub|showcase|chicagotitle|era|realestate|parksauction|auction|"
    r"zillow|redfin|realtor|trulia|apartments|rent|scribd|issuu|pdfcoffee|"
    r"uml\.edu\.ni"
    r")\b",
    re.IGNORECASE,
)
JUNK_RE = re.compile(
    r"\b(minutes?|agenda|newsletter|budget|financial|audit|reserve study|"
    r"rental|lease|pool|welcome package|application|form|directory|roster|"
    r"violation|estoppel|closing|court|lawsuit|docket|bankruptcy|"
    r"legislative session report|overview of concerns|case\s+\d|"
    r"property information packet|listing|mls|for sale|handbook)\b",
    re.IGNORECASE,
)
OUT_OF_STATE_RE = re.compile(
    r"\b(florida|california|massachusetts|missouri|georgia|kentucky|north carolina|"
    r"south carolina|alabama|mississippi|arkansas|virginia|texas|palm beach|"
    r"holbrook,\s*ma|chicago title|pleasant prairie|brentwood hoa.*palm beach)\b",
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


def _clean_name(raw: str) -> str | None:
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
    name = re.sub(r"(?i)\s+(?:a|an)\s+tennessee\s+(?:nonprofit|not[- ]for[- ]profit|corporation).*$", "", name)
    name = re.sub(r"(?i),?\s+(?:a\s+)?tennessee\s+(?:nonprofit|not[- ]for[- ]profit|corporation).*$", "", name)
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


def _name_from_filename(filename: str) -> str | None:
    stem = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    stem = unquote(stem)
    stem = re.sub(r"[_+%-]+", " ", stem)
    stem = re.split(
        r"(?i)\b(declaration|covenants?|conditions|restrictions?|bylaws?|rules?|regulations?|architectural|guidelines?|amendments?|restated|recorded|signed|final|searchable)\b",
        stem,
        maxsplit=1,
    )[0]
    return _clean_name(stem)


def infer_name(row: dict, audit: dict | None, text: str, url: str) -> str | None:
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
    candidates.append(str(row.get("name") or ""))
    candidates.append(_name_from_filename(_filename(url)) or "")
    host = re.sub(r"^www\.", "", urlparse(url).netloc.lower()).split(".", 1)[0]
    host = re.sub(r"(hoa|tn)$", "", host)
    candidates.append(host.replace("-", " "))

    best: tuple[int, str] | None = None
    for candidate in candidates:
        cleaned = _clean_name(candidate)
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


def _build_out_of_state_re(args: argparse.Namespace) -> re.Pattern[str]:
    """Drop the current state's name from OUT_OF_STATE_RE so target hits are kept."""
    skip = {(args.state_name or "").lower(), (args.state or "").lower()}
    skip.update({(h or "").lower() for h in (args.state_hint or [])})
    pattern_parts = [
        "florida", "california", "massachusetts", "missouri", "georgia",
        "kentucky", "north carolina", "south carolina", "alabama",
        "mississippi", "arkansas", "virginia", "texas", "tennessee",
        "kansas", "oklahoma", "ohio", "michigan", "illinois",
        "palm beach", "holbrook,\\s*ma", "chicago title",
        "pleasant prairie", "brentwood hoa.*palm beach",
    ]
    pattern_parts = [p for p in pattern_parts if p.lower() not in skip]
    return re.compile(r"\b(" + "|".join(pattern_parts) + r")\b", re.IGNORECASE)


def clean(args: argparse.Namespace) -> int:
    rows = _jsonl_rows(Path(args.input))
    audit_map = _audit_by_url(Path(args.audit)) if args.audit else {}
    session = requests.Session()
    accepted: list[dict] = []
    seen_urls: set[str] = set()
    reject_path = Path(args.rejects)
    reject_path.parent.mkdir(parents=True, exist_ok=True)
    out_of_state_re = _build_out_of_state_re(args)
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
            if BLOCKED_HOST_RE.search(host) or JUNK_RE.search(metadata) or out_of_state_re.search(metadata):
                print(json.dumps({"url": url, "reason": "metadata_reject", "metadata": metadata[:300]}, sort_keys=True), file=rejects)
                continue
            pdf_bytes, skip = _download_pdf(session, url)
            if skip or not pdf_bytes:
                print(json.dumps({"url": url, "reason": skip or "download_failed"}, sort_keys=True), file=rejects)
                continue
            text = _extract_text(pdf_bytes, args.max_pages)
            if out_of_state_re.search(text[:5000]):
                print(json.dumps({"url": url, "reason": "text_out_of_state"}, sort_keys=True), file=rejects)
                continue
            if not _state_ok(text, metadata, args):
                print(json.dumps({"url": url, "reason": "no_state_evidence"}, sort_keys=True), file=rejects)
                continue
            clf = classify_from_text(text, str(row.get("name") or "")) if text else None
            if not clf:
                clf = classify_from_filename(_filename(url))
            category = str((clf or {}).get("category") or "")
            if category not in BANKABLE_CATEGORIES:
                print(json.dumps({"url": url, "reason": "category_reject", "category": category, "metadata": metadata[:300]}, sort_keys=True), file=rejects)
                continue
            name = infer_name(row, audit, text, url)
            if not name:
                print(json.dumps({"url": url, "reason": "name_unresolved", "metadata": metadata[:300]}, sort_keys=True), file=rejects)
                continue
            lead = Lead(
                name=name,
                source=f"clean-direct-pdf-{args.state.lower()}",
                source_url=url,
                state=args.state.upper(),
                city=row.get("city"),
                county=row.get("county"),
                website=None,
            )
            payload = asdict(lead)
            payload["pre_discovered_pdf_urls"] = [url]
            payload["cleaning"] = {
                "category": category,
                "method": (clf or {}).get("method"),
                "confidence": (clf or {}).get("confidence"),
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
