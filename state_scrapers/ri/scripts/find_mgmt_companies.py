#!/usr/bin/env python3
"""For each address-cluster of 4+ RI HOAs in the SoS data, identify the
management company (Serper reverse-lookup on the address), find their
website, and harvest governing-doc PDFs from that website. Output a leads
JSONL where each lead is keyed on the HOA name in the cluster and carries
any PDF URL whose filename matches that HOA's name tokens.
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
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / "settings.env", override=False)

USER_AGENT = "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
SERPER = "https://google.serper.dev/search"

GENERIC_TOKENS = {
    "the", "and", "of", "association", "associations", "condominium", "condominiums",
    "condo", "condos", "homeowners", "homeowner", "owners", "owner", "property",
    "properties", "inc", "incorporated", "corporation", "corp", "llc", "limited",
    "rhode", "island", "ri", "village", "estates", "homes", "home", "house",
    "apartment", "apartments", "place", "court", "park", "phase",
}


def normalize_tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def serper_search(query: str, *, num: int = 10) -> list[dict]:
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        raise RuntimeError("SERPER_API_KEY missing")
    r = requests.post(
        SERPER,
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        json={"q": query, "num": num, "gl": "us", "hl": "en"},
        timeout=30,
    )
    if r.status_code >= 400:
        return []
    return list(r.json().get("organic", []))


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
        if not addr or addr == ",":
            continue
        clusters[addr].append(rec)
    return {a: leads for a, leads in clusters.items() if len(leads) >= min_cluster}


def identify_firm(address: str, cache: dict) -> dict | None:
    """Reverse-Serper the address to find the management firm + its website."""
    if address in cache:
        return cache[address]
    # Use a quoted address fragment that's likely unique enough
    short_addr = re.sub(r",.*$", "", address).strip()
    queries = [
        f'"{short_addr}" Rhode Island property management OR HOA',
        f'"{short_addr}" condominium association management',
    ]
    candidates: list[tuple[int, str, str]] = []  # (score, host, snippet)
    for q in queries:
        rows = serper_search(q, num=8)
        for r in rows:
            link = (r.get("link") or "").strip()
            title = (r.get("title") or "").strip()
            snippet = (r.get("snippet") or "").strip()
            if not link or not link.startswith(("http://","https://")):
                continue
            host = urlparse(link).netloc.lower()
            # Skip directory/listing/government sites
            if re.search(r"(yelp|bbb|zillow|realtor|redfin|trulia|opencorporates|"
                         r"yellowpages|loopnet|crexi|costar|facebook|linkedin|"
                         r"sos\.ri\.gov|business\.sos|\.gov)", host):
                continue
            text = f"{title} {snippet} {link}".lower()
            score = 0
            if any(t in text for t in ["property management","management company",
                                        "hoa management","condo management",
                                        "association management","communities served",
                                        "we manage"]):
                score += 10
            if "rhode island" in text or " ri " in text:
                score += 3
            if any(t in host for t in ["management","mgmt","property","realty"]):
                score += 4
            # Extract the address fragment match
            if short_addr.lower() in text:
                score += 5
            if score >= 8:
                candidates.append((score, host, link))
        time.sleep(0.4)
    if not candidates:
        cache[address] = None
        return None
    candidates.sort(reverse=True)
    # Group by host, take top
    best_host = candidates[0][1]
    homepage = f"https://{best_host}/"
    cache[address] = {"host": best_host, "homepage": homepage,
                      "evidence": candidates[0][2]}
    return cache[address]


def harvest_pdfs(host: str, polite: float = 0.3) -> list[tuple[str, str]]:
    """Run site:host filetype:pdf Serper to enumerate all PDFs the firm publishes."""
    pdfs: list[tuple[str, str]] = []  # (url, title)
    seen = set()
    for q in [
        f"site:{host} filetype:pdf",
        f"site:{host} declaration filetype:pdf",
        f"site:{host} bylaws filetype:pdf",
        f"site:{host} covenants filetype:pdf",
        f"site:{host} condominium filetype:pdf",
        f"site:{host} HOA filetype:pdf",
    ]:
        rows = serper_search(q, num=10)
        for r in rows:
            link = (r.get("link") or "").strip()
            if not link.endswith(".pdf"): continue
            if link in seen: continue
            seen.add(link)
            pdfs.append((link, r.get("title") or ""))
        time.sleep(polite)
    return pdfs


def match_pdf_to_hoa(pdf_url: str, pdf_title: str, hoa_names: list[str]) -> str | None:
    """Find the HOA name whose specific (non-generic) tokens overlap most with
    the PDF URL/title. Need at least 1 specific token match."""
    blob = f"{pdf_url} {pdf_title}".lower()
    blob_tokens = normalize_tokens(blob)
    best = None; best_score = 0
    for name in hoa_names:
        all_tokens = normalize_tokens(name)
        specific = {t for t in all_tokens if t and t not in GENERIC_TOKENS and len(t) > 2}
        if not specific: continue
        overlap = len(specific & blob_tokens)
        if overlap == 0: continue
        if overlap > best_score:
            best_score = overlap
            best = name
    return best if best_score >= 2 else None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--leads", default=str(ROOT / "state_scrapers/ri/leads/ri_sos_associations.jsonl"))
    p.add_argument("--output", default=str(ROOT / "state_scrapers/ri/leads/ri_mgmt_company_leads.jsonl"))
    p.add_argument("--min-cluster", type=int, default=4)
    p.add_argument("--cache", default=str(ROOT / "state_scrapers/ri/results/mgmt_firm_cache.json"))
    args = p.parse_args()

    leads_path = Path(args.leads)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path = Path(args.cache)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    clusters = cluster_leads(leads_path, args.min_cluster)
    print(f"Clusters: {len(clusters)}, total entities clustered: {sum(len(v) for v in clusters.values())}", file=sys.stderr)

    by_name = {l["name"]: l for line in leads_path.read_text().splitlines() if line.strip() for l in [json.loads(line)]}

    new_leads = []
    matched_count = 0
    firms_found = 0
    pdfs_total = 0

    for addr, members in sorted(clusters.items(), key=lambda x: -len(x[1])):
        firm = identify_firm(addr, cache)
        cache_path.write_text(json.dumps(cache, indent=2))
        if not firm:
            print(f"[skip] {len(members)} @ {addr[:60]} → no firm identified", file=sys.stderr)
            continue
        firms_found += 1
        host = firm["host"]
        print(f"[firm] {len(members)} @ {addr[:50]} → {host}", file=sys.stderr)
        pdfs = harvest_pdfs(host)
        pdfs_total += len(pdfs)
        if not pdfs:
            print(f"  no PDFs at {host}", file=sys.stderr)
            continue
        # For each PDF, try to match to one of the cluster's HOAs
        names = [m["name"] for m in members]
        pdf_assignments = defaultdict(list)
        unmatched = []
        for url, title in pdfs:
            hoa = match_pdf_to_hoa(url, title, names)
            if hoa:
                pdf_assignments[hoa].append(url)
            else:
                unmatched.append((url, title))
        print(f"  {len(pdfs)} PDFs → matched to {len(pdf_assignments)} HOAs ({sum(len(v) for v in pdf_assignments.values())} URLs); {len(unmatched)} unmatched", file=sys.stderr)
        for hoa_name, urls in pdf_assignments.items():
            base = by_name.get(hoa_name) or {"name": hoa_name}
            lead = {
                "name": hoa_name,
                "source": f"mgmt-company-{host}",
                "source_url": firm["homepage"],
                "state": "RI",
                "city": base.get("city"),
                "county": base.get("county"),
                "postal_code": base.get("postal_code"),
                "website": firm["homepage"],
                "pre_discovered_pdf_urls": urls,
                "mgmt_company_address": addr,
                "mgmt_company_host": host,
            }
            new_leads.append(lead)
            matched_count += 1

    out_path.write_text("\n".join(json.dumps(l, sort_keys=True) for l in new_leads))
    print(json.dumps({
        "clusters": len(clusters),
        "firms_identified": firms_found,
        "total_pdfs_found": pdfs_total,
        "leads_written": matched_count,
        "output": str(out_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
