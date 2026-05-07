#!/usr/bin/env python3
"""Harvest governing-doc PDFs from RI condo management company websites.

Two strategies per firm:
  1. site:<firm-host> filetype:pdf   — Serper site-scoped index search.
  2. Crawl the firm's homepage + 1 level of doc-related subpages, harvest
     PDFs by name/URL patterns. Reuses hoaware.discovery.probe's link
     harvesting heuristics.

Each found PDF is matched to one of the cluster's HOA names by specific
(non-generic) name-token overlap. Output: leads JSONL feeding probe-batch.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / "settings.env", override=False)

USER_AGENT = "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
SERPER = "https://google.serper.dev/search"

# Hand-curated cluster-address → firm map. Each is a recognized RI/SE-MA
# condo property-management firm with a website worth crawling. Keys are
# the normalized cluster addresses produced by find_mgmt_companies.py's
# clustering rule.
FIRMS = {
    "181 KNIGHT STREET WARWICK, RI 02886": {
        "firm": "The Hennessy Group", "host": "www.thehennessygrp.com"},
    "222 BROADWAY PROVIDENCE, RI 02903": {
        "firm": "Divine Investments", "host": "www.divineinvestments.com"},
    "498 MAIN STREET WARREN, RI 02885": {
        "firm": "Apex Management Group", "host": "apexmanagementgroup.net"},
    "250B CENTERVILLE ROAD WARWICK, RI 02886": {
        "firm": "Summit Management Corp", "host": "summit-mgmtri.com"},
    "786 OAKLAWN AVENUE CRANSTON, RI 02920": {
        "firm": "C.R.S. Management", "host": "www.crsmgmt.com"},
    "75 LAMBERT LIND HIGHWAY WARWICK, RI 02886": {
        "firm": "Picerne Real Estate Group", "host": "www.picerne.com"},
    "615 JEFFERSON BLVD  WARWICK, RI 02886": {
        # Multiple firms at this office park; crawl the building's flagship.
        "firm": "Brock Associates / Heffner & Associates", "host": "brockassociates.com"},
}

GENERIC_TOKENS = {
    "the","and","of","association","associations","condominium","condominiums",
    "condo","condos","homeowners","homeowner","owners","owner","property",
    "properties","inc","incorporated","corporation","corp","llc","limited",
    "rhode","island","ri","village","estates","homes","home","house",
    "apartment","apartments","place","court","park","phase","at","ii","iii","iv",
    "lp","co","plc","trust","group","management","mgmt",
}

DOCS_PAGE_RE = re.compile(
    r"\b(documents?|governing|library|downloads?|forms?|resources?|files?|"
    r"publications?|covenants?|restrictions?|bylaws?|declaration|hoa|condo|"
    r"associations?|portal)\b",
    re.IGNORECASE,
)


def normalize_tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def serper_site_search(host: str, query: str, *, num: int = 10) -> list[dict]:
    key = os.environ["SERPER_API_KEY"]
    r = requests.post(
        SERPER,
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        json={"q": f"site:{host} {query}", "num": num, "gl": "us", "hl": "en"},
        timeout=30,
    )
    if r.status_code >= 400:
        return []
    return list(r.json().get("organic", []))


def harvest_via_serper(host: str) -> list[tuple[str, str]]:
    out = []
    seen = set()
    for q in ["filetype:pdf", "declaration filetype:pdf", "bylaws filetype:pdf",
              "covenants filetype:pdf", "condominium filetype:pdf", "HOA documents"]:
        rows = serper_site_search(host, q, num=10)
        for r in rows:
            link = (r.get("link") or "").strip()
            if not link.lower().split("?",1)[0].endswith(".pdf"): continue
            if link in seen: continue
            seen.add(link)
            out.append((link, r.get("title") or ""))
        time.sleep(0.4)
    return out


def harvest_via_crawl(host: str) -> list[tuple[str, str]]:
    """Crawl homepage + 1-level into doc-page subpages and harvest PDF links."""
    base = f"https://{host}/"
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    out = []
    seen = set()

    def fetch(url: str) -> str | None:
        try:
            r = session.get(url, timeout=20, allow_redirects=True)
            if r.status_code != 200: return None
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "text" not in ctype: return None
            return r.text
        except requests.RequestException:
            return None

    def harvest(html: str, page_url: str):
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#","mailto:","javascript:","tel:")): continue
            full = urljoin(page_url, href)
            text = (a.get_text(" ", strip=True) or "")[:140]
            if full.lower().split("?",1)[0].endswith(".pdf"):
                if full in seen: continue
                seen.add(full)
                out.append((full, text))
        return soup

    home = fetch(base)
    if not home:
        return out
    soup = harvest(home, base)
    # Depth-1 crawl into doc-pages
    sub_urls = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        full = urljoin(base, href)
        if urlparse(full).netloc != host: continue
        if full.lower().split("?",1)[0].endswith(".pdf"): continue
        text = a.get_text(" ", strip=True) or ""
        path = urlparse(full).path or "/"
        if DOCS_PAGE_RE.search(path) or DOCS_PAGE_RE.search(text):
            if full not in sub_urls:
                sub_urls.append(full)
        if len(sub_urls) >= 12:
            break
    for su in sub_urls:
        time.sleep(0.4)
        sh = fetch(su)
        if sh:
            harvest(sh, su)
    return out


def match_pdf_to_hoa(url: str, title: str, hoa_names: list[str]) -> str | None:
    blob_tokens = normalize_tokens(f"{url} {title}")
    best, best_score = None, 0
    for name in hoa_names:
        all_tokens = normalize_tokens(name)
        specific = {t for t in all_tokens if t and t not in GENERIC_TOKENS and len(t) > 2}
        if not specific: continue
        overlap = len(specific & blob_tokens)
        if overlap == 0: continue
        if overlap > best_score:
            best_score = overlap; best = name
    return best if best_score >= 1 else None


def cluster_leads(leads_path: Path, min_cluster: int = 4):
    clusters = defaultdict(list)
    for line in leads_path.read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        rec = json.loads(line)
        raw = (rec.get("sos_address_raw") or "").strip()
        if not raw: continue
        addr = re.sub(r"\s+", " ", raw).upper()
        addr = re.sub(r"\b(SUITE|UNIT|APT|STE|#)\s*[A-Z0-9-]+\b", "", addr)
        addr = re.sub(r"^C/O\s+[A-Z0-9 .,&'-]+?\s+(?=\d)", "", addr)
        addr = re.sub(r",?\s*USA?\s*$", "", addr).strip()
        if not addr: continue
        clusters[addr].append(rec)
    return {a: leads for a, leads in clusters.items() if len(leads) >= min_cluster}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--leads", default=str(ROOT / "state_scrapers/ri/leads/ri_sos_associations.jsonl"))
    p.add_argument("--output", default=str(ROOT / "state_scrapers/ri/leads/ri_mgmt_company_leads.jsonl"))
    args = p.parse_args()

    clusters = cluster_leads(Path(args.leads), min_cluster=4)
    by_name = {l["name"]: l for line in Path(args.leads).read_text().splitlines() if line.strip() for l in [json.loads(line)]}

    new_leads = []
    summary = []
    for addr, firm_meta in FIRMS.items():
        members = clusters.get(addr, [])
        host = firm_meta["host"]
        firm = firm_meta["firm"]
        names = [m["name"] for m in members]
        print(f"\n[{firm}] {len(members)} entities @ {addr}", file=sys.stderr)

        # Strategy 1: Serper
        serper_pdfs = harvest_via_serper(host)
        # Strategy 2: Crawl
        crawl_pdfs = harvest_via_crawl(host)
        # Merge
        all_pdfs = list({p[0]: p for p in serper_pdfs + crawl_pdfs}.values())
        print(f"  serper={len(serper_pdfs)} crawl={len(crawl_pdfs)} unique={len(all_pdfs)}", file=sys.stderr)

        if not all_pdfs:
            summary.append({"firm": firm, "host": host, "members": len(members), "pdfs": 0, "matched": 0})
            continue

        # Match PDFs to HOAs
        assignments = defaultdict(list)
        unmatched = []
        for url, title in all_pdfs:
            hoa = match_pdf_to_hoa(url, title, names) if names else None
            if hoa: assignments[hoa].append(url)
            else: unmatched.append((url, title))

        print(f"  matched: {len(assignments)} HOAs ({sum(len(v) for v in assignments.values())} URLs); unmatched={len(unmatched)}", file=sys.stderr)
        for u, t in unmatched[:5]:
            print(f"    UNMATCHED: {t[:50]:50s} {u[:80]}", file=sys.stderr)
        for hoa, urls in list(assignments.items())[:5]:
            print(f"    MATCH {hoa[:50]:50s} → {len(urls)} URLs", file=sys.stderr)

        for hoa, urls in assignments.items():
            base = by_name.get(hoa) or {"name": hoa, "state": "RI"}
            new_leads.append({
                "name": hoa,
                "source": f"mgmt-{host}",
                "source_url": f"https://{host}/",
                "state": "RI",
                "city": base.get("city"),
                "county": base.get("county"),
                "postal_code": base.get("postal_code"),
                "website": f"https://{host}/",
                "pre_discovered_pdf_urls": urls,
                "mgmt_firm": firm,
            })

        summary.append({
            "firm": firm, "host": host, "members": len(members),
            "pdfs": len(all_pdfs),
            "matched_hoas": len(assignments),
            "matched_urls": sum(len(v) for v in assignments.values()),
            "unmatched": len(unmatched),
        })

    Path(args.output).write_text("\n".join(json.dumps(l, sort_keys=True) for l in new_leads))
    print(json.dumps({"leads_written": len(new_leads), "by_firm": summary, "output": args.output}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
