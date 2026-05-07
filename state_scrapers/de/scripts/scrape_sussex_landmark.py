#!/usr/bin/env python3
"""Bank Sussex County HOA governing documents from Landmark Web."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.cloud import storage
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from hoaware.bank import DocumentInput, bank_hoa, slugify  # noqa: E402

BASE_URL = "https://deeds.sussexcountyde.gov/LandmarkWeb"
STATE = "DE"
COUNTY = "Sussex"
SOURCE = "de-sussex-landmark-web"
USER_AGENT = (
    os.environ.get("HOA_DISCOVERY_USER_AGENT")
    or "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
)

SEARCH_TERMS = [
    "HOMEOWNERS ASSOCIATION",
    "HOME OWNERS ASSOCIATION",
    "PROPERTY OWNERS ASSOCIATION",
    "OWNERS ASSOCIATION",
    "CONDOMINIUM ASSOCIATION",
    "MAINTENANCE CORPORATION",
    "CIVIC ASSOCIATION",
]

KEEP_DOC_RE = re.compile(
    r"\b(bylaws?|by-laws?|declarations?|decla|covenants?|restrictions?|restated|"
    r"code\s+of\s+regulations|rules?|regulations?|amend(?:ment)?|plat|plan)\b",
    re.I,
)
DROP_DOC_RE = re.compile(
    r"\b(release\s+of\s+(?:assessment\s+)?lien|rel\s+of\s+lien|assessment\s+lien|"
    r"mortgage|satisfaction|power\s+of\s+attorney|deed\b|right\s+of\s+way)\b",
    re.I,
)
ASSOCIATION_RE = re.compile(
    r"\b(home\s*owners?|homeowners?|property\s+owners?|owners?|condominium|"
    r"maintenance|civic)\s+(?:association|corporation|corp)\b",
    re.I,
)
ASSOCIATION_NAME_RE = re.compile(
    r"\b([A-Z0-9][A-Z0-9 &'.,-]{2,80}?\s+(?:home\s*owners?|homeowners?|"
    r"property\s+owners?|owners?|condominium|maintenance|civic)\s+"
    r"(?:association|corporation|corp)(?:\s+inc)?(?:\s+the)?)\b",
    re.I,
)
BAD_NAME_RE = re.compile(
    r"\b(home\s+owners?\s+loan|bank|mortgage|lending|llc|l\.l\.c\.|developer|"
    r"development\s+inc|county\s+of|state\s+of\s+delaware)\b",
    re.I,
)
PRINT_TOKEN_RE = re.compile(r"PrintDoc\(\"([^\"]+)\"")


@dataclass
class LandmarkRow:
    doc_id: str
    row_number: str
    search_name: str
    direct_name: str
    indirect_name: str
    recorded_date: str
    doc_type: str
    book_type: str
    book: str
    page: str
    instrument_number: str
    legal: str
    remarks: str
    image_count: int


def now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def clean_cell(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    for prefix in ("nobreak_", "hidden_", "unclickable_", "legalfield_"):
        value = value.replace(prefix, "")
    return " ".join(value.split())


def clean_name(value: str) -> str:
    value = clean_cell(value)
    value = re.sub(r"\bINCORPORATED\b", "INC", value, flags=re.I)
    value = re.sub(r"\bCORP\.\b", "CORPORATION", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" ,.-")
    return value.title()


def category_hint(row: LandmarkRow) -> str | None:
    hay = f"{row.doc_type} {row.remarks}"
    if re.search(r"\bby-?laws?\b", hay, re.I):
        return "bylaws"
    if re.search(r"\b(amend|amendment)\b", hay, re.I):
        return "amendment"
    if re.search(r"\b(code\s+of\s+regulations|rules?|regulations?)\b", hay, re.I):
        return "rules"
    if re.search(r"\b(plat|plan)\b", hay, re.I):
        return "plat"
    if re.search(r"\b(declarations?|decla|covenants?|restrictions?|restated)\b", hay, re.I):
        return "ccr"
    return None


def row_from_datatable(payload: dict[str, Any]) -> LandmarkRow | None:
    row_id = str(payload.get("DT_RowId") or "")
    match = re.match(r"doc_(\d+)_(\d+)", row_id)
    if not match:
        return None
    image_count = clean_cell(str(payload.get("21") or "0"))
    return LandmarkRow(
        doc_id=match.group(1),
        row_number=match.group(2),
        search_name=clean_cell(str(payload.get("4") or "")),
        direct_name=clean_cell(str(payload.get("5") or "")),
        indirect_name=clean_cell(str(payload.get("6") or "")),
        recorded_date=clean_cell(str(payload.get("7") or "")),
        doc_type=clean_cell(str(payload.get("8") or "")),
        book_type=clean_cell(str(payload.get("9") or "")),
        book=clean_cell(str(payload.get("10") or "")),
        page=clean_cell(str(payload.get("11") or "")),
        instrument_number=clean_cell(str(payload.get("12") or "")),
        legal=clean_cell(str(payload.get("15") or "")),
        remarks=clean_cell(str(payload.get("15") or "")),
        image_count=int(image_count) if image_count.isdigit() else 0,
    )


def candidate_name(row: LandmarkRow) -> str | None:
    candidates: list[str] = []
    for value in (row.search_name, row.direct_name, row.indirect_name):
        candidates.extend(match.group(1) for match in ASSOCIATION_NAME_RE.finditer(value))
        parts = re.split(r"\s{2,}|;|/", value)
        for part in parts:
            candidates.append(part)
    for candidate in candidates:
        name = clean_name(candidate)
        name = re.sub(r"\bI$", "Inc", name)
        name = re.sub(r"\bThe$", "", name).strip()
        if ASSOCIATION_RE.search(name) and not BAD_NAME_RE.search(name):
            return name
    return None


def should_keep_row(row: LandmarkRow, hoa_name: str | None, *, page_cap: int) -> tuple[bool, str | None]:
    hay = f"{row.search_name} {row.direct_name} {row.indirect_name} {row.doc_type} {row.legal} {row.remarks}"
    if not hoa_name:
        return False, "no_association_name"
    if row.image_count <= 0:
        return False, "no_images"
    if row.image_count > page_cap:
        return False, f"page_cap:{row.image_count}"
    if DROP_DOC_RE.search(hay) and not KEEP_DOC_RE.search(hay):
        return False, "lien_or_transactional"
    if not KEEP_DOC_RE.search(hay):
        return False, "no_governing_signal"
    return True, None


def landmark_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    response = session.get(BASE_URL, timeout=30)
    response.raise_for_status()
    response = session.post(f"{BASE_URL}/Search/SetDisclaimer", timeout=30)
    response.raise_for_status()
    response = session.get(f"{BASE_URL}/search/index?theme=.blue&section=searchCriteriaName", timeout=30)
    response.raise_for_status()
    return session


def run_name_search(session: requests.Session, term: str, *, record_count: int) -> None:
    criteria = {
        "searchLikeType": "1",
        "type": "0",
        "name": term,
        "doctype": "",
        "bookType": "0",
        "beginDate": "",
        "endDate": "",
        "recordCount": str(record_count),
        "exclude": "false",
        "ReturnIndexGroups": "false",
        "townName": "",
        "selectedNamesIds": "",
        "includeNickNames": "false",
        "selectedNames": "",
        "mobileHomesOnly": "false",
    }
    response = session.post(f"{BASE_URL}/Search/NameSearch", data=criteria, timeout=90)
    response.raise_for_status()
    if "Error" in response.text[:200]:
        raise RuntimeError(f"Landmark search failed for {term}")


def fetch_results(session: requests.Session, *, length: int) -> list[LandmarkRow]:
    response = session.post(
        f"{BASE_URL}/Search/GetSearchResults",
        data={"draw": "1", "start": "0", "length": str(length)},
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    rows = []
    for item in payload.get("data") or []:
        row = row_from_datatable(item)
        if row:
            rows.append(row)
    return rows


def open_document(session: requests.Session, row: LandmarkRow) -> str:
    response = session.post(
        f"{BASE_URL}/Document/Index",
        data={"id": row.doc_id, "row": row.row_number, "time": str(time.time()), "navigationType": "0"},
        timeout=90,
    )
    response.raise_for_status()
    return response.text


def activate_document_search(session: requests.Session, row: LandmarkRow) -> LandmarkRow:
    """Refresh Landmark's server-side result context before opening a document."""
    run_name_search(session, row.search_name or row.direct_name, record_count=100)
    rows = fetch_results(session, length=100)
    for refreshed in rows:
        if refreshed.doc_id == row.doc_id:
            return refreshed
    return row


def extract_print_token(document_html: str) -> str:
    tokens = PRINT_TOKEN_RE.findall(document_html)
    if not tokens:
        raise RuntimeError("no print token found")
    return tokens[-1]


def printable_images_to_pdf(print_html: str) -> bytes:
    soup = BeautifulSoup(print_html, "html.parser")
    images: list[Image.Image] = []
    for input_el in soup.find_all("input"):
        value = str(input_el.get("value") or "")
        if not value.startswith("data:image/"):
            continue
        b64 = value.split(",", 1)[1]
        img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
        images.append(img)
    if not images:
        raise RuntimeError("printable response did not contain image pages")
    output = io.BytesIO()
    first, *rest = images
    first.save(output, format="PDF", save_all=True, append_images=rest, resolution=200.0)
    return output.getvalue()


def download_pdf(session: requests.Session, token: str) -> bytes:
    response = session.get(
        f"{BASE_URL}/Document/GetDocumentForPrintPNG/",
        params={"request": token},
        timeout=120,
    )
    response.raise_for_status()
    return printable_images_to_pdf(response.text)


def download_pdf_pages(session: requests.Session, row: LandmarkRow) -> bytes:
    images: list[Image.Image] = []
    for page_num in range(1, row.image_count + 1):
        response = session.get(
            f"{BASE_URL}/Document/GetDocumentImage/",
            params={
                "documentId": row.doc_id,
                "index": "0",
                "pageNum": str(page_num),
                "type": "normal",
                "time": str(time.time()),
                "rotate": "0",
            },
            timeout=90,
        )
        response.raise_for_status()
        ctype = response.headers.get("content-type", "").lower()
        if "image" not in ctype:
            raise RuntimeError(f"page {page_num} was not an image: {ctype}")
        images.append(Image.open(io.BytesIO(response.content)).convert("RGB"))
    if not images:
        raise RuntimeError("no document pages downloaded")
    output = io.BytesIO()
    first, *rest = images
    first.save(output, format="PDF", save_all=True, append_images=rest, resolution=200.0)
    return output.getvalue()


def discover(args: argparse.Namespace, run_dir: Path) -> list[tuple[LandmarkRow, str]]:
    session = landmark_session()
    ledger = run_dir / "sussex_landmark_discovery.jsonl"
    by_doc: dict[str, tuple[LandmarkRow, str]] = {}
    for term in SEARCH_TERMS[: args.max_terms]:
        run_name_search(session, term, record_count=args.record_count)
        rows = fetch_results(session, length=args.result_length)
        append_jsonl(ledger, {"event": "search", "term": term, "rows": len(rows)})
        for row in rows:
            hoa_name = candidate_name(row)
            ok, reason = should_keep_row(row, hoa_name, page_cap=args.page_cap)
            append_jsonl(
                ledger,
                {
                    "event": "candidate" if ok else "skip",
                    "reason": reason,
                    "hoa_name": hoa_name,
                    "row": asdict(row),
                },
            )
            if ok and hoa_name and row.doc_id not in by_doc:
                by_doc[row.doc_id] = (row, hoa_name)
            if len(by_doc) >= args.max_candidates:
                return list(by_doc.values())
        time.sleep(args.search_delay_s)
    return list(by_doc.values())


def bank_candidates(args: argparse.Namespace, run_dir: Path, candidates: list[tuple[LandmarkRow, str]]) -> dict[str, Any]:
    if not args.apply:
        return {"skipped": True, "reason": "not_apply", "candidates": len(candidates)}
    session = landmark_session()
    client = storage.Client()
    existing_doc_ids = existing_landmark_doc_ids(client, args.bank_bucket) if args.skip_existing else set()
    ledger = run_dir / "sussex_landmark_bank.jsonl"
    banked = 0
    failed = 0
    skipped = 0
    seen_sha: set[str] = set()
    for row, hoa_name in candidates:
        if row.doc_id in existing_doc_ids:
            skipped += 1
            append_jsonl(ledger, {"event": "skip", "reason": "existing_document_id", "doc_id": row.doc_id, "hoa_name": hoa_name})
            continue
        try:
            row = activate_document_search(session, row)
            if row.doc_id in existing_doc_ids:
                skipped += 1
                append_jsonl(ledger, {"event": "skip", "reason": "existing_document_id", "doc_id": row.doc_id, "hoa_name": hoa_name})
                continue
            document_html = open_document(session, row)
            if not PRINT_TOKEN_RE.search(document_html):
                raise RuntimeError("document did not expose print controls")
            pdf_bytes = download_pdf_pages(session, row)
            sha = hashlib.sha256(pdf_bytes).hexdigest()
            if sha in seen_sha:
                append_jsonl(ledger, {"event": "skip", "reason": "duplicate_sha_in_run", "doc_id": row.doc_id, "sha256": sha})
                continue
            seen_sha.add(sha)
            filename_id = row.instrument_number or f"{row.book}-{row.page}" or row.doc_id
            uri = bank_hoa(
                name=hoa_name,
                metadata_type="hoa",
                address={"state": STATE, "county": COUNTY},
                geometry={},
                website={
                    "url": "https://sussexcountyde.gov/recorder-deeds",
                    "platform": "sussex-landmark-web",
                    "is_walled": False,
                },
                metadata_source={
                    "source": SOURCE,
                    "source_url": f"{BASE_URL}/search/index",
                    "fields_provided": ["name", "state", "county", "documents"],
                    "document_id": row.doc_id,
                    "instrument_number": row.instrument_number,
                    "book": row.book,
                    "page": row.page,
                    "book_type": row.book_type,
                    "recorded_date": row.recorded_date,
                    "document_type": row.doc_type,
                    "legal": row.legal,
                    "remarks": row.remarks,
                    "image_count": row.image_count,
                },
                documents=[
                    DocumentInput(
                        pdf_bytes=pdf_bytes,
                        source_url=f"{BASE_URL}/Document/Index?id={row.doc_id}",
                        filename=f"sussex-{slugify(hoa_name)}-{filename_id or row.doc_id}.pdf",
                        category_hint=category_hint(row),
                        text_extractable_hint=False,
                    )
                ],
                state_verified_via="sussex-county-landmark-web",
                bucket_name=args.bank_bucket,
                client=client,
            )
            banked += 1
            append_jsonl(
                ledger,
                {
                    "event": "banked",
                    "hoa_name": hoa_name,
                    "doc_id": row.doc_id,
                    "sha256": sha,
                    "manifest_uri": uri,
                    "page_count": row.image_count,
                },
            )
            existing_doc_ids.add(row.doc_id)
        except Exception as exc:
            failed += 1
            append_jsonl(ledger, {"event": "error", "doc_id": row.doc_id, "hoa_name": hoa_name, "error": f"{type(exc).__name__}: {exc}"})
        time.sleep(args.document_delay_s)
    return {"candidates": len(candidates), "banked": banked, "failed": failed, "skipped": skipped}


def existing_landmark_doc_ids(client: storage.Client, bucket_name: str) -> set[str]:
    bucket = client.bucket(bucket_name)
    doc_ids: set[str] = set()
    for blob in bucket.list_blobs(prefix=f"v1/{STATE}/sussex/"):
        if not blob.name.endswith("/manifest.json"):
            continue
        try:
            manifest = json.loads(blob.download_as_bytes())
        except Exception:
            continue
        for item in manifest.get("metadata_sources") or []:
            if isinstance(item, dict) and item.get("source") == SOURCE and item.get("document_id"):
                doc_ids.add(str(item["document_id"]))
    return doc_ids


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=f"sussex_landmark_{now_id()}")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--bank-bucket", default=os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank"))
    parser.add_argument("--max-terms", type=int, default=len(SEARCH_TERMS))
    parser.add_argument("--max-candidates", type=int, default=45)
    parser.add_argument("--record-count", type=int, default=500)
    parser.add_argument("--result-length", type=int, default=500)
    parser.add_argument("--page-cap", type=int, default=80)
    parser.add_argument("--search-delay-s", type=float, default=0.5)
    parser.add_argument("--document-delay-s", type=float, default=0.5)
    parser.add_argument("--skip-existing", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    run_dir = ROOT / "state_scrapers/de/results" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    candidates = discover(args, run_dir)
    (run_dir / "sussex_landmark_candidates.json").write_text(
        json.dumps([{"hoa_name": name, "row": asdict(row)} for row, name in candidates], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    result = bank_candidates(args, run_dir, candidates)
    (run_dir / "sussex_landmark_report.json").write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
