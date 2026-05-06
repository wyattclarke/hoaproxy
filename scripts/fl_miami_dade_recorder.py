"""Miami-Dade County Recorder of Deeds scraper (ORIPS).

Fetches Declaration of Condominium (DCO) and Restrictions (RES) / Covenant (COV)
instruments from the Miami-Dade Clerk's Official Records system and emits Lead
JSONL rows suitable for hoaware.discovery probe-batch.

SYSTEM NOTES (from Phase-1 spike, 2026-05-05):
-----------------------------------------------
The original ASP.NET ORIPS (www2.miami-dadeclerk.gov/officialrecords/StandardSearch.aspx)
has been replaced by a React SPA at:
    https://onlineservices.miamidadeclerk.gov/officialrecords/

The backend REST API is:
    POST /officialrecords/api/home/standardsearch
         ?partyName=<name>
         &dateRangeFrom=<MM/DD/YYYY>
         &dateRangeTo=<MM/DD/YYYY>
         &documentType=<code>      # e.g. DCO, RES, COV, AIN
         &searchT=<code>
         &firstQuery=true
         &searchtype=documentType
    Headers: x-recaptcha-token: <google-v3-token>

Response: {"isValidSearch": bool, "qs": <string|null>}

When isValidSearch=true the qs token is used to paginate:
    GET /officialrecords/api/SearchResults/getStandardRecords?qs=<qs>

PDF image fetch:
    GET /officialrecords/api/DocumentImage/getdocumentimage
         ?sBook=<book>&sPage=<page>&sBookType=<type>&redact=false
    OR (preferred — returns encrypted path):
    GET /officialrecords/api/DocumentImage/getEncryptedImagePath?cfnMasterID=<id>
    GET /officialrecords/api/DocumentImage/getimagepaths?cfnMasterID=<id>

DOCUMENT TYPE CODES (full list from /officialrecords/api/home/documentTypes):
    DCO  = DECLARATION OF CONDOMINIUM
    RES  = RESTRICTIONS
    COV  = COVENANT
    AIN  = ARTICLES OF INCORPORATION
    DEE  = DEED
    MOR  = MORTGAGE

BLOCKER — reCAPTCHA v3 (site key: 6LfI8ikaAAAAAH0qlQMApskMGd1U6EqDyniH5t0x):
    Every POST to standardsearch requires a valid Google reCAPTCHA v3 token.
    The backend validates the token server-side. Empty/fake tokens return
    {"isValidSearch": false, "qs": null}. There is NO plain-HTTP fallback.

    Options to unblock:
    A. Register a Developer API account at:
       https://www2.miamidadeclerk.gov/Developers/Home/MyAccount
       The Developer API uses an AuthKey instead of reCAPTCHA. Costs "units"
       (presumably per-query). Contact: 305-275-1155.
    B. Use a headless browser (Playwright) to solve reCAPTCHA v3 legitimately
       (the token is valid for ~2 minutes). This is technically feasible but
       substantially increases complexity and maintenance burden.
    C. The system shows a UI message "Login to avoid reCaptcha" — a logged-in
       account session may bypass reCAPTCHA. Login at:
       https://www2.miamidadeclerk.gov/UserManagementServices/?hs=orb

TOS VERDICT — RESTRICTED (requires written permission for bulk storage):
    From the JS-embedded terms of service:
    "Other than making limited copies of this website's content, you may not
    reproduce, retransmit, redistribute, upload or post any part of this website,
    including the contents thereof, in any form or by any means, or store it in
    any information storage and retrieval system, without prior written permission
    from the Clerk and Comptroller's Office."
    "If you are interested in obtaining permission to reproduce, retransmit, or
    store any part of this website beyond that which you may use for personal use,
    as defined above, visit our Web API Services."
    => Verdict: Automated bulk download to GCS (the bank) requires either
       (a) registering for the commercial Developer API, or (b) obtaining written
       permission from the Clerk's office. This script is scaffolded for option (a).

This script is a SPIKE SCAFFOLD. It will run to completion if HOA_RECORDER_AUTHKEY
is set in the environment (Developer API). Without it, it exits with a clear
error message pointing to the unblock path.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://onlineservices.miamidadeclerk.gov/officialrecords"
DEV_API_BASE = "https://www2.miamidadeclerk.gov/Developers/api"
USER_AGENT = "HOAproxy public-records research (+https://hoaproxy.org; contact: hello@hoaproxy.org)"

# Instrument types relevant to HOA governing documents:
#   DCO  = Declaration of Condominium (Chapter 718)
#   RES  = Restrictions (CC&Rs / deed restrictions — Chapter 720 HOAs)
#   COV  = Covenant (some subdivisions record under this type)
#   AIN  = Articles of Incorporation (sometimes recorded alongside Declaration)
DEFAULT_INSTRUMENT_TYPES = ["DCO", "RES", "COV"]

SUPPORTED_INSTRUMENT_TYPES = {
    "DCO": "DECLARATION OF CONDOMINIUM",
    "RES": "RESTRICTIONS",
    "COV": "COVENANT",
    "AIN": "ARTICLES OF INCORPORATION",
    "DEE": "DEED",
    "MOR": "MORTGAGE",
    "AGR": "AGREEMENT",
    "EAS": "EASEMENT",
}

MIN_PACING_SECONDS = 2.0  # 1 query per 2 seconds minimum

logger = logging.getLogger("fl_miami_dade_recorder")


# ---------------------------------------------------------------------------
# Session setup
# ---------------------------------------------------------------------------

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


# ---------------------------------------------------------------------------
# Developer API path (AuthKey-based — avoids reCAPTCHA)
# ---------------------------------------------------------------------------

def search_via_dev_api(
    session: requests.Session,
    auth_key: str,
    instrument_type: str,
    date_from: str,
    date_to: str,
    max_records: int,
) -> list[dict]:
    """Search using the commercial Developer API (requires AuthKey).

    The Developer API lives at www2.miamidadeclerk.gov/Developers/api and uses
    an AuthKey parameter. Endpoint URL confirmed from the Help page. The exact
    parameter names for OfficialRecords StandardSearch are inferred from the
    public-facing API shape; adjust if the Clerk's office provides different docs.

    Returns a list of raw record dicts from the API.
    """
    url = f"{DEV_API_BASE}/OfficialRecords/StandardSearch"
    params = {
        "authKey": auth_key,
        "documentType": instrument_type,
        "dateRangeFrom": date_from,
        "dateRangeTo": date_to,
        "searchtype": "documentType",
        "firstQuery": "true",
    }
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # The API likely returns a list of records or a paginated envelope.
    # Shape TBD once we have a real AuthKey to test with.
    if isinstance(data, list):
        return data[:max_records]
    if isinstance(data, dict) and "records" in data:
        return data["records"][:max_records]
    logger.warning("Unexpected Developer API response shape: %s", type(data))
    return []


# ---------------------------------------------------------------------------
# Public SPA path (requires reCAPTCHA v3 — blocked without a real token)
# ---------------------------------------------------------------------------

def search_via_spa_api(
    session: requests.Session,
    instrument_type: str,
    date_from: str,
    date_to: str,
    recaptcha_token: str,
) -> dict | None:
    """POST to the SPA's standardsearch endpoint.

    Requires a valid Google reCAPTCHA v3 token (site key 6LfI8ikaAAAAAH0qlQMApskMGd1U6EqDyniH5t0x).
    Returns the search result envelope {"isValidSearch": bool, "qs": str|null}.
    """
    url = f"{BASE_URL}/api/home/standardsearch"
    params = {
        "partyName": "",
        "dateRangeFrom": date_from,
        "dateRangeTo": date_to,
        "documentType": instrument_type,
        "searchT": instrument_type,
        "firstQuery": "true",
        "searchtype": "documentType",
    }
    headers = {
        "Accept": "application/json",
        "content-type": "application/json; charset=utf-8",
        "x-recaptcha-token": recaptcha_token,
    }
    resp = session.post(url, params=params, headers=headers, data="{}", timeout=30)
    if not resp.ok:
        logger.error("SPA search returned HTTP %s", resp.status_code)
        return None
    return resp.json()


def get_search_results(session: requests.Session, qs: str) -> list[dict]:
    """Fetch the actual result rows using the qs token from a prior search."""
    url = f"{BASE_URL}/api/SearchResults/getStandardRecords"
    resp = session.get(url, params={"qs": qs}, timeout=30)
    if not resp.ok:
        logger.error("getStandardRecords returned HTTP %s", resp.status_code)
        return []
    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # May be wrapped; try common keys
        for key in ("recordingModels", "records", "results", "data"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return []


def get_pdf_image_url(session: requests.Session, cfn_master_id: str) -> str | None:
    """Resolve the PDF download URL for a given cfnMasterID."""
    url = f"{BASE_URL}/api/DocumentImage/getimagepaths"
    resp = session.get(url, params={"cfnMasterID": cfn_master_id}, timeout=20)
    if resp.ok:
        data = resp.json()
        # Shape TBD; return the first URL if found
        if isinstance(data, str):
            return data
        if isinstance(data, dict) and "path" in data:
            return data["path"]
        if isinstance(data, list) and data:
            return data[0]
    return None


def fetch_pdf(session: requests.Session, book: str, page: str, book_type: str) -> bytes | None:
    """Download PDF image bytes via the direct book/page endpoint."""
    url = f"{BASE_URL}/api/DocumentImage/getdocumentimage"
    params = {"sBook": book, "sPage": page, "sBookType": book_type, "redact": "false"}
    resp = session.get(url, params=params, timeout=60)
    if resp.ok and resp.headers.get("content-type", "").startswith("application/pdf"):
        return resp.content
    return None


# ---------------------------------------------------------------------------
# Record → Lead conversion
# ---------------------------------------------------------------------------

def record_to_lead(record: dict, instrument_type: str) -> dict:
    """Convert a raw API record dict to a Lead-shaped JSONL row.

    Field mapping is approximate — adjust once real API response shape is known.
    Common field names in FL recorder systems: grantor, grantee, docNumber,
    book, page, recordingDate, instrumentType, legalDescription, imageUrl.
    """
    # Try multiple possible field name conventions
    grantor = (
        record.get("grantor")
        or record.get("graNTOR_NAME")
        or record.get("grantorName")
        or record.get("partyName")
        or record.get("party1Name")
        or ""
    ).strip()

    doc_number = (
        record.get("docNumber")
        or record.get("cfN_MASTER_ID")
        or record.get("cfnMasterId")
        or record.get("documentNumber")
        or ""
    )

    book = record.get("book") or record.get("cfN_BOOK") or record.get("bookNo") or ""
    page = record.get("page") or record.get("cfN_PAGE") or record.get("pageNo") or ""
    book_type = record.get("bookType") or record.get("cfN_BOOK_TYPE") or "OR"
    recording_date = (
        record.get("recordingDate")
        or record.get("recDate")
        or record.get("cfN_REC_DATE")
        or ""
    )
    legal_desc = record.get("legalDescription") or record.get("legal") or ""

    # Build image URL from book/page
    image_url = (
        f"{BASE_URL}/api/DocumentImage/getdocumentimage"
        f"?sBook={book}&sPage={page}&sBookType={book_type}&redact=false"
    )

    # Use grantor as HOA name; grantor on a Declaration/Restriction is typically the developer
    # or the association itself. May need post-hoc name repair.
    hoa_name = grantor or f"Unknown HOA - {instrument_type} {doc_number}"

    return {
        # Lead fields
        "name": hoa_name,
        "state": "FL",
        "county": "miami-dade",
        "source": "miami-dade-recorder",
        "source_url": image_url,
        "website": None,
        "pre_discovered_pdf_urls": [image_url],
        # Extended metadata (not in Lead dataclass but useful for audit)
        "_recorder_doc_number": doc_number,
        "_recorder_book": book,
        "_recorder_page": page,
        "_recorder_book_type": book_type,
        "_recorder_recording_date": recording_date,
        "_recorder_instrument_type": instrument_type,
        "_recorder_legal_description": legal_desc,
        "_recorder_grantor": grantor,
    }


# ---------------------------------------------------------------------------
# PDF download + save
# ---------------------------------------------------------------------------

def download_and_save_pdf(
    session: requests.Session,
    record: dict,
    pdf_dir: Path,
) -> str | None:
    """Download the PDF for a record and save to pdf_dir. Returns local path or None."""
    doc_number = record.get("_recorder_doc_number") or "unknown"
    book = record.get("_recorder_book") or ""
    page = record.get("_recorder_page") or ""
    book_type = record.get("_recorder_book_type") or "OR"

    if not book or not page:
        logger.warning("No book/page for doc %s; skipping PDF download", doc_number)
        return None

    pdf_bytes = fetch_pdf(session, book, page, book_type)
    if not pdf_bytes:
        logger.warning("PDF fetch returned no bytes for doc %s", doc_number)
        return None

    safe_doc = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(doc_number))
    local_path = pdf_dir / f"{safe_doc}.pdf"
    local_path.write_bytes(pdf_bytes)
    logger.info("Saved PDF to %s (%d bytes)", local_path, len(pdf_bytes))
    return str(local_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Scrape Miami-Dade County Recorder official records for HOA governing instruments.\n\n"
            "BLOCKER: Requires HOA_RECORDER_AUTHKEY (Developer API key) OR a valid reCAPTCHA v3\n"
            "token path. Without one of these, all searches will be rejected by the server.\n\n"
            "To unblock:\n"
            "  Register at https://www2.miamidadeclerk.gov/Developers/Home/MyAccount\n"
            "  Then set HOA_RECORDER_AUTHKEY=<your-key> in settings.env."
        )
    )
    p.add_argument(
        "--instrument-type",
        default="DCO",
        help=(
            "Instrument type code. Common codes: DCO (Declaration of Condominium), "
            "RES (Restrictions/CC&Rs), COV (Covenant), AIN (Articles of Incorporation). "
            "Default: DCO"
        ),
    )
    p.add_argument("--start-date", required=True, help="Start date MM/DD/YYYY (e.g. 04/01/2026)")
    p.add_argument("--end-date", required=True, help="End date MM/DD/YYYY (e.g. 04/30/2026)")
    p.add_argument(
        "--max-records",
        type=int,
        default=20,
        help="Maximum records to fetch (default: 20)",
    )
    p.add_argument(
        "--output",
        help=(
            "Output JSONL path. Default: data/fl_miami_dade_recorder_<timestamp>.jsonl"
        ),
    )
    p.add_argument(
        "--pdf-dir",
        default="data/sunbiz/recorder_pdfs",
        help="Directory to save downloaded PDFs (default: data/sunbiz/recorder_pdfs)",
    )
    p.add_argument(
        "--skip-pdfs",
        action="store_true",
        help="Skip PDF download; emit Lead rows only",
    )
    p.add_argument(
        "--pacing-seconds",
        type=float,
        default=MIN_PACING_SECONDS,
        help=f"Seconds between API calls (default: {MIN_PACING_SECONDS})",
    )
    p.add_argument(
        "--recaptcha-token",
        default="",
        help=(
            "Raw reCAPTCHA v3 token (short-lived, ~2 min). Used only when "
            "HOA_RECORDER_AUTHKEY is not set. Not recommended for automation."
        ),
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path(f"data/fl_miami_dade_recorder_{stamp}.jsonl")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # PDF directory
    pdf_dir = Path(args.pdf_dir)
    if not args.skip_pdfs:
        pdf_dir.mkdir(parents=True, exist_ok=True)

    # Check for AuthKey
    auth_key = os.environ.get("HOA_RECORDER_AUTHKEY", "").strip()
    recaptcha_token = args.recaptcha_token.strip()

    if not auth_key and not recaptcha_token:
        print(
            "\nBLOCKER: No authentication method available.\n\n"
            "The Miami-Dade ORIPS system requires one of:\n"
            "  A) HOA_RECORDER_AUTHKEY env var — set this after registering at:\n"
            "     https://www2.miamidadeclerk.gov/Developers/Home/MyAccount\n"
            "  B) --recaptcha-token <token> — a valid Google reCAPTCHA v3 token\n"
            "     (short-lived ~2 min; not suitable for automation)\n\n"
            "TOS note: Bulk storage of records requires written permission or the\n"
            "Developer API. See fl-discovery-handoff.md for details.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    instrument_type = args.instrument_type.upper()
    if instrument_type not in SUPPORTED_INSTRUMENT_TYPES:
        logger.warning(
            "Instrument type %r not in known list %s; proceeding anyway",
            instrument_type,
            list(SUPPORTED_INSTRUMENT_TYPES.keys()),
        )

    session = _make_session()
    records: list[dict] = []

    # Phase 1: search
    if auth_key:
        logger.info(
            "Searching via Developer API: type=%s date=%s to %s",
            instrument_type,
            args.start_date,
            args.end_date,
        )
        try:
            raw = search_via_dev_api(
                session, auth_key, instrument_type,
                args.start_date, args.end_date, args.max_records,
            )
            records = raw
            logger.info("Developer API returned %d records", len(records))
        except requests.HTTPError as exc:
            logger.error("Developer API error: %s", exc)
            logger.error(
                "Response body: %s",
                exc.response.text[:500] if exc.response else "(none)",
            )
            logger.error(
                "The Developer API endpoint URL or parameter names may differ from "
                "what was inferred from the public UI. Check the Help docs at "
                "https://www2.miamidadeclerk.gov/Developers/Help for the exact "
                "OfficialRecords StandardSearch parameters."
            )
            sys.exit(1)
    else:
        # Attempt via SPA API with user-supplied reCAPTCHA token
        logger.info(
            "Searching via SPA API with reCAPTCHA token (type=%s, date=%s to %s)",
            instrument_type,
            args.start_date,
            args.end_date,
        )
        result = search_via_spa_api(
            session, instrument_type,
            args.start_date, args.end_date, recaptcha_token,
        )
        if not result or not result.get("isValidSearch"):
            logger.error(
                "SPA search returned isValidSearch=false. "
                "The reCAPTCHA token is invalid or expired. "
                "Provide HOA_RECORDER_AUTHKEY for a non-token-based approach."
            )
            sys.exit(1)
        qs = result.get("qs")
        if not qs:
            logger.error("Search succeeded but qs token is null; no results.")
            sys.exit(1)
        time.sleep(args.pacing_seconds)
        records = get_search_results(session, qs)
        logger.info("SPA API returned %d records", len(records))

    if not records:
        logger.warning("No records returned. Output file will be empty.")

    # Phase 2: convert → Lead rows, optionally download PDFs
    lead_rows: list[dict] = []
    for i, record in enumerate(records[: args.max_records]):
        lead = record_to_lead(record, instrument_type)

        if not args.skip_pdfs:
            time.sleep(args.pacing_seconds)
            local_pdf = download_and_save_pdf(session, lead, pdf_dir)
            if local_pdf:
                lead["_local_pdf_path"] = local_pdf

        lead_rows.append(lead)
        logger.info(
            "[%d/%d] %s — doc %s",
            i + 1,
            min(len(records), args.max_records),
            lead["name"],
            lead.get("_recorder_doc_number"),
        )

        if i < len(records) - 1:
            time.sleep(args.pacing_seconds)

    # Phase 3: emit JSONL
    with output_path.open("w") as f:
        for row in lead_rows:
            # Emit only the Lead-compatible fields (strip internal _recorder_* fields
            # when piping to probe-batch, or keep them for audit)
            f.write(json.dumps(row) + "\n")

    logger.info("Wrote %d Lead rows to %s", len(lead_rows), output_path)
    print(f"Output: {output_path}")
    print(f"Records: {len(lead_rows)}")
    print()
    print("To bank via discovery:")
    print(f"  python -m hoaware.discovery probe-batch {output_path}")


if __name__ == "__main__":
    main()
