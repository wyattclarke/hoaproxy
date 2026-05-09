"""Direct-PDF mine productive HOA-owned and HOA-adjacent domains.

Source-family promotion (playbook Phase 2): after a domain has produced ≥2
banked PDFs in prior keyword sweeps, stop using models on it and mine it
deterministically — `site:{domain} filetype:pdf` Serper queries against
known governing-doc keywords, then bank each result via `hoaware.bank.bank_hoa`.

For this run the productive domains were extracted from
`benchmark/results/il_serper_docpages_*/probe_results.jsonl`. See
`state_scrapers/il/notes/source-inventory.md` Tier 2.

Cost: ~10 Serper queries per domain × ~30 domains = ~300 queries (~$0.30).
Most results dedup against the existing bank.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from hoaware.bank import DocumentInput, bank_hoa  # noqa: E402

SERPER_URL = "https://google.serper.dev/search"


# Confirmed-productive domains from session-2 lead mining + manual additions.
# Format: (domain, county_slug, county_name, hoa_name_hint).
# `hoa_name_hint` is used as the bank manifest's `name` since these domains
# typically host docs for a single named HOA. If None, derive from filename.
PRODUCTIVE_DOMAINS = [
    # Confirmed HOA-owned (single-HOA domains)
    ("harborpointehoa.com", "tazewell", "Tazewell", "East Peoria Harbor Pointe Homeowners' Association, Inc."),
    ("www.libertyonthelakehoa.com", "will", "Will", "Liberty on the Lake Homeowners Association"),
    ("chadwickestates.org", "champaign", "Champaign", "Chadwick Place Section One Homeowners Association"),
    ("neuhavenhoa.com", "lake", "Lake", "Neuhaven Homeowners Association"),
    ("www.shadowlakesassociation.com", "kane", "Kane", "Shadow Lakes II Association"),
    ("ashlandparkhoa.org", "champaign", "Champaign", "Ashland Park Homeowners Association"),
    ("oaklandhillshoa.org", "winnebago", "Winnebago", "Oak Land Hills Homeowners Association"),
    ("thegrovebloomington.com", "mclean", "McLean", "The Grove on Kickapoo Creek Second Addition Homeowners Association"),
    ("www.windingcreek.org", None, None, None),  # county TBD
    ("www.nppoa.org", None, None, "North Pointe Property Owners Association"),
    ("www.leapo.com", None, None, "Loch Lomond Property Owners Association"),
    ("www.111echestnut.org", "cook", "Cook", "111 East Chestnut Condominium Association"),  # Cook = Chicagoland — banks under cook/ but DON'T touch live entries
    ("www.wheatlandshoa.org", None, None, "The Wheatlands Homeowners Association"),
    ("www.willowwalkpalatine.org", "cook", "Cook", "Willow Walk Homeowners Association"),  # Palatine = Cook
    ("foxpoint.org", None, None, None),
    ("www.wheatlands.com", None, None, None),
    ("cobblecreeksubdivision.org", "champaign", "Champaign", "Cobble Creek Subdivision Homeowner's Association"),
    ("braesidecondomgmt.com", "cook", "Cook", None),  # mgmt co, Chicagoland
    ("static.secure.website", None, None, None),  # CDN — likely various
    ("brellingerhoa.org", None, None, "Brellinger Homeowners Association"),

    # Multi-HOA mgmt-co / developer / municipal domains: harvest broadly,
    # bank with hint=None (pre-bank name inference will derive from snippet/file)
    ("mperial.com", "cook", "Cook", None),  # Mperial Asset Mgmt — Cook (Chicagoland skip in cleanup)
    ("osbornproperties.com", None, None, None),  # Multi-HOA developer
    ("u.realgeeks.media", None, None, None),
    ("www.markmonge.com", "peoria", "Peoria", None),  # Peoria realtor
    ("www.shermanil.org", "sangamon", "Sangamon", None),  # Sherman, Sangamon
    ("www.chathamil.gov", "sangamon", "Sangamon", None),  # Chatham, Sangamon
    ("cuhomerentals.com", "champaign", "Champaign", None),  # Champaign-Urbana rentals
    ("tazewell-il.gov", "tazewell", "Tazewell", None),
    ("dekalbcounty.org", "dekalb", "DeKalb", None),
    ("www.warrenville.il.us", "dupage", "DuPage", None),
    ("cwadams.com", None, None, None),
]

GOVERNING_DOC_QUERIES = [
    'site:{domain} filetype:pdf (Declaration OR Bylaws OR Covenants)',
    'site:{domain} filetype:pdf (Restrictions OR Articles OR Amendment)',
    'site:{domain} filetype:pdf (Master Deed OR Condominium OR Association)',
]


def slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:60] or "unknown"


def serper_search(query: str, api_key: str, num: int = 10) -> list[dict]:
    try:
        r = requests.post(
            SERPER_URL,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num, "gl": "us"},
            timeout=30,
        )
        if r.status_code != 200:
            return []
        return r.json().get("organic") or []
    except Exception as exc:
        print(f"  serper error: {exc}", file=sys.stderr)
        return []


def fetch_pdf(url: str, max_bytes: int = 30_000_000) -> bytes | None:
    try:
        r = requests.get(url, timeout=45, allow_redirects=True, stream=True)
        if r.status_code != 200:
            return None
        ct = r.headers.get("content-type", "").lower()
        if not (ct.startswith("application/pdf") or ct.startswith("application/octet-stream")
                or url.lower().endswith(".pdf")):
            return None
        out = bytearray()
        for chunk in r.iter_content(8192):
            out.extend(chunk)
            if len(out) > max_bytes:
                return None
        if len(out) < 1024 or not bytes(out[:4]).startswith(b"%PDF"):
            return None
        return bytes(out)
    except Exception:
        return None


def derive_filename(url: str) -> str:
    p = urllib.parse.urlparse(url).path
    fn = p.rsplit("/", 1)[-1] or "document.pdf"
    return urllib.parse.unquote(fn)[:200]


def derive_name_from_filename(filename: str) -> str:
    """Heuristic name from PDF filename when we don't have a hint."""
    n = re.sub(r"\.pdf$", "", filename, flags=re.I)
    n = re.sub(r"[-_]+", " ", n).strip()
    # drop common doc-type suffixes
    n = re.sub(r"\b(Declaration|Bylaws|Covenants|Restrictions|Amendment|Articles|Inc)\b.*$", "", n, flags=re.I).strip()
    return n[:80] or "Unknown HOA"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", default="state_scrapers/il/results/owned_domain_mining")
    p.add_argument("--bank-bucket", default="hoaproxy-bank")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--max-pdfs-per-domain", type=int, default=20)
    p.add_argument("--sleep-s", type=float, default=1.0)
    p.add_argument("--filter-domains", help="Comma-separated subset to mine (default: all PRODUCTIVE_DOMAINS)")
    args = p.parse_args()

    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        print("SERPER_API_KEY not set", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = out_dir / "owned_domain_mining_ledger.jsonl"

    targets = PRODUCTIVE_DOMAINS
    if args.filter_domains:
        wanted = set(d.strip() for d in args.filter_domains.split(","))
        targets = [t for t in targets if t[0] in wanted]

    seen_urls: set[str] = set()
    decisions = []
    banked = 0

    for domain, county_slug, county_name, hoa_name_hint in targets:
        print(f"\n=== {domain} ({county_name or 'unknown county'}) ===", file=sys.stderr)
        domain_urls: list[tuple[str, str, str]] = []  # (url, title, snippet)

        for tmpl in GOVERNING_DOC_QUERIES:
            query = tmpl.format(domain=domain)
            results = serper_search(query, api_key, num=10)
            for r in results:
                u = r.get("link") or ""
                if not u or u in seen_urls:
                    continue
                if not u.lower().endswith(".pdf"):
                    continue
                seen_urls.add(u)
                domain_urls.append((u, r.get("title") or "", r.get("snippet") or ""))
            time.sleep(0.5)

        print(f"  found {len(domain_urls)} candidate PDFs", file=sys.stderr)
        if not domain_urls:
            decisions.append({"domain": domain, "status": "no_results"})
            continue

        for url, title, snippet in domain_urls[: args.max_pdfs_per_domain]:
            pdf_bytes = fetch_pdf(url)
            time.sleep(args.sleep_s)
            if pdf_bytes is None:
                decisions.append({"domain": domain, "url": url, "status": "fetch_failed"})
                continue

            # Pin the canonical name when known; else fall back to title/filename
            filename = derive_filename(url)
            if hoa_name_hint:
                pinned_name = hoa_name_hint
            elif title and len(title) > 4:
                # Strip ".pdf" / file-extension noise from title
                pinned_name = re.sub(r"\s+[-|·•]\s+.*$", "", title).strip()[:80]
                if not pinned_name or pinned_name.lower().endswith(".pdf"):
                    pinned_name = derive_name_from_filename(filename)
            else:
                pinned_name = derive_name_from_filename(filename)

            address: dict[str, Any] = {"state": "IL"}
            if county_name:
                address["county"] = county_name

            if not args.apply:
                decisions.append({"domain": domain, "url": url, "would_bank": pinned_name, "filename": filename, "county": county_name, "status": "dry_run"})
                continue

            try:
                manifest_uri = bank_hoa(
                    name=pinned_name,
                    metadata_type="hoa",
                    address=address,
                    geometry={},
                    website=None,
                    metadata_source={
                        "source": "il-owned-domain-direct-pdf",
                        "source_url": url,
                        "domain": domain,
                        "discovery_pattern": "source-family-promotion",
                        "search_query": f"site:{domain} filetype:pdf",
                    },
                    documents=[
                        DocumentInput(
                            pdf_bytes=pdf_bytes,
                            source_url=url,
                            filename=filename,
                            category_hint=None,
                            text_extractable_hint=None,
                        )
                    ],
                    state_verified_via="il-owned-domain-direct",
                    bucket_name=args.bank_bucket,
                    pinned_name=bool(hoa_name_hint),
                )
                banked += 1
                decisions.append({
                    "domain": domain, "url": url, "name": pinned_name,
                    "manifest_uri": manifest_uri,
                    "status": "banked",
                })
                print(f"    banked {url[:80]} -> {pinned_name!r}", file=sys.stderr)
            except Exception as exc:
                decisions.append({"domain": domain, "url": url, "error": f"{type(exc).__name__}: {exc}", "status": "bank_error"})

        # incremental flush
        ledger_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))

    ledger_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
    print(json.dumps({
        "domains_processed": len(targets),
        "candidates_found": sum(1 for d in decisions if d.get("status") in ("banked", "fetch_failed", "dry_run", "bank_error")),
        "banked": banked,
        "ledger": str(ledger_path),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
