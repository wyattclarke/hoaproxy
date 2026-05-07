"""Prepared GCS ingest queue helpers.

Prepared bundles are produced outside Render after filtering and OCR. Render
only imports bundles that already contain extracted page-text sidecars.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage as gcs

from .chunker import PageContent
from .doc_classifier import REJECT_PII

SCHEMA_VERSION = 1
VERSION_PREFIX = "v1"
DEFAULT_PREPARED_BUCKET = os.environ.get(
    "HOA_PREPARED_GCS_BUCKET", "hoaproxy-ingest-ready"
)
DEFAULT_BANK_BUCKET = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")

PREPARED_KEEP_CATEGORIES = {
    "ccr",
    "bylaws",
    "articles",
    "rules",
    "amendment",
    "resolution",
    "plat",
}
PREPARED_OPTIONAL_CATEGORIES = {"minutes", "financial", "insurance"}
PREPARED_ALLOWED_CATEGORIES = PREPARED_KEEP_CATEGORIES | PREPARED_OPTIONAL_CATEGORIES
PREPARED_STATUSES = {"ready", "claimed", "imported", "failed", "skipped"}


class PreparedIngestError(ValueError):
    """Raised when a prepared bundle cannot be safely imported."""


@dataclass(frozen=True)
class GcsUri:
    bucket: str
    blob: str

    def __str__(self) -> str:
        return f"gs://{self.bucket}/{self.blob}"


@dataclass(frozen=True)
class PreparedDocument:
    sha256: str
    filename: str
    pdf_gcs_path: str
    text_gcs_path: str
    source_url: str | None
    category: str
    text_extractable: bool | None
    page_count: int | None
    docai_pages: int
    filter_reason: str | None


@dataclass(frozen=True)
class PreparedBundle:
    schema_version: int
    bundle_id: str
    source_manifest_uri: str
    state: str
    county: str | None
    hoa_name: str
    metadata_type: str | None
    website_url: str | None
    address: dict[str, Any]
    geometry: dict[str, Any]
    documents: list[PreparedDocument]
    rejected_documents: list[dict[str, Any]]
    created_at: str | None
    raw: dict[str, Any]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_claimant() -> str:
    return f"render-importer:{socket.gethostname()}"


def parse_gcs_uri(uri: str) -> GcsUri:
    if not isinstance(uri, str) or not uri.startswith("gs://"):
        raise PreparedIngestError(f"Expected gs:// URI, got {uri!r}")
    parsed = urlparse(uri)
    bucket = parsed.netloc
    blob = parsed.path.lstrip("/")
    if not bucket or not blob:
        raise PreparedIngestError(f"Invalid GCS URI: {uri!r}")
    return GcsUri(bucket=bucket, blob=blob)


def gcs_uri(bucket: str, blob: str) -> str:
    blob = blob.lstrip("/")
    return f"gs://{bucket}/{blob}"


def prepared_bundle_prefix(
    *, state: str, county_slug: str, hoa_slug: str, bundle_id: str
) -> str:
    state_n = _normalize_state(state)
    if not county_slug or not hoa_slug or not bundle_id:
        raise PreparedIngestError("state, county_slug, hoa_slug, and bundle_id are required")
    return f"{VERSION_PREFIX}/{state_n}/{county_slug.strip('/')}/{hoa_slug.strip('/')}/{bundle_id.strip('/')}"


def status_blob_name(bundle_prefix: str) -> str:
    return f"{bundle_prefix.rstrip('/')}/status.json"


def bundle_blob_name(bundle_prefix: str) -> str:
    return f"{bundle_prefix.rstrip('/')}/bundle.json"


def docs_blob_name(bundle_prefix: str, sha256: str) -> str:
    return f"{bundle_prefix.rstrip('/')}/docs/{sha256}.pdf"


def text_blob_name(bundle_prefix: str, sha256: str) -> str:
    return f"{bundle_prefix.rstrip('/')}/texts/{sha256}.json"


def _normalize_state(state: str) -> str:
    state_n = (state or "").strip().upper()
    if len(state_n) != 2 or not state_n.isalpha():
        raise PreparedIngestError("state must be a two-letter abbreviation")
    return state_n


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PreparedIngestError(f"bundle.{key} is required")
    return value.strip()


def _normalize_bool(value: Any, *, field: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise PreparedIngestError(f"{field} must be true, false, or null")


def validate_text_sidecar(payload: dict[str, Any]) -> tuple[list[PageContent], int]:
    if not isinstance(payload, dict):
        raise PreparedIngestError("text sidecar must be a JSON object")
    raw_pages = payload.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        raise PreparedIngestError("text sidecar pages must be a non-empty list")

    pages: list[PageContent] = []
    for idx, page in enumerate(raw_pages):
        if not isinstance(page, dict):
            raise PreparedIngestError(f"text sidecar pages[{idx}] must be an object")
        number = page.get("number")
        text = page.get("text", "")
        if not isinstance(number, int) or number < 1:
            raise PreparedIngestError(
                f"text sidecar pages[{idx}].number must be a positive integer"
            )
        if not isinstance(text, str):
            raise PreparedIngestError(f"text sidecar pages[{idx}].text must be a string")
        pages.append(PageContent(number=number, text=text))

    docai_pages = payload.get("docai_pages", 0)
    if not isinstance(docai_pages, int) or docai_pages < 0:
        raise PreparedIngestError("text sidecar docai_pages must be a non-negative integer")
    return pages, docai_pages


def validate_bundle(payload: dict[str, Any], *, expected_state: str | None = None) -> PreparedBundle:
    if not isinstance(payload, dict):
        raise PreparedIngestError("bundle.json must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise PreparedIngestError(f"unsupported schema_version {schema_version!r}")

    state = _normalize_state(_require_string(payload, "state"))
    if expected_state and state != _normalize_state(expected_state):
        raise PreparedIngestError(f"bundle state {state} does not match requested state")

    bundle_id = _require_string(payload, "bundle_id")
    source_manifest_uri = _require_string(payload, "source_manifest_uri")
    src = parse_gcs_uri(source_manifest_uri)
    if src.bucket != DEFAULT_BANK_BUCKET or not src.blob.startswith(f"{VERSION_PREFIX}/{state}/"):
        raise PreparedIngestError(
            "source_manifest_uri must preserve the raw bank input under "
            f"gs://{DEFAULT_BANK_BUCKET}/{VERSION_PREFIX}/{state}/..."
        )

    hoa_name = _require_string(payload, "hoa_name")
    docs_raw = payload.get("documents")
    if not isinstance(docs_raw, list) or not docs_raw:
        raise PreparedIngestError("bundle.documents must be a non-empty list")

    documents: list[PreparedDocument] = []
    seen_shas: set[str] = set()
    for idx, doc in enumerate(docs_raw):
        if not isinstance(doc, dict):
            raise PreparedIngestError(f"documents[{idx}] must be an object")
        sha256 = _require_string(doc, "sha256").lower()
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise PreparedIngestError(f"documents[{idx}].sha256 must be a hex SHA-256")
        if sha256 in seen_shas:
            raise PreparedIngestError(f"duplicate document sha256: {sha256}")
        seen_shas.add(sha256)

        category = _require_string(doc, "category").lower()
        if category in REJECT_PII:
            raise PreparedIngestError(f"documents[{idx}].category is PII and cannot import")
        if category not in PREPARED_ALLOWED_CATEGORIES:
            raise PreparedIngestError(
                f"documents[{idx}].category {category!r} is not allowed for prepared bulk import"
            )

        pdf_gcs_path = _require_string(doc, "pdf_gcs_path")
        text_gcs_path = _require_string(doc, "text_gcs_path")
        parse_gcs_uri(pdf_gcs_path)
        parse_gcs_uri(text_gcs_path)
        filename = _require_string(doc, "filename")
        page_count = doc.get("page_count")
        if page_count is not None and (not isinstance(page_count, int) or page_count < 1):
            raise PreparedIngestError(f"documents[{idx}].page_count must be positive")
        docai_pages = doc.get("docai_pages", 0)
        if not isinstance(docai_pages, int) or docai_pages < 0:
            raise PreparedIngestError(f"documents[{idx}].docai_pages must be non-negative")

        documents.append(
            PreparedDocument(
                sha256=sha256,
                filename=filename,
                pdf_gcs_path=pdf_gcs_path,
                text_gcs_path=text_gcs_path,
                source_url=doc.get("source_url") if isinstance(doc.get("source_url"), str) else None,
                category=category,
                text_extractable=_normalize_bool(
                    doc.get("text_extractable"), field=f"documents[{idx}].text_extractable"
                ),
                page_count=page_count,
                docai_pages=docai_pages,
                filter_reason=doc.get("filter_reason")
                if isinstance(doc.get("filter_reason"), str)
                else None,
            )
        )

    address = payload.get("address") if isinstance(payload.get("address"), dict) else {}
    geometry = payload.get("geometry") if isinstance(payload.get("geometry"), dict) else {}
    rejected = (
        payload.get("rejected_documents")
        if isinstance(payload.get("rejected_documents"), list)
        else []
    )
    return PreparedBundle(
        schema_version=schema_version,
        bundle_id=bundle_id,
        source_manifest_uri=source_manifest_uri,
        state=state,
        county=payload.get("county") if isinstance(payload.get("county"), str) else None,
        hoa_name=hoa_name,
        metadata_type=payload.get("metadata_type")
        if isinstance(payload.get("metadata_type"), str)
        else None,
        website_url=payload.get("website_url") if isinstance(payload.get("website_url"), str) else None,
        address=address,
        geometry=geometry,
        documents=documents,
        rejected_documents=rejected,
        created_at=payload.get("created_at") if isinstance(payload.get("created_at"), str) else None,
        raw=payload,
    )


def load_json_blob(bucket: gcs.Bucket, blob_name: str) -> dict[str, Any]:
    blob = bucket.blob(blob_name)
    try:
        return json.loads(blob.download_as_bytes())
    except gcs_exceptions.NotFound as exc:
        raise PreparedIngestError(f"missing GCS object: gs://{bucket.name}/{blob_name}") from exc
    except json.JSONDecodeError as exc:
        raise PreparedIngestError(f"invalid JSON in gs://{bucket.name}/{blob_name}: {exc}") from exc


def download_blob_bytes(bucket: gcs.Bucket, blob_name: str) -> bytes:
    blob = bucket.blob(blob_name)
    try:
        return blob.download_as_bytes()
    except gcs_exceptions.NotFound as exc:
        raise PreparedIngestError(f"missing GCS object: gs://{bucket.name}/{blob_name}") from exc


def _load_status_with_generation(blob: gcs.Blob) -> tuple[dict[str, Any], int]:
    try:
        blob.reload()
        generation = int(blob.generation or 0)
        status = json.loads(blob.download_as_bytes())
    except gcs_exceptions.NotFound as exc:
        raise PreparedIngestError(f"missing status object: gs://{blob.bucket.name}/{blob.name}") from exc
    except json.JSONDecodeError as exc:
        raise PreparedIngestError(f"invalid status JSON: gs://{blob.bucket.name}/{blob.name}") from exc
    if status.get("status") not in PREPARED_STATUSES:
        raise PreparedIngestError(f"invalid bundle status {status.get('status')!r}")
    return status, generation


def list_ready_bundle_prefixes(
    bucket: gcs.Bucket,
    *,
    state: str,
    limit: int = 10,
) -> list[str]:
    state_n = _normalize_state(state)
    out: list[str] = []
    for blob in bucket.list_blobs(prefix=f"{VERSION_PREFIX}/{state_n}/"):
        if not blob.name.endswith("/status.json"):
            continue
        try:
            status = json.loads(blob.download_as_bytes())
        except Exception:
            continue
        if status.get("status") == "ready":
            out.append(blob.name.removesuffix("/status.json"))
            if len(out) >= limit:
                break
    return out


def claim_ready_bundle(
    bucket: gcs.Bucket,
    bundle_prefix: str,
    *,
    claimed_by: str | None = None,
) -> bool:
    blob = bucket.blob(status_blob_name(bundle_prefix))
    status, generation = _load_status_with_generation(blob)
    if status.get("status") != "ready":
        return False
    updated = {
        **status,
        "status": "claimed",
        "claimed_by": claimed_by or default_claimant(),
        "claimed_at": now_iso(),
        "error": None,
    }
    try:
        blob.upload_from_string(
            json.dumps(updated, indent=2, sort_keys=True),
            content_type="application/json",
            if_generation_match=generation,
        )
    except gcs_exceptions.PreconditionFailed:
        return False
    return True


def update_bundle_status(
    bucket: gcs.Bucket,
    bundle_prefix: str,
    *,
    status: str,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    if status not in PREPARED_STATUSES:
        raise PreparedIngestError(f"invalid status {status!r}")
    blob = bucket.blob(status_blob_name(bundle_prefix))
    current, generation = _load_status_with_generation(blob)
    updated = {**current, "status": status, "error": error}
    if status == "imported":
        updated["imported_at"] = now_iso()
    if status == "failed":
        updated["failed_at"] = now_iso()
    if extra:
        updated.update(extra)
    blob.upload_from_string(
        json.dumps(updated, indent=2, sort_keys=True),
        content_type="application/json",
        if_generation_match=generation,
    )
