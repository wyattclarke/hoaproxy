#!/usr/bin/env python3
"""Zero-lead Kansas HOA governing-doc discovery benchmark.

The model's job is search strategy: propose web searches from only a state.
Code executes Serper searches, fetches public pages/PDFs, asks the model to
triage candidate PDFs from small snippets, then banks accepted PDFs.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from hoaware.bank import DocumentInput, bank_hoa  # noqa: E402
from hoaware.cost_tracker import COST_SERPER_PER_QUERY  # noqa: E402
from hoaware.doc_classifier import ALL_CATEGORIES, VALID_CATEGORIES, classify_from_filename, classify_from_text  # noqa: E402


SERPER_ENDPOINT = "https://google.serper.dev/search"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
BANKABLE_CATEGORIES = {"amendment", "articles", "bylaws", "ccr", "resolution", "rules"}
DEFAULT_USER_AGENT = (
    os.environ.get("HOA_DISCOVERY_USER_AGENT")
    or "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
)
PDF_MAGIC = b"%PDF-"
MAX_PDF_BYTES = 25 * 1024 * 1024
REQUEST_TIMEOUT = 25
PDF_TIMEOUT = 60

GOVDOC_RE = re.compile(
    r"\b(cc&?r|c\.c\.&r|covenants?|declaration|bylaws?|by-laws?|articles? of incorporation|"
    r"rules? and regulations?|architectural guidelines?|design guidelines?|amendments?|"
    r"homes association|homeowners association|property owners association)\b",
    re.IGNORECASE,
)
JUNK_RE = re.compile(
    r"\b(apartments?|rental|lease|minutes|agenda|newsletter|coupon|pool pass|"
    r"financial statement|budget|violation|directory|roster|court|lawsuit|irs|990|"
    r"information|resources?|preferred properties|amazon s3|cdn|website files)\b",
    re.IGNORECASE,
)
COMMUNITY_TOKEN_RE = re.compile(
    r"\b(hoa|homeowners?|homes association|property owners|owners association|"
    r"estates?|creek|lakes?|hills?|ridge|woods?|villas?|place|park|trails?|"
    r"crossing|landing|addition|subdivision|farms?|meadows?|point|harbor)\b",
    re.IGNORECASE,
)
GENERIC_NAME_RE = re.compile(
    r"^(homes association(?: of)?(?: kansas city)?|community|properties|developer|declarant|"
    r"association|documents?|governing documents?|homeowners association information|"
    r"preferred properties kansas hoa|amazon s3 hoa|kansas hoa|hoa kansas city|original hoa)$",
    re.IGNORECASE,
)
REJECT_RATIONALE_RE = re.compile(
    r"\b(not\s+(?:a\s+)?kansas|not\s+in\s+kansas|missouri|florida|palm beach county|newsletter|meeting minutes)\b",
    re.IGNORECASE,
)
PRIVATE_RE = re.compile(
    r"(townsq|frontsteps|cincsystems|cincweb|appfolio|buildium|enumerateengage|"
    r"caliber\.cloud|/login|/signin|/account|drive\.google\.com|docs\.google\.com)",
    re.IGNORECASE,
)
KS_HINT_RE = re.compile(
    r"\b(kansas|ks|johnson county|sedgwick county|wyandotte county|shawnee county|"
    r"wichita|overland park|olathe|shawnee|lenexa|lawrence|topeka|manhattan|kansas city)\b",
    re.IGNORECASE,
)


@dataclass
class SearchResult:
    query: str
    title: str
    link: str
    snippet: str


@dataclass
class PdfCandidate:
    source_page: str
    pdf_url: str
    link_text: str
    title: str
    snippet: str
    query: str
    pdf_bytes: bytes
    sha256: str
    filename: str
    page_count: int | None
    text_extractable: bool | None
    snippet_text: str
    deterministic_category: str | None
    deterministic_confidence: float | None


@dataclass
class AcceptedDoc:
    model: str
    hoa_name: str
    city: str | None
    county: str | None
    category: str
    confidence: float
    rationale: str
    candidate: PdfCandidate
    manifest_uri: str | None = None
    bank_error: str | None = None


def _now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _jsonl_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        print(json.dumps(payload, sort_keys=True), file=f)


def _is_pdf_url(url: str) -> bool:
    return url.lower().split("?", 1)[0].split("#", 1)[0].endswith(".pdf")


def _filename_from_url(url: str) -> str:
    name = urlparse(url).path.rsplit("/", 1)[-1] or "document.pdf"
    return name[:180]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _openrouter_client() -> OpenAI:
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("QA_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY or QA_API_KEY is required")
    timeout = float(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "45"))
    return OpenAI(
        api_key=key,
        base_url=os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL),
        timeout=timeout,
        max_retries=0,
    )


def _chat_json(client: OpenAI, model: str, messages: list[dict[str, str]], *, max_tokens: int = 1200) -> tuple[Any, dict]:
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.2,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        extra_body={"include_reasoning": False},
    )
    usage = getattr(resp, "usage", None)
    usage_payload = {}
    if usage:
        usage_payload = {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }
    content = resp.choices[0].message.content or "{}"
    return _parse_json(content), usage_payload


def seed_queries() -> list[str]:
    cities = [
        "Overland Park", "Olathe", "Lenexa", "Shawnee KS", "Wichita", "Lawrence KS",
        "Topeka", "Manhattan KS", "Kansas City KS", "Leawood", "Prairie Village",
        "Derby KS", "Andover KS", "Haysville KS", "Emporia KS", "Hutchinson KS",
        "Newton KS", "Gardner KS", "De Soto KS", "Mission KS", "Roeland Park KS",
        "Merriam KS", "Lansing KS", "Leavenworth KS", "Basehor KS", "Pittsburg KS",
    ]
    counties = [
        "Johnson County", "Sedgwick County", "Wyandotte County", "Shawnee County",
        "Douglas County", "Riley County", "Leavenworth County", "Butler County",
        "Reno County", "Harvey County", "Miami County", "Pottawatomie County",
        "Cowley County", "Saline County", "Crawford County", "Finney County",
    ]
    base = [
        '"Kansas" "declaration of covenants" HOA filetype:pdf',
        '"Kansas" "homes association" bylaws filetype:pdf',
        '"Kansas" "homeowners association" "covenants" filetype:pdf',
        '"KS" "declaration of covenants" "homeowners association" filetype:pdf',
        '"Johnson County" Kansas HOA covenants filetype:pdf',
        '"Sedgwick County" Kansas HOA bylaws filetype:pdf',
        '"Kansas" "architectural guidelines" HOA filetype:pdf',
        '"Kansas" "declaration of restrictions" "homes association" filetype:pdf',
        '"Kansas" "restrictive covenants" "homes association" filetype:pdf',
        '"Kansas" "articles of incorporation" "homeowners association" filetype:pdf',
        '"Kansas" "deed restrictions" "homes association" filetype:pdf',
        'site:ha-kc.org/data/restrictions "Kansas" filetype:pdf',
        'site:homesassociation.org/data/restrictions "Kansas" filetype:pdf',
        'site:eneighbors.com "Homes Association Bylaws" "Kansas"',
        'site:payhoa.com/uploads "Kansas" "bylaws" "HOA" filetype:pdf',
        'site:hoaedge.com/file/document-page "Kansas" "bylaws"',
        'site:ha-kc.org/data/bylaws "Kansas" filetype:pdf',
        'site:ha-kc.org/data/restrictions "Declaration" "Restrictions" filetype:pdf',
        'site:homesassociation.org/data/bylaws "Kansas" filetype:pdf',
        'site:homesassociation.org/data/restrictions "Declaration" filetype:pdf',
        'site:mccurdy.com/files "Homeowners Association" "Kansas" filetype:pdf',
        'site:mccurdy.com/files "Bylaws" "Sedgwick County" filetype:pdf',
        'site:langere.com/wp-content/uploads "Covenants" "Kansas" filetype:pdf',
        'site:wordpress.com "Kansas" "HOA" "bylaws" filetype:pdf',
        'site:wp-content/uploads "Kansas" "Homeowners Association" "Bylaws" filetype:pdf',
        'site:file/document "Kansas" "Homeowners Association" "Bylaws"',
        'site:ks.gov "declaration of covenants" "homeowners association" pdf',
        'site:wycokck.org "declaration of covenants" "homeowners association" pdf',
    ]
    for city in cities:
        base.extend([
            f'"{city}" "HOA" "bylaws" filetype:pdf',
            f'"{city}" "homes association" "covenants" filetype:pdf',
            f'"{city}" "declaration of covenants" filetype:pdf',
        ])
    for county in counties:
        base.extend([
            f'"{county}" Kansas "homes association" "restrictions" filetype:pdf',
            f'"{county}" Kansas HOA "bylaws" filetype:pdf',
            f'"{county}" Kansas "declaration of restrictions" filetype:pdf',
        ])
    return base


def model_queries(client: OpenAI, model: str, *, count: int, audit: Path) -> list[str]:
    if count <= 0:
        return []
    prompt = (
        "You are planning public web searches to find Kansas HOA governing documents suitable for HOAproxy. "
        "Start with no HOA leads. Generate high-yield Google-style queries likely to find public PDFs or "
        "public management-company community document pages in Kansas. Include Kansas-specific wording like "
        "'homes association'. Avoid logged-in portals and broad junk queries. Return JSON: {\"queries\": [..]}."
    )
    try:
        data, usage = _chat_json(
            client,
            model,
            [
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=900,
        )
        _jsonl_write(audit, {"event": "query_generation", "model": model, "usage": usage, "data": data})
        queries = [str(q).strip() for q in data.get("queries", []) if str(q).strip()]
        return queries[:count]
    except Exception as exc:
        _jsonl_write(audit, {"event": "query_generation_failed", "model": model, "error": str(exc)})
        return []


def serper_search(query: str, *, num: int, page: int, audit: Path) -> list[SearchResult]:
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        raise RuntimeError("SERPER_API_KEY is required")
    headers = {"X-API-KEY": key, "Content-Type": "application/json"}
    payload = {"q": query, "num": num, "page": page, "gl": "us", "hl": "en"}
    resp = requests.post(SERPER_ENDPOINT, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    if resp.status_code >= 400:
        raise RuntimeError(f"Serper {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    _jsonl_write(audit, {"event": "serper", "query": query, "page": page, "organic_count": len(data.get("organic", []))})
    out = []
    for row in data.get("organic", []):
        link = str(row.get("link") or "").strip()
        if not link.startswith(("http://", "https://")):
            continue
        out.append(SearchResult(
            query=query,
            title=str(row.get("title") or ""),
            link=link,
            snippet=str(row.get("snippet") or ""),
        ))
    return out


def _result_score(result: SearchResult) -> int:
    hay = " ".join([result.title, result.link, result.snippet])
    score = 0
    if _is_pdf_url(result.link):
        score += 4
    if GOVDOC_RE.search(hay):
        score += 5
    if KS_HINT_RE.search(hay):
        score += 2
    if "filetype:pdf" in result.query.lower():
        score += 1
    if PRIVATE_RE.search(result.link):
        score -= 10
    if JUNK_RE.search(hay):
        score -= 2
    return score


def select_results(results: list[SearchResult], limit: int) -> list[SearchResult]:
    dedup: dict[str, SearchResult] = {}
    for r in results:
        normalized = r.link.split("#", 1)[0]
        if normalized not in dedup or _result_score(r) > _result_score(dedup[normalized]):
            dedup[normalized] = r
    ranked = sorted(dedup.values(), key=_result_score, reverse=True)
    return [r for r in ranked if _result_score(r) > 0][:limit]


def fetch_html(session: requests.Session, url: str) -> str | None:
    if PRIVATE_RE.search(url):
        return None
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code != 200:
            return None
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            return None
        return resp.text[:800_000]
    except requests.RequestException:
        return None


def harvest_pdf_links(html: str, base_url: str, *, result: SearchResult, limit: int = 20) -> list[SearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[SearchResult] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        url = urljoin(base_url, href)
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        text = " ".join((a.get_text(" ") or "").split())
        hay = f"{url} {text}"
        if _is_pdf_url(url) or GOVDOC_RE.search(hay):
            if PRIVATE_RE.search(url):
                continue
            seen.add(url)
            out.append(SearchResult(query=result.query, title=result.title, link=url, snippet=f"{result.snippet} {text}".strip()))
        if len(out) >= limit:
            break
    return out


def download_pdf(session: requests.Session, result: SearchResult) -> tuple[bytes | None, str | None]:
    if PRIVATE_RE.search(result.link):
        return None, "private_or_walled"
    try:
        head = session.head(result.link, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        size = int(head.headers.get("Content-Length") or 0)
        if size > MAX_PDF_BYTES:
            return None, f"too_large_{size}"
    except requests.RequestException:
        pass
    try:
        resp = session.get(result.link, timeout=PDF_TIMEOUT, stream=True, allow_redirects=True)
        if resp.status_code != 200:
            return None, f"status_{resp.status_code}"
        buf = bytearray()
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > MAX_PDF_BYTES:
                resp.close()
                return None, "too_large_streamed"
        resp.close()
        data = bytes(buf)
        if not data.startswith(PDF_MAGIC):
            return None, "not_pdf"
        return data, None
    except requests.RequestException as exc:
        return None, f"request_{type(exc).__name__}"


def inspect_pdf(pdf_bytes: bytes, filename: str, hoa_hint: str = "") -> tuple[int | None, bool | None, str, str | None, float | None]:
    page_count: int | None = None
    text_extractable: bool | None = None
    snippet = ""
    category: str | None = None
    confidence: float | None = None
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
        parts = []
        for i in range(min(5, page_count)):
            try:
                parts.append(reader.pages[i].extract_text() or "")
            except Exception:
                pass
        snippet = "\n".join(parts)
        text_extractable = len(snippet.strip()) >= 50
        clf = classify_from_text(snippet, hoa_hint)
        if clf:
            category = str(clf["category"])
            confidence = float(clf["confidence"])
    except Exception:
        pass
    if not category:
        clf = classify_from_filename(filename)
        if clf:
            category = str(clf["category"])
            confidence = float(clf["confidence"])
    return page_count, text_extractable, snippet[:3000], category, confidence


def _clean_hoa_name(name: str) -> str:
    name = re.sub(r"(?i)^\s*\[?pdf\]?\s*", "", name)
    name = name.split("|", 1)[0]
    name = re.sub(r"(?i)^\s*(by and between|between|for|the)\s+", "", name)
    name = re.sub(r"(?i)\b(?:incorporated|inc\.?|llc|l\.l\.c\.)\b", "", name)
    name = re.sub(
        r"(?i)\s+to\s+(andover|wichita|olathe|overland park|shawnee|lenexa|lawrence|topeka|manhattan|kansas city)\b",
        "",
        name,
    )
    name = re.sub(r"(?i),?\s+(?:kansas|ks)\b(?=\s+HOA|\s*$)", "", name)
    name = re.sub(r"(?i)\b(bylaws?|declaration|covenants?|conditions|restrictions|cc&rs?|rules?|regulations?|amended|restated|unified|of|the)\b", " ", name)
    name = re.sub(r"(?i)\bhome\s*owner'?s?\s+association\b", "HOA", name)
    name = re.sub(r"(?i)\bhomes\s+association\b", "HOA", name)
    name = re.sub(r"(?i)\bhomeowners?\s+association\b", "HOA", name)
    name = re.sub(r"[_/\\-]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .,-")
    if name and not re.search(r"(?i)\b(hoa|homeowners?|homes association|owners association|association)\b", name):
        name = f"{name} HOA"
    return name[:120]


def _valid_inferred_name(name: str, *, allow_host: bool = False) -> bool:
    if len(name) < 5:
        return False
    if GENERIC_NAME_RE.search(name):
        return False
    if JUNK_RE.search(name):
        return False
    if re.search(r"\b(s3|cdn|storage|document|file|upload|download|pdf)\b", name, re.IGNORECASE):
        return False
    if allow_host:
        return bool(COMMUNITY_TOKEN_RE.search(name))
    return bool(COMMUNITY_TOKEN_RE.search(name) or re.search(r"\bHOA\b|\bAssociation\b", name, re.IGNORECASE))


def _name_from_slug(slug: str) -> str | None:
    slug = re.sub(r"\.pdf$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"%20|\+", " ", slug)
    slug = re.sub(r"[_-]+", " ", slug)
    slug = re.sub(r"\s+", " ", slug).strip()
    if not COMMUNITY_TOKEN_RE.search(slug):
        return None
    slug = re.split(r"(?i)\b(declaration|covenants?|restrictions?|bylaws?|rules?|amended|restated)\b", slug, 1)[0].strip(" .,-")
    if not slug:
        return None
    cleaned = _clean_hoa_name(slug)
    return cleaned if _valid_inferred_name(cleaned) else None


def infer_hoa_name(candidate: PdfCandidate) -> str | None:
    title = re.sub(r"(?i)^\s*\[pdf\]\s*", "", candidate.title).strip()
    if " - " in title:
        # Search result titles are often "[PDF] Bylaws - Arlington Estates HOA".
        segment = title.rsplit(" - ", 1)[-1].split("|", 1)[0].strip()
        cleaned = _clean_hoa_name(segment)
        if _valid_inferred_name(cleaned):
            return cleaned

    path_bits = [bit for bit in urlparse(candidate.pdf_url).path.split("/") if bit]
    for bit in reversed(path_bits[-4:]):
        name = _name_from_slug(bit)
        if name:
            return name

    host = urlparse(candidate.pdf_url).netloc.lower()
    host = re.sub(r"^www\.", "", host)
    host_name = host.split(".", 1)[0]
    if (
        host_name
        and not re.search(r"(?i)\b(cdn|blob|storage|digitaloceanspaces|wp-content|mccmeetingspublic|cobaltreks|langere)\b", host)
        and COMMUNITY_TOKEN_RE.search(host_name)
    ):
        host_name = re.sub(r"(hoa|ks|kc|mo|inc)$", "", host_name)
        if len(host_name) >= 5:
            cleaned = _clean_hoa_name(host_name)
            if _valid_inferred_name(cleaned, allow_host=True):
                return cleaned

    hay = "\n".join([
        candidate.title,
        candidate.filename.replace(".pdf", " "),
        candidate.snippet,
        candidate.snippet_text[:1600],
        candidate.pdf_url,
    ])
    patterns = [
        r"([A-Z][A-Za-z0-9&'., -]{2,90}\s+(?:Homeowners|Home Owners|Homes|Property Owners|Owners)\s+Association(?:,\s*Inc\.?)?)",
        r"([A-Z][A-Za-z0-9&'., -]{2,90}\s+HOA)",
        r"(?:Bylaws? of)\s+(?:the\s+)?([A-Z][A-Za-z0-9&'., -]{3,90})",
        r"(?:Declaration .*? for all phases.*? of|Declaration .*? of)\s+(?:the\s+)?([A-Z][A-Za-z0-9&'., -]{3,80}?(?:Addition|Subdivision|Estates|Homes Association|Homeowners Association|HOA))",
        r"/([A-Za-z0-9-]*(?:estates|creek|lakes|place|park|ridge|woods|villas|addition|subdivision|hoa)[A-Za-z0-9-]*)/",
    ]
    for pattern in patterns:
        m = re.search(pattern, hay, re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        raw = m.group(1).replace("-", " ")
        cleaned = _clean_hoa_name(raw)
        if _valid_inferred_name(cleaned):
            return cleaned
    return None


def infer_hoa_name_from_reason(reason: str) -> str | None:
    patterns = [
        r"\bfor\s+([A-Z][A-Za-z0-9&'., -]{3,90}?(?:HOA|Homeowners Association|Homes Association|Owners Association|Estates|Addition|Ridge|Woods|Creek|Lake|Lakes|Hills))\b",
        r"\b([A-Z][A-Za-z0-9&'., -]{3,90}?(?:HOA|Homeowners Association|Homes Association|Owners Association))\b",
        r"\b([A-Z][A-Za-z0-9&'., -]{3,90}?(?:Estates|Addition|Ridge|Woods|Creek|Lake|Lakes|Hills)),?\s+(?:a|an|in|referencing)\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, reason)
        if not m:
            continue
        cleaned = _clean_hoa_name(m.group(1))
        if _valid_inferred_name(cleaned):
            return cleaned
    return None


EVIDENCE_STOPWORDS = {
    "hoa", "home", "owner", "owners", "homeowner", "homeowners", "homes", "association",
    "assn", "inc", "llc", "addition", "the", "of", "and", "to", "at", "in", "ks", "kansas",
}


def _evidence_tokens(name: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return [token for token in tokens if len(token) > 2 and token not in EVIDENCE_STOPWORDS]


def _name_has_candidate_evidence(name: str, candidate: PdfCandidate) -> bool:
    tokens = _evidence_tokens(name)
    if not tokens:
        return False
    parsed = urlparse(candidate.pdf_url)
    host_blob = parsed.netloc.lower() if re.search(r"hoa|home|association", parsed.netloc, re.IGNORECASE) else ""
    evidence = " ".join([
        candidate.title,
        candidate.snippet,
        candidate.filename,
        candidate.snippet_text[:3000],
        parsed.path,
        host_blob,
    ]).lower()
    evidence = re.sub(r"[^a-z0-9]+", " ", evidence)
    compact = evidence.replace(" ", "")
    hits = sum(1 for token in tokens if token in evidence or token in compact)
    required = 1 if len(tokens) == 1 else min(2, len(tokens))
    return hits >= required


def choose_hoa_name(raw_model_name: str, reason: str, candidate: PdfCandidate) -> str | None:
    """Pick a bankable HOA name from model output, rationale, or local clues."""
    sources = [
        (raw_model_name, True),
        (infer_hoa_name_from_reason(reason) or "", True),
        (infer_hoa_name(candidate) or "", False),
    ]
    for raw, require_evidence in sources:
        raw = str(raw or "").strip()
        if not raw:
            continue
        cleaned = _clean_hoa_name(raw)
        if _valid_inferred_name(cleaned) and (not require_evidence or _name_has_candidate_evidence(cleaned, candidate)):
            return cleaned
    return None


def collect_candidates(
    results: list[SearchResult],
    *,
    max_pages: int,
    max_pdfs: int,
    audit: Path,
) -> list[PdfCandidate]:
    session = _session()
    candidates: list[PdfCandidate] = []
    seen_pdf: set[str] = set()
    pages_seen = 0
    queue: list[SearchResult] = []
    for r in select_results(results, max_pages):
        if _is_pdf_url(r.link):
            queue.append(r)
            continue
        html = fetch_html(session, r.link)
        pages_seen += 1
        if html:
            queue.extend(harvest_pdf_links(html, r.link, result=r))
        if pages_seen >= max_pages:
            break

    direct_pdfs = [r for r in select_results(results, max_pages * 2) if _is_pdf_url(r.link)]
    queue = direct_pdfs + queue

    for r in queue:
        if len(candidates) >= max_pdfs:
            break
        pdf_url = r.link.split("#", 1)[0]
        if pdf_url in seen_pdf:
            continue
        seen_pdf.add(pdf_url)
        pdf_bytes, skip = download_pdf(session, r)
        if skip or not pdf_bytes:
            _jsonl_write(audit, {"event": "pdf_skipped", "url": pdf_url, "reason": skip})
            continue
        sha = hashlib.sha256(pdf_bytes).hexdigest()
        filename = _filename_from_url(pdf_url)
        page_count, text_extractable, text, det_cat, det_conf = inspect_pdf(pdf_bytes, filename)
        candidates.append(PdfCandidate(
            source_page=r.link if not _is_pdf_url(r.link) else "",
            pdf_url=pdf_url,
            link_text="",
            title=r.title,
            snippet=r.snippet,
            query=r.query,
            pdf_bytes=pdf_bytes,
            sha256=sha,
            filename=filename,
            page_count=page_count,
            text_extractable=text_extractable,
            snippet_text=text,
            deterministic_category=det_cat,
            deterministic_confidence=det_conf,
        ))
        _jsonl_write(audit, {
            "event": "pdf_candidate",
            "url": pdf_url,
            "sha256": sha,
            "page_count": page_count,
            "deterministic_category": det_cat,
        })
    return candidates


def _candidate_prompt_payload(candidates: list[PdfCandidate]) -> list[dict[str, Any]]:
    payload = []
    for i, c in enumerate(candidates):
        payload.append({
            "index": i,
            "url": c.pdf_url,
            "filename": c.filename,
            "title": c.title,
            "search_snippet": c.snippet,
            "query": c.query,
            "page_count": c.page_count,
            "deterministic_category": c.deterministic_category,
            "text": c.snippet_text[:1600],
        })
    return payload


def model_triage(
    client: OpenAI,
    model: str,
    candidates: list[PdfCandidate],
    *,
    audit: Path,
    batch_size: int = 4,
    on_batch_accepted: Callable[[list[AcceptedDoc]], None] | None = None,
) -> tuple[list[AcceptedDoc], list[dict[str, Any]]]:
    accepted: list[AcceptedDoc] = []
    rejected: list[dict[str, Any]] = []
    if not candidates:
        return accepted, rejected
    categories = sorted(ALL_CATEGORIES)
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start:start + batch_size]
        batch_accepted: list[AcceptedDoc] = []
        prompt = {
            "task": "Decide which public PDFs are Kansas HOA governing documents suitable for HOAproxy.",
            "valid_categories": sorted(VALID_CATEGORIES),
            "all_categories": categories,
            "rules": [
                "Keep CC&Rs/declarations/covenants/bylaws/articles/rules/architectural guidelines/amendments/resolutions.",
                "Reject meeting minutes, newsletters, budgets, directories, owner lists, filled ballots, violations, court/tax/government-only docs, and unrelated PDFs.",
                "Require a plausible HOA or homes association name.",
                "For every kept item, include hoa_name, category, confidence, and rationale.",
                "Return only JSON with key decisions.",
            ],
            "candidates": _candidate_prompt_payload(batch),
        }
        try:
            data, usage = _chat_json(
                client,
                model,
                [
                    {"role": "system", "content": "You are a strict public HOA governing-document triage agent. Return valid JSON only."},
                    {"role": "user", "content": json.dumps(prompt)},
                ],
                max_tokens=1600,
            )
            _jsonl_write(audit, {"event": "triage", "model": model, "usage": usage, "data": data})
        except Exception as exc:
            _jsonl_write(audit, {"event": "triage_failed", "model": model, "error": str(exc)})
            continue
        if isinstance(data, list):
            decisions = data
        elif isinstance(data, dict):
            decisions = data.get("decisions", [])
        else:
            decisions = []
        for d in decisions:
            if not isinstance(d, dict):
                continue
            idx = int(d.get("index", -1))
            if idx < 0 or idx >= len(batch):
                continue
            candidate = batch[idx]
            decision = str(d.get("decision") or "").strip().lower()
            keep_value = d.get("keep")
            keep = bool(keep_value) or decision in {"keep", "accept", "accepted", "yes", "true"}
            category = str(d.get("category") or "").strip().lower()
            if not keep and not decision and category in BANKABLE_CATEGORIES:
                keep = True
            confidence = _safe_float(d.get("confidence"), 0.0)
            rationale = str(d.get("rationale") or d.get("reason") or "").strip()[:300]
            hoa_name = choose_hoa_name(str(d.get("hoa_name") or ""), rationale, candidate) or ""
            if keep and REJECT_RATIONALE_RE.search(rationale):
                keep = False
            if confidence <= 0.0 and keep:
                confidence = candidate.deterministic_confidence or 0.65
            if keep and category in BANKABLE_CATEGORIES and hoa_name and confidence >= 0.55:
                doc = AcceptedDoc(
                    model=model,
                    hoa_name=hoa_name,
                    city=str(d.get("city") or "").strip() or None,
                    county=str(d.get("county") or "").strip() or None,
                    category=category,
                    confidence=confidence,
                    rationale=rationale,
                    candidate=candidate,
                )
                accepted.append(doc)
                batch_accepted.append(doc)
            else:
                rejected.append({
                    "url": candidate.pdf_url,
                    "category": category,
                    "confidence": confidence,
                    "reason": rationale or "model_rejected",
                })
        if batch_accepted and on_batch_accepted:
            on_batch_accepted(batch_accepted)
    return accepted, rejected


def bank_docs(docs: list[AcceptedDoc], *, bucket: str, model_slug: str, audit: Path) -> None:
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "hoaware"
    seen_sha: set[str] = set()
    for doc in docs:
        if doc.candidate.sha256 in seen_sha:
            continue
        seen_sha.add(doc.candidate.sha256)
        metadata_source = {
            "source": f"serper-openrouter-{model_slug}",
            "source_url": doc.candidate.pdf_url,
            "fields_provided": ["name", "state", "source_url"],
            "query": doc.candidate.query,
            "model": doc.model,
            "confidence": doc.confidence,
            "rationale": doc.rationale,
        }
        try:
            uri = bank_hoa(
                name=doc.hoa_name,
                address={k: v for k, v in {"state": "KS", "city": doc.city, "county": doc.county}.items() if v},
                metadata_source=metadata_source,
                documents=[DocumentInput(
                    pdf_bytes=doc.candidate.pdf_bytes,
                    source_url=doc.candidate.pdf_url,
                    filename=doc.candidate.filename,
                    category_hint=doc.category,
                    text_extractable_hint=doc.candidate.text_extractable,
                )],
                bucket_name=bucket,
            )
            doc.manifest_uri = uri
            _jsonl_write(audit, {"event": "banked", "hoa": doc.hoa_name, "url": doc.candidate.pdf_url, "manifest_uri": uri})
        except Exception as exc:
            doc.bank_error = str(exc)
            _jsonl_write(audit, {"event": "bank_failed", "hoa": doc.hoa_name, "url": doc.candidate.pdf_url, "error": str(exc)})


def run_model(args: argparse.Namespace, model: str, run_dir: Path) -> dict[str, Any]:
    model_slug = re.sub(r"[^a-zA-Z0-9_.-]+", "_", model)
    audit = run_dir / f"{model_slug}.jsonl"
    client = _openrouter_client()
    started = time.time()
    generated = model_queries(client, model, count=max(0, args.model_queries), audit=audit)
    queries = []
    seen_q = set()
    for q in generated + seed_queries():
        if q not in seen_q:
            queries.append(q)
            seen_q.add(q)
        if len(queries) >= args.max_queries:
            break

    all_results: list[SearchResult] = []
    search_calls = 0
    for q in queries:
        for page in range(1, args.pages_per_query + 1):
            try:
                all_results.extend(serper_search(q, num=args.results_per_query, page=page, audit=audit))
                search_calls += 1
            except Exception as exc:
                _jsonl_write(audit, {"event": "serper_failed", "query": q, "page": page, "error": str(exc)})
            time.sleep(args.search_delay)

    selected = select_results(all_results, args.max_results)
    _jsonl_write(audit, {"event": "selected_results", "count": len(selected), "links": [asdict(r) for r in selected[: args.max_results]]})
    candidates = collect_candidates(selected, max_pages=args.max_pages, max_pdfs=args.max_pdfs, audit=audit)
    accepted, rejected = model_triage(
        client,
        model,
        candidates,
        audit=audit,
        batch_size=args.triage_batch_size,
        on_batch_accepted=lambda docs: bank_docs(docs, bucket=args.bucket, model_slug=model_slug, audit=audit),
    )
    banked = [d for d in accepted if d.manifest_uri]
    summary = {
        "model": model,
        "queries": len(queries),
        "search_calls": search_calls,
        "serper_est_cost_usd": round(search_calls * COST_SERPER_PER_QUERY, 6),
        "search_results": len(all_results),
        "selected_results": len(selected),
        "pdf_candidates": len(candidates),
        "accepted": len(accepted),
        "banked": len(banked),
        "bank_errors": len([d for d in accepted if d.bank_error]),
        "rejected": len(rejected),
        "duration_sec": round(time.time() - started, 2),
        "banked_docs": [
            {
                "hoa": d.hoa_name,
                "category": d.category,
                "confidence": d.confidence,
                "url": d.candidate.pdf_url,
                "sha256": d.candidate.sha256,
                "manifest_uri": d.manifest_uri,
            }
            for d in banked
        ],
    }
    (run_dir / f"{model_slug}_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Run KS zero-lead OpenRouter/Serper HOA discovery")
    ap.add_argument("--models", nargs="+", default=["qwen/qwen3.5-flash-02-23"], help="OpenRouter model ids")
    ap.add_argument("--bucket", default=os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank"))
    ap.add_argument("--run-id", default=_now_id())
    ap.add_argument("--max-queries", type=int, default=20)
    ap.add_argument("--model-queries", type=int, default=10)
    ap.add_argument("--results-per-query", type=int, default=10)
    ap.add_argument("--pages-per-query", type=int, default=1)
    ap.add_argument("--max-results", type=int, default=80)
    ap.add_argument("--max-pages", type=int, default=40)
    ap.add_argument("--max-pdfs", type=int, default=30)
    ap.add_argument("--triage-batch-size", type=int, default=4)
    ap.add_argument("--search-delay", type=float, default=0.25)
    args = ap.parse_args()

    run_dir = ROOT / "benchmark" / "results" / f"ks_openrouter_{args.run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for model in args.models:
        print(f"== {model} ==")
        summary = run_model(args, model, run_dir)
        summaries.append(summary)
        print(json.dumps(summary, indent=2))

    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summaries, indent=2, sort_keys=True))
    tsv = run_dir / "summary.tsv"
    with tsv.open("w") as f:
        print("model\tqueries\tpdf_candidates\taccepted\tbanked\tserper_est_cost_usd\tduration_sec", file=f)
        for s in summaries:
            print(
                f"{s['model']}\t{s['queries']}\t{s['pdf_candidates']}\t{s['accepted']}\t"
                f"{s['banked']}\t{s['serper_est_cost_usd']}\t{s['duration_sec']}",
                file=f,
            )
    print(f"Results: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
