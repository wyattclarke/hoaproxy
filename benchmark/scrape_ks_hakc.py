#!/usr/bin/env python3
"""Scrape public Kansas HOA docs from Homes Association of Kansas City.

HA-KC exposes a Kansas association index and a public document endpoint:

    /scripts/showdocuments.php?an={association_id}&dt={doc_type}

This scraper is deterministic and only calls bank_hoa() when it finds at
least one governing-document PDF for an association.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from hoaware.bank import DocumentInput, bank_hoa  # noqa: E402


BASE = "https://www.ha-kc.org"
KS_INDEX = f"{BASE}/index.php/kansas-associations"
DOC_ENDPOINT = f"{BASE}/scripts/showdocuments.php"
USER_AGENT = (
    os.environ.get("HOA_DISCOVERY_USER_AGENT")
    or "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
)
PDF_MAGIC = b"%PDF-"
REQUEST_TIMEOUT = 25
PDF_TIMEOUT = 60
MAX_PDF_BYTES = 35 * 1024 * 1024

DOC_TYPES = {
    "B": "bylaws",
    "R": "ccr",
    "A": "articles",
    "P": "rules",
}
JUNK_DOC_RE = re.compile(
    r"\b(arc|architectural\s+review\s+form|application|request|form|certificate|"
    r"newsletter|minutes|financial|budget|invoice|coupon|pool|roster|directory)\b",
    re.IGNORECASE,
)
GOVDOC_RE = re.compile(
    r"\b(bylaws?|by-laws?|declaration|restrictions?|covenants?|cc&?rs?|"
    r"articles?|rules?|regulations?|guidelines?|policies?|amendments?)\b",
    re.IGNORECASE,
)


@dataclass
class Association:
    name: str
    page_url: str
    association_id: str


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    return s


def _jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        print(json.dumps(payload, sort_keys=True), file=f)


def _get_html(session: requests.Session, url: str) -> str | None:
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return resp.text


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" \t\r\n\u00a0")
    name = re.sub(r"\s+<.*$", "", name)
    return name


def scrape_associations(session: requests.Session) -> list[Association]:
    html = _get_html(session, KS_INDEX)
    if not html:
        raise RuntimeError(f"could not fetch {KS_INDEX}")
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, Association] = {}
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        text = _clean_name(a.get_text(" ") or "")
        if not text or len(text) < 3:
            continue
        if text in {"Home", "About Us", "Services", "Kansas Associations", "Missouri Associations", "Contact Us"}:
            continue
        page_url = urljoin(KS_INDEX, href)
        assoc_id = None
        m = re.search(r"/index\.php/(\d+)-", href)
        if m:
            assoc_id = m.group(1)
        elif "kansas-associations" in href:
            # These are Joomla category links in the index, not association
            # document ids. The real association pages appear separately.
            continue
        if not assoc_id:
            # Fetch named pages later only if they expose numeric document tabs.
            if not href.startswith("/index.php/") or any(x in href for x in ("about-us", "services", "contact-us")):
                continue
            assoc_id = discover_association_id(session, page_url)
        if not assoc_id:
            continue
        out[assoc_id] = Association(name=text, page_url=page_url, association_id=assoc_id)
    return list(out.values())


def discover_association_id(session: requests.Session, page_url: str) -> str | None:
    html = _get_html(session, page_url)
    if not html:
        return None
    ids = re.findall(
        r"/index\.php/(\d+)-[a-z-]*(?:bylaws|restrictions|rules|policies|articles)",
        html,
        re.IGNORECASE,
    )
    if ids:
        return ids[0]
    iframe_ids = re.findall(r"showdocuments\.php\?an=(\d+)&amp;dt=[A-Z]", html, re.IGNORECASE)
    return iframe_ids[0] if iframe_ids else None


def document_links(session: requests.Session, association_id: str) -> list[tuple[str, str, str]]:
    docs: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for dt, category in DOC_TYPES.items():
        url = f"{DOC_ENDPOINT}?an={association_id}&dt={dt}"
        html = _get_html(session, url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = str(a.get("href") or "")
            text = _clean_name(a.get_text(" ") or "")
            pdf_url = urljoin(BASE, href)
            if not pdf_url.lower().split("?", 1)[0].endswith(".pdf"):
                continue
            hay = f"{text} {pdf_url}"
            if JUNK_DOC_RE.search(hay):
                continue
            if not GOVDOC_RE.search(hay):
                continue
            if pdf_url in seen:
                continue
            seen.add(pdf_url)
            docs.append((pdf_url, category, text))
    return docs


def download_pdf(session: requests.Session, url: str) -> tuple[bytes | None, str | None]:
    try:
        resp = session.get(url, timeout=PDF_TIMEOUT, stream=True, allow_redirects=True)
    except requests.RequestException as exc:
        return None, f"request_{type(exc).__name__}"
    if resp.status_code != 200:
        resp.close()
        return None, f"status_{resp.status_code}"
    buf = bytearray()
    for chunk in resp.iter_content(64 * 1024):
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > MAX_PDF_BYTES:
            resp.close()
            return None, "too_large"
    resp.close()
    data = bytes(buf)
    if not data.startswith(PDF_MAGIC):
        return None, "not_pdf"
    return data, None


def run(args: argparse.Namespace) -> dict:
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "hoaware"
    run_dir = ROOT / "benchmark" / "results" / f"ks_hakc_{args.run_id}"
    audit = run_dir / "audit.jsonl"
    session = _session()
    associations = scrape_associations(session)
    _jsonl(audit, {"event": "associations", "count": len(associations)})

    probed = 0
    with_docs = 0
    banked_docs = 0
    skipped_docs = 0
    for assoc in associations[: args.max_associations]:
        probed += 1
        links = document_links(session, assoc.association_id)
        _jsonl(audit, {
            "event": "association",
            "name": assoc.name,
            "association_id": assoc.association_id,
            "page_url": assoc.page_url,
            "candidate_docs": len(links),
        })
        documents: list[DocumentInput] = []
        seen_sha: set[str] = set()
        for pdf_url, category, link_text in links[: args.max_docs_per_association]:
            pdf_bytes, skip = download_pdf(session, pdf_url)
            if skip or not pdf_bytes:
                skipped_docs += 1
                _jsonl(audit, {"event": "pdf_skipped", "hoa": assoc.name, "url": pdf_url, "reason": skip})
                continue
            sha = hashlib.sha256(pdf_bytes).hexdigest()
            if sha in seen_sha:
                continue
            seen_sha.add(sha)
            filename = pdf_url.rsplit("/", 1)[-1].split("?", 1)[0] or "document.pdf"
            documents.append(DocumentInput(
                pdf_bytes=pdf_bytes,
                source_url=pdf_url,
                filename=filename,
                category_hint=category,
                text_extractable_hint=None,
            ))
            _jsonl(audit, {"event": "pdf_downloaded", "hoa": assoc.name, "url": pdf_url, "category": category, "sha256": sha})
            time.sleep(args.delay)
        if not documents:
            continue
        with_docs += 1
        uri = bank_hoa(
            name=assoc.name,
            address={"state": "KS"},
            website={"url": assoc.page_url, "platform": "ha-kc", "is_walled": False},
            metadata_source={
                "source": "ha-kc-kansas-associations",
                "source_url": assoc.page_url,
                "fields_provided": ["name", "state", "website", "source_url"],
                "association_id": assoc.association_id,
            },
            documents=documents,
            bucket_name=args.bucket,
        )
        banked_docs += len(documents)
        _jsonl(audit, {"event": "banked", "hoa": assoc.name, "manifest_uri": uri, "documents": len(documents)})
        time.sleep(args.delay)

    summary = {
        "associations_found": len(associations),
        "associations_probed": probed,
        "associations_with_docs": with_docs,
        "banked_docs": banked_docs,
        "skipped_docs": skipped_docs,
        "run_dir": str(run_dir),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape HA-KC public Kansas HOA governing docs")
    ap.add_argument("--bucket", default=os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank"))
    ap.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    ap.add_argument("--max-associations", type=int, default=500)
    ap.add_argument("--max-docs-per-association", type=int, default=12)
    ap.add_argument("--delay", type=float, default=0.1)
    args = ap.parse_args()
    print(json.dumps(run(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
