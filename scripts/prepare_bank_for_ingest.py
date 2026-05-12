#!/usr/bin/env python3
"""Prepare raw banked HOA PDFs for Render-side import.

Reads raw discovery manifests from gs://hoaproxy-bank/v1/{STATE}/..., filters
documents before OCR, extracts page text locally or in a GCP worker, then writes
prepared bundles to gs://hoaproxy-ingest-ready/v1/{STATE}/....
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests.exceptions as _requests_exc
from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage as gcs

from hoaware import db

# Errors we treat as transient and retry on. The google-cloud-storage library
# already retries HTTP 5xx with its built-in policy, but socket-level
# ReadTimeout / ConnectionError leak through verbatim and crash prepare ~every
# 2 hours. Retry those, plus the api_core wrappers, with bounded exponential
# backoff.
_GCS_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    _requests_exc.ConnectionError,
    _requests_exc.ReadTimeout,
    _requests_exc.Timeout,
    _requests_exc.ChunkedEncodingError,
    ConnectionResetError,
    gcs_exceptions.ServiceUnavailable,
    gcs_exceptions.GatewayTimeout,
    gcs_exceptions.InternalServerError,
    gcs_exceptions.TooManyRequests,
    gcs_exceptions.RetryError,
)
_GCS_RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0, 32.0)


def _with_gcs_retry(op_name: str, fn, *args, **kwargs):
    """Call fn(*args, **kwargs); on transient GCS errors, retry up to 5 times
    with exponential backoff (2s, 4s, 8s, 16s, 32s) and re-raise after.

    op_name should identify the call site (e.g. the gs:// URI) so retries are
    forensically traceable.
    """
    last_exc: BaseException | None = None
    for attempt, delay in enumerate([0.0, *_GCS_RETRY_DELAYS]):
        if delay:
            log.warning(
                "gcs_transient_retry op=%s attempt=%d sleep=%.1fs last=%s",
                op_name,
                attempt,
                delay,
                type(last_exc).__name__ if last_exc else "n/a",
            )
            time.sleep(delay)
        try:
            return fn(*args, **kwargs)
        except _GCS_TRANSIENT_ERRORS as exc:
            last_exc = exc
            continue
    assert last_exc is not None
    log.error("gcs_transient_giveup op=%s err=%s", op_name, last_exc)
    raise last_exc
from hoaware.bank import DEFAULT_BUCKET as DEFAULT_BANK_BUCKET
from hoaware.bank import _inspect_pdf, slugify
from hoaware.config import load_settings
from hoaware.cost_tracker import COST_DOCAI_PER_PAGE
from hoaware.doc_classifier import REJECT_JUNK, REJECT_PII, classify_from_text
from hoaware.name_utils import is_dirty as _is_dirty_name
from hoaware.pdf_utils import MAX_PAGES_FOR_OCR, MAX_PAGES_FOR_OCR_SCANNED, extract_pages
from hoaware.prepared_ingest import (
    DEFAULT_PREPARED_BUCKET,
    PREPARED_KEEP_CATEGORIES,
    VERSION_PREFIX,
    bundle_blob_name,
    docs_blob_name,
    gcs_uri,
    prepared_bundle_prefix,
    status_blob_name,
    text_blob_name,
    validate_bundle,
    validate_text_sidecar,
)

LOW_VALUE_CATEGORIES = {"minutes", "financial", "insurance"}
GOVERNING_METADATA_RE = re.compile(
    r"\b("
    r"declarations?|covenants?|restrictions?|cc&?rs?|c\.c\.&r\.s?|"
    r"bylaws?|by-laws?|articles?(?:\s+of\s+(?:incorporation|organization))?|"
    r"rules?(?:\s+(?:&|and)\s+regulations?)?|regulations?|guidelines?|"
    r"resolutions?|amendments?|policies|policy|"
    r"plat|subdivision\s+plat|final\s+plat|recorded\s+plat"
    r")\b",
    re.IGNORECASE,
)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSM_POLYGON_TYPES = {
    "neighbourhood",
    "residential",
    "subdivision",
    "suburb",
    "quarter",
    "village",
    "hamlet",
    "locality",
}


def _json_dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def _append_ledger(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _manifest_uri(bucket_name: str, blob_name: str) -> str:
    return f"gs://{bucket_name}/{blob_name}"


def _manifest_parts(blob_name: str) -> tuple[str, str, str]:
    parts = blob_name.split("/")
    if len(parts) < 5 or parts[0] != VERSION_PREFIX or parts[-1] != "manifest.json":
        raise ValueError(f"unexpected manifest path: {blob_name}")
    return parts[1], parts[2], parts[3]


def _load_json_blob(bucket, blob_name: str) -> dict[str, Any]:
    uri = f"gs://{bucket.name}/{blob_name}"
    payload = _with_gcs_retry(
        f"download_as_bytes:{uri}",
        bucket.blob(blob_name).download_as_bytes,
    )
    return json.loads(payload)


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _document_category(doc: dict[str, Any], precheck: dict[str, Any]) -> str | None:
    for key in ("category_hint", "suggested_category"):
        value = doc.get(key) or precheck.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _has_governing_metadata_signal(
    *,
    doc: dict[str, Any],
    filename: str,
    source_url: str | None,
    hoa_name: str,
) -> bool:
    fields = [
        filename,
        source_url or "",
        hoa_name,
        str(doc.get("title") or ""),
        str(doc.get("link_text") or ""),
    ]
    return any(GOVERNING_METADATA_RE.search(value) for value in fields if value)


def _compact_hoa_name(name: str) -> str:
    tokens = [
        "homeowners association",
        "homeowners",
        "home owners association",
        "homes association",
        "property owners association",
        "owners association",
        "community association",
        "condominium association",
        "association",
        "hoa",
        "inc.",
        "inc",
    ]
    cleaned = " ".join(str(name or "").replace("&", " and ").split())
    lowered = cleaned.lower()
    for token in tokens:
        lowered = lowered.replace(token, " ")
    return " ".join(lowered.split()).title()


def _geo_query_candidates(
    *,
    hoa_name: str,
    address: dict[str, Any],
    state: str,
    county_slug: str,
) -> list[str]:
    city = str(address.get("city") or "").strip()
    county = str(address.get("county") or county_slug.replace("-", " ")).strip()
    full = " ".join(str(hoa_name or "").split())
    compact = _compact_hoa_name(full)
    bases = [full]
    if compact and compact.casefold() != full.casefold():
        bases.append(compact)

    queries: list[str] = []
    for base in bases:
        if city:
            queries.append(f"{base}, {city}, {state}")
        if county:
            queries.append(f"{base}, {county} County, {state}")
        queries.append(f"{base}, {state}")

    out: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = query.casefold()
        if key not in seen:
            seen.add(key)
            out.append(query)
    return out[:6]


def _nominatim_result_score(result: dict[str, Any]) -> int:
    score = 0
    geojson = result.get("geojson")
    geo_type = geojson.get("type") if isinstance(geojson, dict) else None
    if geo_type in {"Polygon", "MultiPolygon"}:
        score += 100
    elif geo_type:
        score += 20
    result_type = str(result.get("type") or "").lower()
    result_class = str(result.get("class") or "").lower()
    if result_type in OSM_POLYGON_TYPES:
        score += 35
    if result_class == "place":
        score += 20
    if result_class == "boundary" and result_type == "administrative":
        score -= 50
    try:
        score += min(15, int(float(result.get("importance") or 0) * 20))
    except Exception:
        pass
    return score


def _select_nominatim_geometry(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked = sorted(results, key=_nominatim_result_score, reverse=True)
    for result in ranked:
        geojson = result.get("geojson")
        if not isinstance(geojson, dict) or geojson.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        result_type = str(result.get("type") or "").lower()
        result_class = str(result.get("class") or "").lower()
        if result_class == "boundary" and result_type == "administrative":
            continue
        if result_type not in OSM_POLYGON_TYPES and result_class != "place":
            continue
        lat = result.get("lat")
        lon = result.get("lon")
        return {
            "boundary_geojson": geojson,
            "latitude": float(lat) if lat is not None else None,
            "longitude": float(lon) if lon is not None else None,
            "location_quality": "polygon",
            "geography_source": "nominatim",
            "geography_confidence": "medium",
            "geography_display_name": result.get("display_name"),
            "osm_id": result.get("osm_id"),
            "osm_type": result.get("osm_type"),
            "osm_class": result.get("class"),
            "osm_place_type": result.get("type"),
        }
    return None


def _enrich_geometry_from_nominatim(
    *,
    hoa_name: str,
    address: dict[str, Any],
    geometry: dict[str, Any],
    state: str,
    county_slug: str,
    cache: dict[str, Any],
    user_agent: str,
    delay_s: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if geometry.get("boundary_geojson") or (
        geometry.get("latitude") is not None and geometry.get("longitude") is not None
    ):
        return geometry, None

    import requests

    for query in _geo_query_candidates(
        hoa_name=hoa_name,
        address=address,
        state=state,
        county_slug=county_slug,
    ):
        cache_key = f"nominatim:v1:{query}"
        if cache_key in cache:
            results = cache[cache_key]
        else:
            time.sleep(max(0.0, delay_s))
            resp = requests.get(
                NOMINATIM_URL,
                params={
                    "q": query,
                    "format": "jsonv2",
                    "polygon_geojson": 1,
                    "addressdetails": 1,
                    "limit": 5,
                    "countrycodes": "us",
                },
                headers={"User-Agent": user_agent},
                timeout=30,
            )
            resp.raise_for_status()
            results = resp.json()
            cache[cache_key] = results
        if not isinstance(results, list):
            continue
        enriched = _select_nominatim_geometry(results)
        if enriched:
            return {**geometry, **enriched, "geography_query": query}, {
                "query": query,
                "display_name": enriched.get("geography_display_name"),
                "osm_id": enriched.get("osm_id"),
                "source": "nominatim",
            }
    return geometry, None


def _live_checksums() -> set[str]:
    settings = load_settings()
    if not settings.db_path.exists():
        return set()
    try:
        with db.get_connection(settings.db_path) as conn:
            rows = conn.execute("SELECT checksum FROM documents WHERE checksum IS NOT NULL").fetchall()
    except Exception:
        return set()
    return {str(row["checksum"]) for row in rows}


def _iter_blobs_with_retry(bucket, prefix: str):
    """Yield blobs under prefix, retrying transient errors page-by-page.

    google.cloud.storage's list_blobs returns a lazy iterator that fetches one
    HTTP page at a time; any of those page fetches can raise the same
    socket-level errors as upload. Wrap each .__next__() call in our retry
    helper so a transient hiccup does not crash the whole listing.
    """
    op = f"list_blobs:gs://{bucket.name}/{prefix}"
    it = iter(_with_gcs_retry(op, bucket.list_blobs, prefix=prefix))
    while True:
        try:
            blob = _with_gcs_retry(op, next, it)
        except StopIteration:
            return
        yield blob


def _prepared_shas(bucket, state: str) -> set[str]:
    out: set[str] = set()
    prefix = f"{VERSION_PREFIX}/{state}/"
    for blob in _iter_blobs_with_retry(bucket, prefix):
        name = blob.name
        if "/docs/" not in name or not name.endswith(".pdf"):
            continue
        sha = Path(name).stem
        if len(sha) == 64:
            out.add(sha)
    return out


def _reject_reason(
    *,
    category: str | None,
    precheck: dict[str, Any],
    include_low_value: bool,
    live_shas: set[str],
    prepared_shas: set[str],
    sha256: str,
    hard_only: bool = False,
    allow_junk_review: bool = False,
) -> str | None:
    if sha256 in live_shas:
        return "duplicate:live"
    if sha256 in prepared_shas:
        return "duplicate:prepared"
    if category in REJECT_PII:
        return f"pii:{category}"
    if category in REJECT_JUNK:
        if hard_only and allow_junk_review:
            return None
        return f"junk:{category}"
    page_count = precheck.get("page_count")
    if isinstance(page_count, int) and page_count > MAX_PAGES_FOR_OCR:
        return f"page_cap:{page_count}"
    # Tighter cap for scanned PDFs (text_extractable=False) — full-document
    # DocAI on >25 pages is almost always a misclassified bulk archive or a
    # gov records dump, not a real governing doc. Text-extractable PDFs are
    # not capped here (PyPDF cost is zero).
    if (
        isinstance(page_count, int)
        and page_count > MAX_PAGES_FOR_OCR_SCANNED
        and precheck.get("text_extractable") is False
    ):
        return f"page_cap_scanned:{page_count}"
    if hard_only:
        return None
    if category in LOW_VALUE_CATEGORIES and not include_low_value:
        return f"low_value:{category}"
    if category not in PREPARED_KEEP_CATEGORIES and not (
        include_low_value and category in LOW_VALUE_CATEGORIES
    ):
        return f"unsupported_category:{category or 'unknown'}"
    return None


def _projected_docai_pages(precheck: dict[str, Any]) -> int:
    value = precheck.get("est_docai_pages")
    if isinstance(value, int):
        return max(0, value)
    try:
        return max(0, int(value or 0))
    except Exception:
        pass
    if precheck.get("text_extractable") is False:
        try:
            return max(0, int(precheck.get("page_count") or 0))
        except Exception:
            return 0
    return 0


def _extract_first_page_review_text(
    *,
    pdf_bytes: bytes,
    text_extractable: bool | None,
) -> tuple[str, int]:
    """Return first-page text for relevance review, OCRing only page 1 if needed."""
    settings = load_settings()
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()
        path = Path(tmp.name)

        if text_extractable is not False:
            try:
                from pypdf import PdfReader

                reader = PdfReader(str(path))
                if reader.pages:
                    text = reader.pages[0].extract_text() or ""
                    if text.strip():
                        return text, 0
            except Exception:
                pass

        if not (settings.enable_docai and settings.docai_project_id and settings.docai_processor_id):
            return "", 0

        from hoaware.docai import extract_with_document_ai

        pages = extract_with_document_ai(
            path,
            project_id=settings.docai_project_id,
            location=settings.docai_location,
            processor_id=settings.docai_processor_id,
            endpoint=settings.docai_endpoint,
            max_pages_per_call=1,
            page_numbers=[1],
        )
    return "\n".join(page.text for page in pages if page.text.strip()), len(pages)


def _extract_sidecar(
    *,
    pdf_bytes: bytes,
    sha256: str,
    text_extractable: bool | None,
) -> tuple[dict[str, Any], int]:
    settings = load_settings()
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()
        pages = extract_pages(
            Path(tmp.name),
            text_extractable=text_extractable,
            enable_docai=settings.enable_docai,
            docai_project_id=settings.docai_project_id,
            docai_location=settings.docai_location,
            docai_processor_id=settings.docai_processor_id,
            docai_endpoint=settings.docai_endpoint,
            docai_chunk_pages=settings.docai_chunk_pages,
        )
    docai_pages = len(pages) if text_extractable is False else 0
    payload = {
        "pages": [{"number": p.number, "text": p.text} for p in pages],
        "docai_pages": docai_pages,
    }
    validate_text_sidecar(payload)
    return payload, docai_pages


def _write_prepared_bundle(
    *,
    prepared_bucket,
    prefix: str,
    bundle_payload: dict[str, Any],
    pdfs_by_sha: dict[str, bytes],
    sidecars_by_sha: dict[str, dict[str, Any]],
    overwrite: bool,
) -> bool:
    """Write a prepared bundle. Returns True on write, False if skipped
    because the bundle already exists and overwrite=False.
    """
    status_blob = prepared_bucket.blob(status_blob_name(prefix))
    status_uri = f"gs://{prepared_bucket.name}/{status_blob_name(prefix)}"
    if _with_gcs_retry(f"exists:{status_uri}", status_blob.exists) and not overwrite:
        return False

    for sha, pdf_bytes in pdfs_by_sha.items():
        docs_name = docs_blob_name(prefix, sha)
        text_name = text_blob_name(prefix, sha)
        _with_gcs_retry(
            f"upload_pdf:gs://{prepared_bucket.name}/{docs_name}",
            prepared_bucket.blob(docs_name).upload_from_string,
            pdf_bytes,
            content_type="application/pdf",
        )
        _with_gcs_retry(
            f"upload_text:gs://{prepared_bucket.name}/{text_name}",
            prepared_bucket.blob(text_name).upload_from_string,
            _json_dump(sidecars_by_sha[sha]),
            content_type="application/json",
        )

    validate_bundle(bundle_payload, expected_state=bundle_payload["state"])
    bundle_name = bundle_blob_name(prefix)
    _with_gcs_retry(
        f"upload_bundle:gs://{prepared_bucket.name}/{bundle_name}",
        prepared_bucket.blob(bundle_name).upload_from_string,
        _json_dump(bundle_payload),
        content_type="application/json",
    )
    status_payload = {
        "status": "ready",
        "claimed_by": None,
        "claimed_at": None,
        "imported_at": None,
        "error": None,
    }
    kwargs = {} if overwrite else {"if_generation_match": 0}
    _with_gcs_retry(
        f"upload_status:{status_uri}",
        prepared_bucket.blob(status_blob_name(prefix)).upload_from_string,
        _json_dump(status_payload),
        content_type="application/json",
        **kwargs,
    )
    return True


def prepare(args: argparse.Namespace) -> int:
    state = args.state.strip().upper()
    client = gcs.Client()
    bank_bucket = client.bucket(args.bank_bucket)
    prepared_bucket = client.bucket(args.prepared_bucket)
    live_shas = _live_checksums() if not args.skip_live_duplicate_check else set()
    already_prepared = _prepared_shas(prepared_bucket, state)
    geo_cache: dict[str, Any] = {}
    if not args.skip_geo_enrichment:
        geo_cache = _load_json_file(args.geo_cache)

    manifest_prefix = f"{VERSION_PREFIX}/{state}/"
    if args.county:
        manifest_prefix += f"{slugify(args.county)}/"

    processed = 0
    written = 0
    total_projected_docai_cost = 0.0
    for blob in _iter_blobs_with_retry(bank_bucket, manifest_prefix):
        if processed >= args.limit:
            break
        if not blob.name.endswith("/manifest.json"):
            continue
        processed += 1
        manifest_uri = _manifest_uri(args.bank_bucket, blob.name)
        try:
            _, county_slug, hoa_slug = _manifest_parts(blob.name)
            manifest = _load_json_blob(bank_bucket, blob.name)
        except Exception as exc:
            _append_ledger(args.ledger, {
                "manifest_uri": manifest_uri,
                "decision": "manifest_error",
                "error": str(exc),
            })
            continue

        hoa_name = manifest.get("name") or hoa_slug
        _name_dirty, _name_dirty_reason = _is_dirty_name(hoa_name)
        if _name_dirty:
            # Log to stderr and ledger — do NOT block import.  The post-import
            # name cleanup pass (state_scrapers/ga/scripts/clean_dirty_hoa_names.py)
            # is the canonical resolver.
            log.warning("dirty hoa_name name=%r reason=%s", hoa_name, _name_dirty_reason)
            _append_ledger(args.ledger, {
                "manifest_uri": manifest_uri,
                "event": "dirty_name_warn",
                "hoa_name": hoa_name,
                "reason": _name_dirty_reason,
            })
        address = manifest.get("address") if isinstance(manifest.get("address"), dict) else {}
        geometry = manifest.get("geometry") if isinstance(manifest.get("geometry"), dict) else {}
        website = manifest.get("website") if isinstance(manifest.get("website"), dict) else {}
        docs = manifest.get("documents") if isinstance(manifest.get("documents"), list) else []
        address_state = str(address.get("state") or "").strip().upper()
        if address_state and address_state != state:
            _append_ledger(args.ledger, {
                "manifest_uri": manifest_uri,
                "decision": "manifest_rejected",
                "reason": f"state_mismatch:{address_state}",
            })
            continue
        geo_match = None
        if not args.skip_geo_enrichment:
            try:
                geometry, geo_match = _enrich_geometry_from_nominatim(
                    hoa_name=hoa_name,
                    address=address,
                    geometry=geometry,
                    state=state,
                    county_slug=county_slug,
                    cache=geo_cache,
                    user_agent=args.nominatim_user_agent,
                    delay_s=args.nominatim_delay_s,
                )
                _write_json_file(args.geo_cache, geo_cache)
            except Exception as exc:
                _append_ledger(args.ledger, {
                    "manifest_uri": manifest_uri,
                    "decision": "geo_enrichment_error",
                    "error": f"{type(exc).__name__}: {exc}",
                })

        prepared_docs: list[dict[str, Any]] = []
        rejected_docs: list[dict[str, Any]] = []
        pdfs_by_sha: dict[str, bytes] = {}
        sidecars_by_sha: dict[str, dict[str, Any]] = {}

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            sha256 = str(doc.get("sha256") or "").lower()
            filename = doc.get("filename") or f"{sha256}.pdf"
            if not str(filename).lower().endswith(".pdf"):
                filename = f"{Path(str(filename)).name or sha256[:12]}.pdf"
            source_url = doc.get("source_url")
            doc_gcs_path = doc.get("gcs_path")
            precheck_blob = None
            if not doc_gcs_path or not sha256:
                rejected_docs.append({"sha256": sha256, "source_url": source_url, "reason": "missing_gcs_path_or_sha"})
                continue

            try:
                raw_uri = doc_gcs_path.removeprefix(f"gs://{args.bank_bucket}/")
                precheck_blob = str(Path(raw_uri).parent / "precheck.json")
                try:
                    precheck = _load_json_blob(bank_bucket, precheck_blob)
                except Exception:
                    precheck = {}

                pdf_bytes = _with_gcs_retry(
                    f"download_pdf:gs://{bank_bucket.name}/{raw_uri}",
                    bank_bucket.blob(raw_uri).download_as_bytes,
                )
                actual_sha = hashlib.sha256(pdf_bytes).hexdigest()
                if actual_sha != sha256:
                    raise RuntimeError(f"sha mismatch: expected {sha256}, got {actual_sha}")
                if not precheck or precheck.get("ok") is False or precheck.get("suggested_category") is None:
                    precheck = _inspect_pdf(pdf_bytes, filename, hoa_name)

                category = _document_category(doc, precheck)
                text_extractable = precheck.get("text_extractable")
                sidecar: dict[str, Any] | None = None
                docai_pages: int | None = None
                page_one_reviewed = False
                review_docai_pages = 0
                allow_junk_review = text_extractable is True or _has_governing_metadata_signal(
                    doc=doc,
                    filename=filename,
                    source_url=source_url,
                    hoa_name=hoa_name,
                )
                reason = _reject_reason(
                    category=category,
                    precheck=precheck,
                    include_low_value=args.include_low_value,
                    live_shas=live_shas,
                    prepared_shas=already_prepared,
                    sha256=sha256,
                    hard_only=True,
                    allow_junk_review=allow_junk_review,
                )
                skip_page_one_review = args.skip_page_one_review or args.skip_unknown_ocr_review
                if not reason and not skip_page_one_review:
                    projected_review_cost = 0.0 if text_extractable is True else COST_DOCAI_PER_PAGE
                    if total_projected_docai_cost + projected_review_cost > args.max_docai_cost_usd:
                        reason = "docai_budget"
                    else:
                        page_one_reviewed = True
                        review_text, review_docai_pages = _extract_first_page_review_text(
                            pdf_bytes=pdf_bytes,
                            text_extractable=text_extractable,
                        )
                        total_projected_docai_cost += review_docai_pages * COST_DOCAI_PER_PAGE
                        clf = classify_from_text(review_text, hoa_name) if review_text.strip() else None
                        if clf:
                            category = str(clf["category"]).strip().lower()
                            precheck["suggested_category"] = category
                            precheck["classification_method"] = f"first_page_review:{clf.get('method') or 'unknown'}"
                            precheck["classification_confidence"] = clf.get("confidence")
                        reason = _reject_reason(
                            category=category,
                            precheck=precheck,
                            include_low_value=args.include_low_value,
                            live_shas=live_shas,
                            prepared_shas=already_prepared,
                            sha256=sha256,
                        )
                elif not reason:
                    reason = _reject_reason(
                        category=category,
                        precheck=precheck,
                        include_low_value=args.include_low_value,
                        live_shas=live_shas,
                        prepared_shas=already_prepared,
                        sha256=sha256,
                    )
                if reason:
                    rejected_docs.append({"sha256": sha256, "source_url": source_url, "reason": reason})
                    _append_ledger(args.ledger, {
                        "manifest_uri": manifest_uri,
                        "sha256": sha256,
                        "category": category,
                        "decision": "rejected",
                        "reason": reason,
                        "page_one_reviewed": page_one_reviewed,
                        "docai_pages": docai_pages or 0,
                        "review_docai_pages": review_docai_pages,
                    })
                    continue

                if sidecar is None or docai_pages is None:
                    projected_docai_pages = _projected_docai_pages(precheck)
                    projected_cost = projected_docai_pages * COST_DOCAI_PER_PAGE
                    if total_projected_docai_cost + projected_cost > args.max_docai_cost_usd:
                        reason = "docai_budget"
                        rejected_docs.append({"sha256": sha256, "source_url": source_url, "reason": reason})
                        _append_ledger(args.ledger, {
                            "manifest_uri": manifest_uri,
                            "sha256": sha256,
                            "category": category,
                            "decision": "rejected",
                            "reason": reason,
                            "projected_docai_cost_usd": projected_cost,
                        })
                        continue

                    sidecar, docai_pages = _extract_sidecar(
                        pdf_bytes=pdf_bytes,
                        sha256=sha256,
                        text_extractable=text_extractable,
                    )
                    total_projected_docai_cost += docai_pages * COST_DOCAI_PER_PAGE
                pdfs_by_sha[sha256] = pdf_bytes
                sidecars_by_sha[sha256] = sidecar
                already_prepared.add(sha256)
                prepared_docs.append({
                    "sha256": sha256,
                    "filename": filename,
                    "pdf_gcs_path": "",
                    "text_gcs_path": "",
                    "source_url": source_url,
                    "category": category,
                    "text_extractable": text_extractable,
                    "page_count": precheck.get("page_count") or len(sidecar["pages"]),
                    "docai_pages": docai_pages,
                    "filter_reason": "page_one_review_valid_governing_doc"
                    if page_one_reviewed
                    else "valid_governing_doc",
                })
                _append_ledger(args.ledger, {
                    "manifest_uri": manifest_uri,
                    "sha256": sha256,
                    "category": category,
                    "decision": "prepared",
                    "text_extractable": text_extractable,
                    "page_count": precheck.get("page_count") or len(sidecar["pages"]),
                    "docai_pages": docai_pages,
                    "prepared_bucket": args.prepared_bucket,
                    "geo_enriched": bool(geo_match),
                    "page_one_reviewed": page_one_reviewed,
                    "review_docai_pages": review_docai_pages,
                })
            except Exception as exc:
                rejected_docs.append({"sha256": sha256, "source_url": source_url, "reason": f"error:{type(exc).__name__}"})
                _append_ledger(args.ledger, {
                    "manifest_uri": manifest_uri,
                    "sha256": sha256,
                    "decision": "error",
                    "precheck_blob": precheck_blob,
                    "error": str(exc),
                })

        if not prepared_docs:
            continue

        bundle_seed = "|".join([manifest_uri, *sorted(pdfs_by_sha)])
        bundle_id = hashlib.sha256(bundle_seed.encode("utf-8")).hexdigest()[:16]
        prefix = prepared_bundle_prefix(
            state=state,
            county_slug=county_slug,
            hoa_slug=hoa_slug,
            bundle_id=bundle_id,
        )
        for doc in prepared_docs:
            sha = doc["sha256"]
            doc["pdf_gcs_path"] = gcs_uri(args.prepared_bucket, docs_blob_name(prefix, sha))
            doc["text_gcs_path"] = gcs_uri(args.prepared_bucket, text_blob_name(prefix, sha))

        bundle_payload = {
            "schema_version": 1,
            "bundle_id": bundle_id,
            "source_manifest_uri": manifest_uri,
            "state": state,
            "county": address.get("county") or county_slug,
            "hoa_name": hoa_name,
            "metadata_type": manifest.get("metadata_type") or "hoa",
            "website_url": website.get("url"),
            "address": address,
            "geometry": geometry,
            "geography_match": geo_match,
            "documents": prepared_docs,
            "rejected_documents": rejected_docs,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        validate_bundle(bundle_payload, expected_state=state)
        if args.dry_run:
            print(_json_dump({"dry_run": True, "prepared_prefix": prefix, "bundle": bundle_payload}))
        else:
            try:
                wrote = _write_prepared_bundle(
                    prepared_bucket=prepared_bucket,
                    prefix=prefix,
                    bundle_payload=bundle_payload,
                    pdfs_by_sha=pdfs_by_sha,
                    sidecars_by_sha=sidecars_by_sha,
                    overwrite=args.overwrite,
                )
                if wrote:
                    written += 1
                    print(f"wrote gs://{args.prepared_bucket}/{prefix}")
                else:
                    print(f"skipped existing gs://{args.prepared_bucket}/{prefix}", file=sys.stderr)
            except gcs_exceptions.PreconditionFailed:
                print(f"skipped existing gs://{args.prepared_bucket}/{prefix}", file=sys.stderr)

    print(json.dumps({"manifests_seen": processed, "bundles_written": written}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, help="Two-letter state abbreviation, e.g. KS")
    parser.add_argument("--county", help="Optional county filter")
    parser.add_argument("--limit", type=int, default=25, help="Maximum bank manifests to inspect")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print bundles without writing GCS")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite an existing prepared bundle")
    parser.add_argument("--include-low-value", action="store_true", help="Include minutes/financial/insurance docs")
    parser.add_argument(
        "--skip-page-one-review",
        action="store_true",
        help="Skip first-page text/OCR review before final low-value or unsupported-category rejection",
    )
    parser.add_argument(
        "--skip-unknown-ocr-review",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--skip-live-duplicate-check", action="store_true")
    parser.add_argument(
        "--skip-geo-enrichment",
        action="store_true",
        help="Do not query Nominatim/OSM for missing HOA boundary polygons before writing bundles",
    )
    parser.add_argument(
        "--geo-cache",
        type=Path,
        default=Path("data/prepared_ingest_geo_cache.json"),
        help="Local cache for Nominatim/OSM lookup responses",
    )
    parser.add_argument(
        "--nominatim-delay-s",
        type=float,
        default=1.1,
        help="Delay between uncached Nominatim requests",
    )
    parser.add_argument(
        "--nominatim-user-agent",
        default=os.environ.get("HOAPROXY_NOMINATIM_USER_AGENT", "HOAproxy prepared-ingest/1.0 (admin@hoaproxy.org)"),
        help="User-Agent sent to Nominatim",
    )
    parser.add_argument("--max-docai-cost-usd", type=float, default=10.0)
    parser.add_argument("--bank-bucket", default=os.environ.get("HOA_BANK_GCS_BUCKET", DEFAULT_BANK_BUCKET))
    parser.add_argument("--prepared-bucket", default=os.environ.get("HOA_PREPARED_GCS_BUCKET", DEFAULT_PREPARED_BUCKET))
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path("data/prepared_ingest_ledger.jsonl"),
        help="Local JSONL audit ledger path",
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    if args.max_docai_cost_usd < 0:
        parser.error("--max-docai-cost-usd must be non-negative")
    if args.nominatim_delay_s < 0:
        parser.error("--nominatim-delay-s must be non-negative")
    return prepare(args)


if __name__ == "__main__":
    raise SystemExit(main())
