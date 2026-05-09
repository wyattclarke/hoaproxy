#!/usr/bin/env python3
"""Deterministic public HOA governing-document discovery for one state.

This is the state-generic successor to the Kansas Serper collector. It searches
with caller-supplied queries, filters public document candidates locally, writes
Lead JSONL, and can optionally probe/bank those leads through hoaware.discovery.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from hoaware.discovery.leads import Lead  # noqa: E402
from hoaware.discovery.probe import probe  # noqa: E402


SERPER_ENDPOINT = "https://google.serper.dev/search"
USER_AGENT = (
    os.environ.get("HOA_DISCOVERY_USER_AGENT")
    or "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
)
REQUEST_TIMEOUT = 20
PAGE_HINT_TIMEOUT = 10

DOC_RE = re.compile(
    r"\b(bylaws?|by-laws?|covenants?|declaration|cc&?rs?|restrictions?|"
    r"deed restrictions?|governing documents?|hoa documents?|association documents?|"
    r"articles? of incorporation|rules?|regulations?|resolutions?|"
    r"architectural|design guidelines?)\b",
    re.IGNORECASE,
)
HOA_RE = re.compile(
    r"\b(hoa|homeowners?|home owners?|homes association|owners association|"
    r"property owners association|condominium association|townhome association)\b",
    re.IGNORECASE,
)
PRIVATE_RE = re.compile(
    r"(townsq|frontsteps|cincsystems|cincweb|appfolio|buildium|enumerateengage|"
    r"caliber\.cloud|/login|/signin|/account|/resident|drive\.google\.com|"
    r"docs\.google\.com)",
    re.IGNORECASE,
)
BLOCKED_HOST_RE = re.compile(
    r"(^|\.)("
    r"facebook|tiktok|instagram|reddit|nextdoor|hopb|hoamanagement|steadily|doorloop|"
    r"nolo|rocketlawyer|avvo|findlaw|justia|caselaw|trellis|lawinsider|"
    r"uslegalforms|scribd|issuu|yumpu|dokumen|pdfcoffee|fliphtml5|"
    r"zillow|redfin|realtor|trulia|homes|coldwellbanker|remax|compass|"
    r"century21|movoto|estately|realty|mls|55places|apartments|rent|"
    r"homeownersassociationdirectory|hoa-community|communitypay|zoominfo|"
    r"propublica|irs|sec|bbb|yelp|indeed|glassdoor"
    r")\.",
    re.IGNORECASE,
)
JUNK_RE = re.compile(
    r"\b(minutes?|agenda|newsletter|budget|financial|audit|rental|apartments?|"
    r"lease|pool|pass|directory|roster|violation|estoppel|closing|coupon|"
    r"court|lawsuit|docket|bankruptcy|tax filing|form 990|for sale|sold|"
    r"listing|mls|city council|planning commission|packet|application form)\b",
    re.IGNORECASE,
)
COMMUNITY_RE = re.compile(
    r"\b(hoa|homeowners?|homes|owners|association|condominium|townhomes?|"
    r"estates?|creek|lakes?|hills?|ridge|woods?|villas?|village|place|park|"
    r"trails?|crossing|landing|farms?|meadows?|point|reserve|springs?|bend|"
    r"run|grove|harbor|plantation|preserve|commons|square|trace)\b",
    re.IGNORECASE,
)
GENERIC_NAME_RE = re.compile(
    r"^(home|hoa|documents?|governing documents?|hoa documents?|association documents?|"
    r"bylaws?|covenants?|restrictions?|declaration|resources?|helpful links|"
    r"architectural guidelines?|rules and regulations)$",
    re.IGNORECASE,
)


def _now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _jsonl_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        print(json.dumps(payload, sort_keys=True), file=f)


def _is_pdf_url(url: str) -> bool:
    clean = url.lower().split("?", 1)[0].split("#", 1)[0]
    return clean.endswith(".pdf") or "format=pdf" in url.lower()


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) in {"1", "true", "True", "yes", "YES"}


def _state_regex(args: argparse.Namespace) -> re.Pattern[str]:
    parts = [re.escape(args.state), re.escape(args.state_name)]
    parts.extend(re.escape(v) for v in args.state_hint)
    parts.extend(re.escape(v) for v in args.city)
    parts.extend(re.escape(v) for v in args.county)
    pattern = r"\b(" + "|".join(p for p in parts if p) + r")\b"
    return re.compile(pattern, re.IGNORECASE)


def _robots_allowed(session: requests.Session, url: str) -> bool:
    if not _env_bool("HOA_DISCOVERY_RESPECT_ROBOTS", "0"):
        return True
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser(robots_url)
    try:
        response = session.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=5, allow_redirects=True)
        if response.status_code >= 400:
            return True
        parser.parse(response.text.splitlines())
        return parser.can_fetch(USER_AGENT, url)
    except requests.RequestException:
        return True


def _title_case_slug(value: str) -> str:
    value = re.sub(r"%20|\+", " ", value)
    value = re.sub(r"[-_./]+", " ", value)
    value = re.sub(r"\b(hoa|inc|org|com|net|pdf)\b$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    small = {"of", "and", "the", "at"}
    words = []
    for idx, word in enumerate(value.split()):
        lw = word.lower()
        words.append(lw if idx and lw in small else lw.capitalize())
    return " ".join(words)


def _clean_name(value: str, args: argparse.Namespace) -> str | None:
    name = re.sub(r"(?i)^\s*\[?pdf\]?\s*", "", value or "")
    name = name.split("|", 1)[0]
    name = re.sub(
        r"(?i)\b(home|page|official website|documents?|governing documents?|hoa documents?|"
        r"association documents?|bylaws?|by-laws?|covenants?|declaration|restrictions?|"
        r"deed restrictions?|rules?|regulations?|architectural guidelines?|resources?)\b",
        " ",
        name,
    )
    name = re.sub(rf"(?i),?\s+(?:{re.escape(args.state_name)}|{re.escape(args.state)})\b", " ", name)
    for city in args.city:
        name = re.sub(rf"(?i)\b(?:in|at)\s+{re.escape(city)}\b", " ", name)
    name = re.sub(r"[_/\\]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .,-:")
    if (
        not name
        or GENERIC_NAME_RE.search(name)
        or len(name) < 4
        or len(name) > 100
        or JUNK_RE.search(name)
    ):
        return None
    if not HOA_RE.search(name):
        name = f"{name} HOA"
    return name[:120]


def infer_name(title: str, url: str, snippet: str, args: argparse.Namespace) -> str:
    candidates: list[str] = []
    hay = " ".join([title or "", snippet or ""])
    for pattern in [
        r"([A-Z][A-Za-z0-9&'., -]{2,85}\s+(?:Homes|Homeowners?|Home Owners?|Property Owners?|Owners|Condominium|Townhomes?)\s+Association)",
        r"([A-Z][A-Za-z0-9&'., -]{2,85}\s+HOA)",
    ]:
        candidates.extend(m.group(1) for m in re.finditer(pattern, hay))
    if title:
        parts = [p.strip() for p in re.split(r"\s+[|–—]\s+|\s+-\s+", title) if p.strip()]
        candidates.extend(parts)
        candidates.extend(reversed(parts))
        candidates.append(title)
    parsed = urlparse(url)
    path_bits = [p for p in parsed.path.split("/") if p]
    for bit in reversed(path_bits[-4:]):
        if COMMUNITY_RE.search(bit):
            candidates.append(_title_case_slug(bit))
    host = re.sub(r"^www\.", "", parsed.netloc.lower()).split(".", 1)[0]
    if host and not re.search(r"^(portal|login|docs|files|cdn|assets|storage|s3)$", host):
        candidates.append(_title_case_slug(host))
    candidates.append(snippet)

    best: tuple[int, str] | None = None
    for candidate in candidates:
        cleaned = _clean_name(candidate, args)
        if not cleaned:
            continue
        score = 0
        if HOA_RE.search(cleaned):
            score += 4
        if re.search(r"\b(association|homes|homeowners?|owners|condominium|townhomes?)\b", cleaned, re.IGNORECASE):
            score += 3
        if COMMUNITY_RE.search(cleaned):
            score += 2
        if re.search(r"(?i)^(hoa|homes association|homeowners association|owners association)\b", cleaned):
            score -= 8
        if best is None or score > best[0]:
            best = (score, cleaned)
    if best and best[0] >= 2:
        return best[1]
    return _clean_name(host, args) or f"Unknown {args.state} HOA"


def load_queries(path: str) -> list[str]:
    with open(path) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def serper_search(query: str, *, num: int, page: int) -> list[dict]:
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        raise RuntimeError("SERPER_API_KEY is required")
    headers = {"X-API-KEY": key, "Content-Type": "application/json"}
    payload = {"q": query, "num": num, "page": page, "gl": "us", "hl": "en"}
    response = requests.post(SERPER_ENDPOINT, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"Serper {response.status_code}: {response.text[:300]}")
    return list(response.json().get("organic", []))


def result_score(row: dict, state_re: re.Pattern[str]) -> int:
    link = str(row.get("link") or "")
    hay = " ".join([str(row.get("title") or ""), link, str(row.get("snippet") or "")])
    host = urlparse(link).netloc.lower()
    score = 0
    if BLOCKED_HOST_RE.search(host) or PRIVATE_RE.search(link):
        return -30
    if DOC_RE.search(hay):
        score += 6
    if HOA_RE.search(hay):
        score += 4
    if state_re.search(hay):
        score += 3
    if COMMUNITY_RE.search(host):
        score += 2
    if _is_pdf_url(link):
        score += 1
    if JUNK_RE.search(hay):
        score -= 6
    return score


def fetch_page_hint(session: requests.Session, url: str) -> tuple[str, str]:
    if _is_pdf_url(url) or PRIVATE_RE.search(url) or not _robots_allowed(session, url):
        return "", ""
    try:
        response = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=PAGE_HINT_TIMEOUT, allow_redirects=True)
        if response.status_code != 200:
            return "", ""
        ctype = (response.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            return "", ""
        soup = BeautifulSoup(response.text[:400_000], "html.parser")
        title = " ".join((soup.title.get_text(" ") if soup.title else "").split())
        heading_el = soup.find(["h1", "h2"])
        heading = " ".join((heading_el.get_text(" ") if heading_el else "").split())
        body = " ".join(soup.get_text(" ").split())[:1600]
        return " | ".join([p for p in (title, heading) if p]), body
    except requests.RequestException:
        return "", ""


def discover(args: argparse.Namespace, run_dir: Path) -> list[tuple[Lead, list[str]]]:
    audit = run_dir / "audit.jsonl"
    state_re = _state_regex(args)
    session = requests.Session()
    rows: list[dict] = []
    search_calls = 0
    for query in load_queries(args.queries_file)[: args.max_queries]:
        for page in range(1, args.pages_per_query + 1):
            try:
                found = serper_search(query, num=args.results_per_query, page=page)
                search_calls += 1
                _jsonl_write(audit, {"event": "serper", "query": query, "page": page, "count": len(found)})
                for row in found:
                    row["_query"] = query
                    rows.append(row)
            except Exception as exc:
                _jsonl_write(audit, {"event": "serper_failed", "query": query, "page": page, "error": str(exc)})
            time.sleep(args.search_delay)

    by_url: dict[str, dict] = {}
    for row in rows:
        link = str(row.get("link") or "").split("#", 1)[0].strip()
        if not link.startswith(("http://", "https://")):
            continue
        if link not in by_url or result_score(row, state_re) > result_score(by_url[link], state_re):
            row["link"] = link
            by_url[link] = row

    ranked = sorted(by_url.values(), key=lambda row: result_score(row, state_re), reverse=True)
    leads: list[tuple[Lead, list[str]]] = []
    seen_lead_keys: set[tuple[str, str]] = set()
    for row in ranked:
        if args.max_leads > 0 and len(leads) >= args.max_leads:
            break
        score = result_score(row, state_re)
        if score < args.min_score:
            continue
        link = str(row.get("link") or "")
        if _is_pdf_url(link) and not args.include_direct_pdfs:
            continue
        title = str(row.get("title") or "")
        snippet = str(row.get("snippet") or "")
        page_title, page_body = fetch_page_hint(session, link) if args.fetch_pages else ("", "")
        state_evidence = " ".join([title, snippet, link, page_title, page_body[:800]])
        if args.require_state_hint and not state_re.search(state_evidence):
            continue
        name = infer_name(page_title or title, link, " ".join([snippet, page_body]), args)
        if name == f"Unknown {args.state} HOA" or GENERIC_NAME_RE.search(name):
            continue
        key = (name.lower(), urlparse(link).netloc.lower())
        if key in seen_lead_keys:
            continue
        seen_lead_keys.add(key)
        lead = Lead(
            name=name,
            state=args.state.upper(),
            city=None,
            county=args.default_county,
            website=None if _is_pdf_url(link) or args.direct_only else link,
            source=f"search-serper-{args.state.lower()}-docpages",
            source_url=link,
        )
        pdf_urls = [link] if _is_pdf_url(link) else []
        leads.append((lead, pdf_urls))
        _jsonl_write(audit, {
            "event": "lead",
            "score": score,
            "lead": asdict(lead),
            "pdf_urls": pdf_urls,
            "query": row.get("_query"),
            "title": title,
            "snippet": snippet,
        })

    summary = {
        "state": args.state.upper(),
        "search_calls": search_calls,
        "raw_results": len(rows),
        "unique_urls": len(by_url),
        "leads": len(leads),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return leads


def write_leads(path: Path, leads: list[tuple[Lead, list[str]]]) -> None:
    with path.open("w") as f:
        for lead, pdf_urls in leads:
            payload = asdict(lead)
            if pdf_urls:
                payload["pre_discovered_pdf_urls"] = pdf_urls
            print(json.dumps(payload, sort_keys=True), file=f)


def _probe_one(lead: Lead, pdf_urls: list[str], args: argparse.Namespace):
    timeout = args.probe_timeout
    if timeout <= 0:
        return probe(lead, bucket_name=args.bucket, max_pdfs=args.max_pdfs_per_lead, pre_discovered_pdf_urls=pdf_urls)

    def _handler(signum, frame):
        raise TimeoutError(f"probe exceeded {timeout}s wall-clock limit")

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(timeout)
    try:
        return probe(lead, bucket_name=args.bucket, max_pdfs=args.max_pdfs_per_lead, pre_discovered_pdf_urls=pdf_urls)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def probe_leads(args: argparse.Namespace, run_dir: Path, leads: list[tuple[Lead, list[str]]]) -> None:
    out = run_dir / "probe_results.jsonl"
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "hoaware"
    for lead, pdf_urls in leads:
        try:
            result = _probe_one(lead, pdf_urls, args)
            _jsonl_write(out, {"lead": asdict(lead), "pdf_urls": pdf_urls, "result": asdict(result)})
            print(json.dumps({"name": lead.name, "banked": result.documents_banked, "skipped": result.documents_skipped}))
        except Exception as exc:
            _jsonl_write(out, {"lead": asdict(lead), "pdf_urls": pdf_urls, "error": str(exc)})
            print(f"FAILED {lead.name}: {exc}", file=sys.stderr)
            if args.fail_fast:
                raise
        time.sleep(args.probe_delay)


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover/probe public HOA document pages for one state")
    parser.add_argument("--bucket", default=os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank"))
    parser.add_argument("--state", required=True, help="Two-letter state code, e.g. TN")
    parser.add_argument("--state-name", required=True, help="Full state name, e.g. Tennessee")
    parser.add_argument("--state-hint", action="append", default=[], help="Additional state/city/county hint; repeatable")
    parser.add_argument("--city", action="append", default=[], help="City hint for state matching/name cleanup; repeatable")
    parser.add_argument("--county", action="append", default=[], help="County hint for state matching; repeatable")
    parser.add_argument("--default-county", default=None, help="County to put on emitted leads")
    parser.add_argument("--run-id", default=_now_id())
    parser.add_argument("--queries-file", required=True)
    parser.add_argument("--max-queries", type=int, default=20)
    parser.add_argument("--results-per-query", type=int, default=10)
    parser.add_argument("--pages-per-query", type=int, default=1)
    parser.add_argument("--max-leads", type=int, default=0,
                        help="Hard cap on leads kept per discover() call. 0 = unlimited (default). "
                             "Setting any positive cap is almost always wrong: Serper's already paid, "
                             "the bank merges by (state, county, slug) so duplicates are no-ops, and "
                             "high-yield counties (Hot Springs Village family in AR, Big Sky in MT) "
                             "easily produce >100 banked HOAs from a single sweep.")
    parser.add_argument("--min-score", type=int, default=7)
    parser.add_argument("--search-delay", type=float, default=0.3)
    parser.add_argument("--probe-delay", type=float, default=1.0)
    parser.add_argument("--fetch-pages", action="store_true")
    parser.add_argument("--include-direct-pdfs", action="store_true")
    parser.add_argument("--direct-only", action="store_true", help="Never crawl search-result pages during probe")
    parser.add_argument("--require-state-hint", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--probe-timeout", type=int, default=180)
    parser.add_argument("--max-pdfs-per-lead", type=int, default=10)
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()

    args.state = args.state.upper()
    run_dir = ROOT / "benchmark" / "results" / f"{args.state.lower()}_serper_docpages_{args.run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    leads = discover(args, run_dir)
    write_leads(run_dir / "leads.jsonl", leads)
    print(f"leads={len(leads)} run_dir={run_dir}")
    if args.probe:
        probe_leads(args, run_dir, leads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
