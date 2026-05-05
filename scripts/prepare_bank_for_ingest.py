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
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage as gcs

from hoaware import db
from hoaware.bank import DEFAULT_BUCKET as DEFAULT_BANK_BUCKET
from hoaware.bank import _inspect_pdf, slugify
from hoaware.config import load_settings
from hoaware.cost_tracker import COST_DOCAI_PER_PAGE
from hoaware.doc_classifier import REJECT_JUNK, REJECT_PII
from hoaware.pdf_utils import MAX_PAGES_FOR_OCR, extract_pages
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
    return json.loads(bucket.blob(blob_name).download_as_bytes())


def _document_category(doc: dict[str, Any], precheck: dict[str, Any]) -> str | None:
    for key in ("category_hint", "suggested_category"):
        value = doc.get(key) or precheck.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


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


def _prepared_shas(bucket, state: str) -> set[str]:
    out: set[str] = set()
    prefix = f"{VERSION_PREFIX}/{state}/"
    for blob in bucket.list_blobs(prefix=prefix):
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
) -> str | None:
    if sha256 in live_shas:
        return "duplicate:live"
    if sha256 in prepared_shas:
        return "duplicate:prepared"
    if category in REJECT_PII:
        return f"pii:{category}"
    if category in REJECT_JUNK:
        return f"junk:{category}"
    if category in LOW_VALUE_CATEGORIES and not include_low_value:
        return f"low_value:{category}"
    if category not in PREPARED_KEEP_CATEGORIES and not (
        include_low_value and category in LOW_VALUE_CATEGORIES
    ):
        return f"unsupported_category:{category or 'unknown'}"
    page_count = precheck.get("page_count")
    if isinstance(page_count, int) and page_count > MAX_PAGES_FOR_OCR:
        return f"page_cap:{page_count}"
    return None


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
) -> None:
    status_blob = prepared_bucket.blob(status_blob_name(prefix))
    if status_blob.exists() and not overwrite:
        raise RuntimeError(f"prepared bundle already exists: gs://{prepared_bucket.name}/{prefix}")

    for sha, pdf_bytes in pdfs_by_sha.items():
        prepared_bucket.blob(docs_blob_name(prefix, sha)).upload_from_string(
            pdf_bytes,
            content_type="application/pdf",
        )
        prepared_bucket.blob(text_blob_name(prefix, sha)).upload_from_string(
            _json_dump(sidecars_by_sha[sha]),
            content_type="application/json",
        )

    validate_bundle(bundle_payload, expected_state=bundle_payload["state"])
    prepared_bucket.blob(bundle_blob_name(prefix)).upload_from_string(
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
    prepared_bucket.blob(status_blob_name(prefix)).upload_from_string(
        _json_dump(status_payload),
        content_type="application/json",
        **kwargs,
    )


def prepare(args: argparse.Namespace) -> int:
    state = args.state.strip().upper()
    client = gcs.Client()
    bank_bucket = client.bucket(args.bank_bucket)
    prepared_bucket = client.bucket(args.prepared_bucket)
    live_shas = _live_checksums() if not args.skip_live_duplicate_check else set()
    already_prepared = _prepared_shas(prepared_bucket, state)

    manifest_prefix = f"{VERSION_PREFIX}/{state}/"
    if args.county:
        manifest_prefix += f"{slugify(args.county)}/"

    processed = 0
    written = 0
    total_projected_docai_cost = 0.0
    for blob in bank_bucket.list_blobs(prefix=manifest_prefix):
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

        prepared_docs: list[dict[str, Any]] = []
        rejected_docs: list[dict[str, Any]] = []
        pdfs_by_sha: dict[str, bytes] = {}
        sidecars_by_sha: dict[str, dict[str, Any]] = {}

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            sha256 = str(doc.get("sha256") or "").lower()
            filename = doc.get("filename") or f"{sha256}.pdf"
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

                pdf_bytes = bank_bucket.blob(raw_uri).download_as_bytes()
                actual_sha = hashlib.sha256(pdf_bytes).hexdigest()
                if actual_sha != sha256:
                    raise RuntimeError(f"sha mismatch: expected {sha256}, got {actual_sha}")
                if not precheck or precheck.get("ok") is False or precheck.get("suggested_category") is None:
                    precheck = _inspect_pdf(pdf_bytes, filename, hoa_name)

                category = _document_category(doc, precheck)
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
                    })
                    continue

                text_extractable = precheck.get("text_extractable")
                projected_docai_pages = int(precheck.get("est_docai_pages") or 0)
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
                    "filter_reason": "valid_governing_doc",
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
            "documents": prepared_docs,
            "rejected_documents": rejected_docs,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        validate_bundle(bundle_payload, expected_state=state)
        if args.dry_run:
            print(_json_dump({"dry_run": True, "prepared_prefix": prefix, "bundle": bundle_payload}))
        else:
            try:
                _write_prepared_bundle(
                    prepared_bucket=prepared_bucket,
                    prefix=prefix,
                    bundle_payload=bundle_payload,
                    pdfs_by_sha=pdfs_by_sha,
                    sidecars_by_sha=sidecars_by_sha,
                    overwrite=args.overwrite,
                )
                written += 1
                print(f"wrote gs://{args.prepared_bucket}/{prefix}")
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
    parser.add_argument("--skip-live-duplicate-check", action="store_true")
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
    return prepare(args)


if __name__ == "__main__":
    raise SystemExit(main())
