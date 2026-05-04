#!/usr/bin/env python3
"""Find and optionally probe public Kansas HOA document pages via Serper.

This is intentionally deterministic. The LLM benchmark is useful for strategy
experiments, but once a query family proves productive, code should do the
repeatable search/probe work cheaply.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

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
    r"\b(bylaws?|by-laws?|covenants?|declaration|cc&?rs?|restrictions?|deed restrictions?|"
    r"governing documents?|hoa documents?|association documents?|rules?|regulations?|"
    r"architectural guidelines?)\b",
    re.IGNORECASE,
)
HOA_RE = re.compile(
    r"\b(hoa|homeowners?|home owners?|homes association|owners association|property owners association)\b",
    re.IGNORECASE,
)
KS_RE = re.compile(
    r"\b(kansas|ks|johnson county|sedgwick county|wyandotte county|shawnee county|"
    r"overland park|olathe|shawnee|lenexa|leawood|prairie village|mission|wichita|"
    r"lawrence|topeka|manhattan|kansas city|derby|andover|gardner|de soto)\b",
    re.IGNORECASE,
)
PRIVATE_RE = re.compile(
    r"(townsq|frontsteps|cincsystems|cincweb|appfolio|buildium|enumerateengage|"
    r"caliber\.cloud|/login|/signin|/account|drive\.google\.com|docs\.google\.com)",
    re.IGNORECASE,
)
BLOCKED_HOST_RE = re.compile(
    r"(^|\.)("
    r"facebook|hopb|hoamanagement|steadily|doorloop|nolo|rocketlawyer|avvo|"
    r"hoa-usa|runhoa|maxfieldhoa|fairwaykansas|kslegislature|kscourts|"
    r"sedgwickcounty|jocogov|wycokck|homes|zillow|redfin|realtor|trulia|"
    r"coldwellbanker|estately|remax|nexthomeprofessionals|innovationskc|"
    r"gadwoodgroup|huffgroupkc|wardellholmes|lynchresidential|"
    r"rabbu|whitetailproperties|permitsguide|activerain|lightslocal|"
    r"urbancoolhomes|homefrontflinthills|cali|encodeplus|sec|nepis|"
    r"tharealty|konradlorenz|centrodeservicios|matchhoa|homeadvisor|"
    r"drbrianjonesrealtor|askarealtor|c21community|countyhomecosts|"
    r"resourceks|prospeo|propfusion|best-us-lawyers|home4rent|"
    r"homeownersassociationdirectory|hoa-community|communitypay|zoominfo|"
    r"propublica|justanswer|quora|reddit|instagram|yelp|kansascity|kcur|"
    r"npr|findlaw|caselaw|trellis|studicata|uslegalforms|martinpringle|"
    r"kcrealestatelawyer|realestatepaperpushers|consumer(?:affairs)?|"
    r"civicweb|granicus|ecode360"
    r")\.",
    re.IGNORECASE,
)
OUT_OF_SCOPE_RE = re.compile(
    r"\b(missouri|mo\b|colorado|texas|florida|west virginia|wv\b|martinsburg|"
    r"palm beach|blanco county|clay county,\s*missouri)\b",
    re.IGNORECASE,
)
JUNK_RE = re.compile(
    r"\b(minutes?|agenda|newsletter|budget|financial|rental|apartments?|lease|"
    r"job|court|lawsuit|coupon|pool pass|directory|roster|for sale|sold|"
    r"realtor|real estate|transaction coordinator|earnest money|listing|"
    r"city council|planning commission|case law|tax filing|form 990)\b",
    re.IGNORECASE,
)
COMMUNITY_HOST_RE = re.compile(
    r"(hoa|home|homes|assoc|creek|ridge|villas|estates|lake|lakes|park|"
    r"forest|brook|bend|hill|hills|ranch|village|reserve|meadows|place)",
    re.IGNORECASE,
)
GENERIC_NAME_RE = re.compile(
    r"^(home|hoa|documents?|governing documents?|hoa documents?|association documents?|"
    r"bylaws?|covenants?|restrictions?|declaration|resources?|helpful links)$",
    re.IGNORECASE,
)

COUNTY_CITIES = {
    "johnson": [
        "Overland Park", "Olathe", "Shawnee", "Lenexa", "Leawood", "Prairie Village",
        "Gardner", "De Soto", "Mission", "Merriam", "Roeland Park", "Spring Hill",
    ],
    "sedgwick": ["Wichita", "Derby", "Andover", "Haysville", "Maize", "Park City", "Goddard"],
    "wyandotte": ["Kansas City KS", "Bonner Springs", "Edwardsville"],
    "shawnee": ["Topeka", "Auburn KS"],
    "douglas": ["Lawrence KS", "Eudora KS", "Baldwin City"],
    "riley": ["Manhattan KS"],
    "leavenworth": ["Leavenworth KS", "Lansing KS", "Basehor KS"],
}

DEFAULT_QUERIES = [
    '"HOA Documents" "Kansas" "bylaws"',
    '"HOA Documents" "Overland Park" "bylaws"',
    '"HOA Documents" "Olathe" "bylaws"',
    '"Governing Documents" "Kansas" "HOA"',
    '"Governing Documents" "Kansas" "Homes Association"',
    '"Association Documents" "Kansas" "Homeowners Association"',
    '"Declaration of Restrictions" "Kansas" "Homes Association"',
    '"Deed Restrictions" "Kansas" "Homes Association"',
    '"Covenants" "Kansas" "Homes Association" "Documents"',
    '"Bylaws" "Kansas" "Homes Association" "Documents"',
    'site:*.org "Kansas" "HOA Documents" "Bylaws"',
    'site:*.org "Kansas" "Governing Documents" "HOA"',
    'site:*.com "Kansas" "HOA Documents" "Architectural Guidelines"',
    'site:*.com "Kansas" "Declaration of Restrictions" "HOA"',
    'site:*.org "Overland Park" "governing documents" HOA',
    'site:*.org "Olathe" "governing documents" HOA',
    'site:*.org "Lenexa" "governing documents" HOA',
    'site:*.org "Shawnee" "governing documents" HOA',
    'site:*.org "Wichita" "governing documents" HOA',
]


def _now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _jsonl_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        print(json.dumps(payload, sort_keys=True), file=f)


def _is_pdf_url(url: str) -> bool:
    return url.lower().split("?", 1)[0].split("#", 1)[0].endswith(".pdf")


def _title_case_slug(value: str) -> str:
    value = re.sub(r"[-_]+", " ", value)
    value = re.sub(r"\b(hoa|ks|kc|inc|org|com|net)\b$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    small = {"of", "and", "the", "at"}
    words = []
    for i, word in enumerate(value.split()):
        lw = word.lower()
        words.append(lw if i and lw in small else lw.capitalize())
    return " ".join(words)


def _clean_name(value: str) -> str | None:
    name = re.sub(r"(?i)^\s*\[?pdf\]?\s*", "", value)
    name = re.sub(r"(?i)\ba residential community in\b.*$", "", name)
    name = re.sub(r"(?i)\bwebsite\b", " ", name)
    name = re.sub(r"(?i)\b(home|page|official website|documents?|governing documents?|hoa documents?|"
                  r"association documents?|bylaws?|by-laws?|covenants?|declaration|restrictions?|"
                  r"deed restrictions?|rules?|regulations?|architectural guidelines?|helpful links|resources?)\b", " ", name)
    name = re.sub(r"(?i)\b(?:in|at)\s+(?:overland park|olathe|shawnee|lenexa|leawood|wichita),?\s*(?:kansas|ks)?\b", " ", name)
    name = re.sub(r"(?i),?\s+(?:kansas|ks)\b", " ", name)
    name = re.sub(r"[_/\\|]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .,-:")
    if (
        not name
        or GENERIC_NAME_RE.search(name)
        or re.search(r"(?i)^(hoa|homes association|homeowners association|owners association)(?:\s+in)?$", name)
        or re.search(r"(?i)^(laws?|state laws?|governance|information|info|section|charter|amendments?|board)$", name)
        or len(name) < 4
        or len(name) > 80
    ):
        return None
    if not HOA_RE.search(name):
        name = f"{name} HOA"
    return name[:120]


def infer_name(title: str, url: str, snippet: str) -> str:
    candidates: list[str] = []
    title = re.sub(r"(?i)^\s*\[?pdf\]?\s*", "", title).strip()
    hay = " ".join([title, snippet])
    for pattern in [
        r"([A-Z][A-Za-z0-9&'., -]{2,70}\s+(?:Homes|Homeowners?|Home Owners?|Property Owners?|Owners)\s+Association)",
        r"([A-Z][A-Za-z0-9&'., -]{2,70}\s+HOA)",
    ]:
        for match in re.finditer(pattern, hay):
            candidates.append(match.group(1))
    if title:
        parts = [p.strip() for p in re.split(r"\s+[|–—]\s+|\s+-\s+", title) if p.strip()]
        candidates.extend(parts)
        candidates.extend(reversed(parts))
        candidates.append(title)
    parsed = urlparse(url)
    host = re.sub(r"^www\.", "", parsed.netloc.lower()).split(".", 1)[0]
    if host and not re.search(r"^(portal|login|docs|files|cdn|assets|storage)$", host):
        candidates.append(_title_case_slug(host))
    candidates.append(snippet)
    best: tuple[int, str] | None = None
    for candidate in candidates:
        cleaned = _clean_name(candidate)
        if not cleaned:
            continue
        score = 0
        if HOA_RE.search(cleaned):
            score += 4
        if re.search(r"\b(association|homes|homeowners?|owners)\b", cleaned, re.IGNORECASE):
            score += 3
        if COMMUNITY_HOST_RE.search(cleaned):
            score += 2
        if re.search(r"(?i)^(hoa|homes association|homeowners association|owners association)\b", cleaned):
            score -= 8
        if re.search(r"(?i)\b(laws?|request|section|charter|amendment|board|information|info)\b", cleaned):
            score -= 5
        if best is None or score > best[0]:
            best = (score, cleaned)
    if best and best[0] >= 2:
        return best[1]
    return _clean_name(host) or "Unknown Kansas HOA"


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


def result_score(row: dict) -> int:
    link = str(row.get("link") or "")
    hay = " ".join([str(row.get("title") or ""), link, str(row.get("snippet") or "")])
    host = urlparse(link).netloc.lower()
    score = 0
    if BLOCKED_HOST_RE.search(host):
        return -20
    if DOC_RE.search(hay):
        score += 6
    if HOA_RE.search(hay):
        score += 4
    if KS_RE.search(hay):
        score += 3
    if COMMUNITY_HOST_RE.search(host):
        score += 3
    if _is_pdf_url(link):
        score -= 2
    if PRIVATE_RE.search(link):
        score -= 20
    if OUT_OF_SCOPE_RE.search(hay):
        score -= 12
    if JUNK_RE.search(hay):
        score -= 5
    return score


def fetch_page_hint(url: str) -> tuple[str, str]:
    if _is_pdf_url(url) or PRIVATE_RE.search(url):
        return "", ""
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=PAGE_HINT_TIMEOUT, allow_redirects=True)
        if response.status_code != 200:
            return "", ""
        ctype = (response.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            return "", ""
        soup = BeautifulSoup(response.text[:400_000], "html.parser")
        title = " ".join((soup.title.get_text(" ") if soup.title else "").split())
        h1 = soup.find(["h1", "h2"])
        heading = " ".join((h1.get_text(" ") if h1 else "").split())
        body = " ".join(soup.get_text(" ").split())[:1200]
        return " | ".join([p for p in (title, heading) if p]), body
    except requests.RequestException:
        return "", ""


def county_queries(county: str) -> list[str]:
    key = county.lower().replace(" county", "").strip()
    label = f"{key.title()} County"
    cities = COUNTY_CITIES.get(key, [])
    queries = [
        f'"{label}" Kansas "HOA Documents" bylaws',
        f'"{label}" Kansas "Governing Documents" HOA',
        f'"{label}" Kansas "Homes Association" restrictions',
        f'"{label}" Kansas "Declaration of Restrictions" "Homes Association"',
        f'"{label}" Kansas "deed restrictions" HOA',
    ]
    for city in cities:
        queries.extend([
            f'"{city}" Kansas "HOA Documents" bylaws',
            f'"{city}" Kansas "Governing Documents" HOA',
            f'"{city}" Kansas "Homes Association" "Declaration of Restrictions"',
            f'site:*.org "{city}" "governing documents" HOA',
            f'site:*.com "{city}" "HOA Documents" "bylaws"',
        ])
    return queries


def load_queries(path: str | None, county: str | None) -> list[str]:
    if not path:
        if county:
            return county_queries(county)
        return DEFAULT_QUERIES
    with open(path) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def discover(args: argparse.Namespace, run_dir: Path) -> list[tuple[Lead, list[str]]]:
    audit = run_dir / "audit.jsonl"
    rows: list[dict] = []
    search_calls = 0
    for query in load_queries(args.queries_file, args.county)[: args.max_queries]:
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
        if link not in by_url or result_score(row) > result_score(by_url[link]):
            row["link"] = link
            by_url[link] = row

    ranked = sorted(by_url.values(), key=result_score, reverse=True)
    leads: list[tuple[Lead, list[str]]] = []
    seen_lead_keys: set[tuple[str, str]] = set()
    for row in ranked:
        if len(leads) >= args.max_leads:
            break
        if result_score(row) < args.min_score:
            continue
        link = str(row.get("link") or "")
        host = urlparse(link).netloc.lower()
        if BLOCKED_HOST_RE.search(host):
            continue
        if PRIVATE_RE.search(link):
            continue
        if _is_pdf_url(link) and not args.include_direct_pdfs:
            continue
        title = str(row.get("title") or "")
        snippet = str(row.get("snippet") or "")
        if OUT_OF_SCOPE_RE.search(" ".join([title, snippet, link])):
            continue
        page_title, page_body = fetch_page_hint(link) if args.fetch_pages else ("", "")
        if page_body and OUT_OF_SCOPE_RE.search(page_body[:2000]):
            continue
        name = infer_name(page_title or title, link, " ".join([snippet, page_body]))
        if name == "Unknown Kansas HOA" or GENERIC_NAME_RE.search(name):
            continue
        key = (name.lower(), urlparse(link).netloc.lower())
        if key in seen_lead_keys:
            continue
        seen_lead_keys.add(key)
        lead = Lead(
            name=name,
            state="KS",
            website=None if _is_pdf_url(link) else link,
            source="search-serper-ks-docpages",
            source_url=link,
        )
        pdf_urls = [link] if _is_pdf_url(link) else []
        leads.append((lead, pdf_urls))
        _jsonl_write(audit, {
            "event": "lead",
            "score": result_score(row),
            "lead": asdict(lead),
            "pdf_urls": pdf_urls,
            "query": row.get("_query"),
            "title": title,
            "snippet": snippet,
        })

    summary = {
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


def probe_leads(args: argparse.Namespace, run_dir: Path, leads: list[tuple[Lead, list[str]]]) -> None:
    out = run_dir / "probe_results.jsonl"
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "hoaware"
    for lead, pdf_urls in leads:
        try:
            result = probe(lead, bucket_name=args.bucket, pre_discovered_pdf_urls=pdf_urls)
            _jsonl_write(out, {"lead": asdict(lead), "result": asdict(result)})
            print(json.dumps({"name": lead.name, "banked": result.documents_banked, "skipped": result.documents_skipped}))
        except Exception as exc:
            _jsonl_write(out, {"lead": asdict(lead), "error": str(exc)})
            print(f"FAILED {lead.name}: {exc}", file=sys.stderr)
        time.sleep(args.probe_delay)


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover/probe Kansas HOA public document pages with Serper")
    parser.add_argument("--bucket", default=os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank"))
    parser.add_argument("--run-id", default=_now_id())
    parser.add_argument("--queries-file", default=None)
    parser.add_argument("--county", default=None, help="Focus generated queries on a Kansas county, e.g. Johnson")
    parser.add_argument("--max-queries", type=int, default=20)
    parser.add_argument("--results-per-query", type=int, default=10)
    parser.add_argument("--pages-per-query", type=int, default=1)
    parser.add_argument("--max-leads", type=int, default=50)
    parser.add_argument("--min-score", type=int, default=5)
    parser.add_argument("--search-delay", type=float, default=0.2)
    parser.add_argument("--probe-delay", type=float, default=0.1)
    parser.add_argument("--fetch-pages", action="store_true")
    parser.add_argument("--include-direct-pdfs", action="store_true")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()

    run_dir = ROOT / "benchmark" / "results" / f"ks_serper_docpages_{args.run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    leads = discover(args, run_dir)
    write_leads(run_dir / "leads.jsonl", leads)
    print(f"leads={len(leads)} run_dir={run_dir}")
    if args.probe:
        probe_leads(args, run_dir, leads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
