#!/usr/bin/env python3
"""For each CT SoS-derived lead, run a targeted Serper search for governing
documents and add `pre_discovered_pdf_urls` and (optionally) `website` to the
lead. Output a new JSONL ready for `probe_enriched_leads.py`.

The exact-name match dramatically reduces false positives compared to broad
state-keyword Serper queries: the entity name is unique enough that hits
typically belong to the right HOA.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)

SERPER_ENDPOINT = "https://google.serper.dev/search"

PRIVATE_HOST_RE = re.compile(
    r"(townsq|frontsteps|cincsystems|cincweb|appfolio|buildium|enumerateengage|"
    r"caliber\.cloud|drive\.google\.com|docs\.google\.com|"
    r"facebook\.com|instagram\.com|linkedin\.com|twitter\.com|x\.com|"
    r"yelp\.com|zillow\.com|redfin\.com|realtor\.com|trulia\.com)",
    re.IGNORECASE,
)
BLOCKED_HOST_RE = re.compile(
    r"(^|\.)(opencorporates|bizapedia|dnb|zoominfo|bbb|"
    r"sec\.gov|irs\.gov|congress\.gov|nytimes|wsj|bloomberg|"
    r"scribd|issuu|yumpu|dokumen|pdfcoffee|fliphtml5|"
    r"q4cdn|10k|sec-edgar|"
    r"liladelman|williamraveis|coldwellbanker|sothebysrealty|"
    r"compass\.com|kwri|kw\.com|exitrealty|realtytrust|"
    r"dlcp\.dc\.gov|ssrn|jstor|justia|caselaw|"
    r"cga\.ct\.gov|search\.cga\.state\.ct\.us)\.",
    re.IGNORECASE,
)
# Specific URL patterns that look like an HOA-relevant doc but actually aren't.
# CT general assembly publishes bills under cga.ct.gov/{year}/...
JUNK_URL_RE = re.compile(
    r"(/BillText|/Senate|/House|/HouseText|/SenateText|"
    r"/SEC-Edgar|/10-?K|/10-?Q|/Annual_?Report|"
    r"/asp/cgabillstatus|/lcoasp|/PA[0-9]+\.pdf|"
    r"caine\.org/userfiles/uploads/.*Resource_Directory)",
    re.IGNORECASE,
)
# CT does not appear to expose a clean SoS document drive; placeholder regex
# (kept so the scoring path matches RI's, but it'll just never fire).
SOS_PDF_RE = re.compile(r"business\.ct\.gov/.+\.pdf", re.IGNORECASE)
# Generic name tokens that don't specifically identify a single HOA. Used to
# require a non-generic-token overlap before scoring a candidate.
GENERIC_NAME_TOKENS = {
    "the", "and", "of", "association", "associations", "condominium",
    "condominiums", "condo", "condos", "homeowners", "homeowner",
    "owners", "owner", "property", "properties",
    "inc", "incorporated", "corporation", "corp", "llc", "limited",
    "connecticut", "ct", "conn",
    "apartment", "apartments", "homes", "house", "houses",
    "ave", "avenue", "street", "st", "rd", "road", "lane", "ln", "ct",
    "blvd", "boulevard", "drive", "dr", "place", "pl",
}


def name_for_query(name: str) -> str:
    stripped = re.sub(r",?\s+(LLC|Inc\.?|Incorporated|Corporation|Corp\.?)\s*$", "", name, flags=re.IGNORECASE)
    return stripped.strip()


def normalize_tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def serper_search(query: str, *, num: int) -> list[dict]:
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        raise RuntimeError("SERPER_API_KEY missing from settings.env")
    headers = {"X-API-KEY": key, "Content-Type": "application/json"}
    payload = {"q": query, "num": num, "gl": "us", "hl": "en"}
    response = requests.post(SERPER_ENDPOINT, headers=headers, json=payload, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"Serper {response.status_code}: {response.text[:300]}")
    return list(response.json().get("organic", []))


GOVDOC_RE = re.compile(
    r"\b(declaration|covenants?|cc&?rs?|bylaws?|by-laws?|articles?|charter|"
    r"restrictions?|rules?|regulations?|amendments?|policy|policies|"
    r"resolutions?|master\s+deed|condominium\s+act|governing\s+documents?)\b",
    re.IGNORECASE,
)


def is_pdf(url: str) -> bool:
    clean = url.lower().split("?", 1)[0].split("#", 1)[0]
    return clean.endswith(".pdf") or "format=pdf" in url.lower()


def candidate_score(specific_tokens: set[str], generic_tokens: set[str], row: dict) -> int:
    link = str(row.get("link") or "")
    title = str(row.get("title") or "")
    snippet = str(row.get("snippet") or "")
    host = urlparse(link).netloc.lower()
    if BLOCKED_HOST_RE.search(host):
        return -50
    if JUNK_URL_RE.search(link):
        return -50
    score = 0
    blob = " ".join([title, snippet, link])
    blob_tokens = normalize_tokens(blob)
    specific_overlap = len(specific_tokens & blob_tokens)
    generic_overlap = len(generic_tokens & blob_tokens)
    if specific_tokens and specific_overlap == 0:
        return -50
    score += min(specific_overlap, 6) * 4
    score += min(generic_overlap, 4) * 1
    if GOVDOC_RE.search(blob):
        score += 5
    if "connecticut" in blob.lower() or re.search(r"\bct\b", blob, re.IGNORECASE):
        score += 2
    if is_pdf(link):
        score += 3
    if SOS_PDF_RE.search(link):
        score += 4
    if PRIVATE_HOST_RE.search(host):
        score -= 5
    return score


def enrich_lead(lead: dict, *, results_per_query: int, polite: float, min_score: int) -> dict:
    name = lead.get("name") or ""
    quoted = name_for_query(name)
    all_tokens = normalize_tokens(quoted)
    specific_tokens = {t for t in all_tokens if t and t not in GENERIC_NAME_TOKENS and len(t) > 1}
    generic_tokens = all_tokens - specific_tokens
    queries = [
        f'"{quoted}" Connecticut filetype:pdf',
        f'"{quoted}" "Connecticut" declaration OR bylaws OR covenants',
    ]
    pdf_urls: list[str] = []
    candidate_websites: list[tuple[int, str]] = []
    audit: list[dict] = []
    for q in queries:
        try:
            rows = serper_search(q, num=results_per_query)
        except Exception as exc:
            audit.append({"query": q, "error": str(exc)})
            continue
        for row in rows:
            link = str(row.get("link") or "").split("#", 1)[0].strip()
            if not link.startswith(("http://", "https://")):
                continue
            score = candidate_score(specific_tokens, generic_tokens, row)
            audit.append({
                "query": q, "score": score, "link": link,
                "title": (row.get("title") or "")[:120],
            })
            if score < min_score:
                continue
            if is_pdf(link):
                if link not in pdf_urls:
                    pdf_urls.append(link)
            else:
                if not PRIVATE_HOST_RE.search(urlparse(link).netloc.lower()):
                    candidate_websites.append((score, link))
        time.sleep(polite)
    out = dict(lead)
    if pdf_urls:
        out["pre_discovered_pdf_urls"] = pdf_urls[:8]
    if not out.get("website") and candidate_websites:
        candidate_websites.sort(reverse=True)
        out["website"] = candidate_websites[0][1]
    out["serper_audit"] = audit[:30]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(ROOT / "state_scrapers/ct/leads/ct_sos_associations.jsonl"))
    parser.add_argument("--output", default=str(ROOT / "state_scrapers/ct/leads/ct_sos_associations_enriched.jsonl"))
    parser.add_argument("--results-per-query", type=int, default=10)
    parser.add_argument("--polite-delay", type=float, default=0.4)
    parser.add_argument("--min-score", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0,
                        help="Process at most N leads (0 = all)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip leads already present in --output")
    args = parser.parse_args()

    inp = Path(args.input)
    outp = Path(args.output)
    outp.parent.mkdir(parents=True, exist_ok=True)

    done_keys: set[str] = set()
    if args.resume and outp.exists():
        for line in outp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                done_keys.add(rec.get("sos_id") or rec.get("name", ""))
            except Exception:
                pass

    leads = [json.loads(l) for l in inp.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit > 0:
        leads = leads[: args.limit]

    out_mode = "a" if args.resume and outp.exists() else "w"
    with_pdfs = 0
    with_website = 0
    processed = 0
    skipped = 0
    with outp.open(out_mode, encoding="utf-8") as f:
        for lead in leads:
            key = lead.get("sos_id") or lead.get("name", "")
            if key and key in done_keys:
                skipped += 1
                continue
            try:
                enriched = enrich_lead(
                    lead,
                    results_per_query=args.results_per_query,
                    polite=args.polite_delay,
                    min_score=args.min_score,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"FAIL {lead.get('name')!r}: {exc}", file=sys.stderr)
                enriched = dict(lead)
                enriched["serper_error"] = str(exc)
            f.write(json.dumps(enriched, sort_keys=True) + "\n")
            f.flush()
            processed += 1
            if enriched.get("pre_discovered_pdf_urls"):
                with_pdfs += 1
            if enriched.get("website"):
                with_website += 1
            if processed % 25 == 0:
                print(f"[ct-enrich] processed={processed} pdfs={with_pdfs} websites={with_website}", file=sys.stderr)
    summary = {
        "output": str(outp),
        "input_count": len(leads),
        "processed": processed,
        "skipped_resume": skipped,
        "with_pdfs": with_pdfs,
        "with_website": with_website,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
