#!/usr/bin/env python3
"""Strategy 2: site:-restricted Serper queries against known public
HOA-document-hosting platforms. Many small condos use Squarespace,
Wix, GoDaddy sitebuilder, or AWS S3 buckets to host governing docs
publicly even when their managing firm uses a walled portal.

Each PDF hit is matched to a SoS-derived HOA name by specific name-token
overlap; matched URLs are emitted as leads for probe-batch.
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
load_dotenv(ROOT / "settings.env", override=False)

SERPER = "https://google.serper.dev/search"

# Known public HOA-document-hosting platforms. Some host per-association
# microsites; some host PDFs directly.
HOST_TARGETS = [
    # Direct doc-host platforms
    "hoa-express.com",
    "cinchosting.com",
    "hoastart.com",
    "condoassociation.com",
    "condocerts.com",
    # Static-site builders that host HOA microsites
    # (NB: Squarespace/Wix index too noisy — limit to specific patterns)
    "*.squarespace.com",
    # Public AWS / GCP buckets
    "s3.amazonaws.com",
    "s3.us-east-1.amazonaws.com",
    "storage.googleapis.com",
    # WordPress hosts
    "wordpress.com",
    # Wix
    "*.wixsite.com",
    # General .org/.com/.net constraints when no host targets work
]

GENERIC_TOKENS = {
    "the","and","of","association","associations","condominium","condominiums",
    "condo","condos","homeowners","homeowner","owners","owner","property",
    "properties","inc","incorporated","corporation","corp","llc","limited",
    "rhode","island","ri","village","estates","homes","home","house",
    "apartment","apartments","place","court","park","phase","at","ii","iii","iv",
}


def normalize_tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def serper(query: str, *, num: int = 10) -> list[dict]:
    key = os.environ["SERPER_API_KEY"]
    r = requests.post(
        SERPER,
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        json={"q": query, "num": num, "gl": "us", "hl": "en"},
        timeout=30,
    )
    if r.status_code >= 400:
        return []
    return list(r.json().get("organic", []))


def match_pdf_to_hoa(url: str, title: str, snippet: str, hoa_names_specific: dict[str, set[str]]) -> str | None:
    blob_tokens = normalize_tokens(f"{url} {title} {snippet}")
    best, best_score = None, 0
    for name, specific in hoa_names_specific.items():
        if not specific: continue
        overlap = len(specific & blob_tokens)
        if overlap < 2: continue
        if overlap > best_score:
            best_score = overlap; best = name
    return best if best_score >= 2 else None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--leads", default=str(ROOT / "state_scrapers/ri/leads/ri_sos_associations.jsonl"))
    p.add_argument("--output", default=str(ROOT / "state_scrapers/ri/leads/ri_site_restricted_leads.jsonl"))
    p.add_argument("--polite", type=float, default=0.4)
    args = p.parse_args()

    sos_recs = [json.loads(l) for l in Path(args.leads).read_text().splitlines() if l.strip()]
    by_name = {r["name"]: r for r in sos_recs}
    name_specific = {}
    for r in sos_recs:
        all_tokens = normalize_tokens(r["name"])
        spec = {t for t in all_tokens if t and t not in GENERIC_TOKENS and len(t) > 2}
        if spec:
            name_specific[r["name"]] = spec

    queries = []
    # Build site-restricted queries for each platform
    for host in ["hoa-express.com", "cinchosting.com", "hoastart.com",
                 "condoassociation.com", "condocerts.com"]:
        queries.extend([
            f'site:{host} Rhode Island filetype:pdf',
            f'site:{host} "Rhode Island" condominium',
        ])
    # Static site / bucket queries
    queries.extend([
        '"Rhode Island" condominium declaration filetype:pdf inurl:squarespace',
        '"Rhode Island" homeowners association bylaws filetype:pdf inurl:squarespace',
        '"Rhode Island" condominium declaration filetype:pdf inurl:wixsite',
        '"Rhode Island" condominium declaration filetype:pdf site:s3.amazonaws.com',
        '"Rhode Island" condominium bylaws filetype:pdf site:storage.googleapis.com',
        '"Rhode Island" condo "declaration of condominium" filetype:pdf inurl:.org',
        '"Rhode Island" condominium "amended and restated" filetype:pdf -inurl:gov',
        '"Rhode Island" condominium "covenants conditions and restrictions" filetype:pdf',
        '"Rhode Island" "homeowners association" CCR filetype:pdf -inurl:gov',
        '"Rhode Island" condo declaration -inurl:sos -inurl:business filetype:pdf',
        # More mgmt-company-host targeted (since some firms DO publish):
        'site:thehennessygrp.com filetype:pdf',
        'site:apexmanagementgroup.net filetype:pdf',
        'site:summit-mgmtri.com filetype:pdf',
        'site:divineinvestments.com filetype:pdf',
        # Generic RI HOA condo doc fishing
        'inurl:condo-docs Rhode Island filetype:pdf',
        'inurl:governing-documents Rhode Island filetype:pdf',
        'inurl:hoa-documents Rhode Island filetype:pdf',
    ])

    all_hits: list[dict] = []
    seen = set()
    for q in queries:
        rows = serper(q, num=10)
        for r in rows:
            link = (r.get("link") or "").strip().split("#",1)[0]
            if not link.startswith(("http://","https://")): continue
            if link in seen: continue
            if not link.lower().split("?",1)[0].endswith(".pdf"): continue
            seen.add(link)
            r["_query"] = q
            all_hits.append(r)
        time.sleep(args.polite)
    print(f"Total unique PDF hits across {len(queries)} queries: {len(all_hits)}", file=sys.stderr)

    # Match hits to HOAs
    by_match: dict[str, list[str]] = {}
    unmatched: list[dict] = []
    for hit in all_hits:
        link = hit["link"]
        title = hit.get("title","")
        snippet = hit.get("snippet","")
        hoa = match_pdf_to_hoa(link, title, snippet, name_specific)
        if hoa:
            by_match.setdefault(hoa, []).append(link)
        else:
            unmatched.append(hit)

    print(f"Matched: {len(by_match)} HOAs ({sum(len(v) for v in by_match.values())} URLs)", file=sys.stderr)
    print(f"Unmatched (sample of 8):", file=sys.stderr)
    for h in unmatched[:8]:
        print(f"  {h.get('title','')[:60]:60s} | {h['link'][:80]}", file=sys.stderr)

    leads = []
    for hoa, urls in by_match.items():
        base = by_name.get(hoa) or {"name": hoa, "state": "RI"}
        leads.append({
            "name": hoa,
            "source": "site-restricted-serper",
            "source_url": urls[0] if urls else "",
            "state": "RI",
            "city": base.get("city"),
            "county": base.get("county"),
            "postal_code": base.get("postal_code"),
            "pre_discovered_pdf_urls": urls,
        })

    Path(args.output).write_text("\n".join(json.dumps(l, sort_keys=True) for l in leads))
    print(json.dumps({
        "queries_run": len(queries),
        "unique_pdf_hits": len(all_hits),
        "matched_hoas": len(by_match),
        "matched_urls": sum(len(v) for v in by_match.values()),
        "unmatched": len(unmatched),
        "output": args.output,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
