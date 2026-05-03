"""Raw HOA document bank — write-only sink for discovery results.

Discovery agents call ``bank_hoa()`` to record what they found about an HOA
(name, address, website, metadata-source provenance) and to upload any PDFs
they were able to fetch. The bank is a GCS bucket; the OCR/embed/upload
pipeline is a separate downstream consumer that reads manifests back out.

Layout (state known)::

    gs://{bucket}/v1/{STATE}/{county-slug}/{hoa-slug}/manifest.json
    gs://{bucket}/v1/{STATE}/{county-slug}/{hoa-slug}/doc-{sha256[:12]}/original.pdf
    gs://{bucket}/v1/{STATE}/{county-slug}/{hoa-slug}/doc-{sha256[:12]}/precheck.json

Layout (state unknown — re-bank under the right state once verified)::

    gs://{bucket}/v1/_unverified/{source-slug}/{hoa-slug}/manifest.json

Detective mode: every call appends to ``metadata_sources`` (provenance log)
and merges into the existing manifest under a GCS generation precondition,
so two parallel writers see eventually-consistent results without a lock.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from google.api_core import exceptions as gcs_exceptions
from google.cloud import storage as gcs

SCHEMA_VERSION = 1
DEFAULT_BUCKET = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")
VERSION_PREFIX = "v1"

# Higher rank = more complete discovery state; merges keep the better one.
_STATUS_RANK = {
    "stub_unverified": 0,
    "stub_state_unknown": 1,
    "stub_walled": 2,
    "stub_no_docs": 3,
    "ready_with_docs": 4,
}

# ---------------------------------------------------------------------------
# Slugging / paths
# ---------------------------------------------------------------------------

_HOA_NOISE_RE = re.compile(
    r"\b(hoa|homeowners?|home\s*owners?|community|civic|cluster|condominium|condo|"
    r"property\s*owners?|owners?|master|association|assn|assoc|inc|llc|co|the)\b",
    re.IGNORECASE,
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Stable slug for path identity. Strips common HOA noise words.

    ``"Reston Association, Inc."`` -> ``"reston"``.
    Two records that slug to the same value will merge into one manifest.
    """
    if not name:
        return ""
    s = _HOA_NOISE_RE.sub(" ", name.lower())
    s = _NON_ALNUM_RE.sub("-", s).strip("-")
    if s:
        return s
    # Fallback if every token was noise (e.g. "The HOA Inc"): preserve the
    # original tokens so the path is still unique.
    return _NON_ALNUM_RE.sub("-", name.lower()).strip("-")


def _state_norm(state: str | None) -> str | None:
    if not state:
        return None
    s = state.strip().upper()
    return s if len(s) == 2 and s.isalpha() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path_for(
    *,
    state: str | None,
    county: str | None,
    hoa_slug: str,
    source_slug: str | None,
) -> str:
    """GCS object prefix (no trailing slash) for an HOA."""
    state_n = _state_norm(state)
    if state_n:
        county_slug = slugify(county) if county else "_unknown-county"
        return f"{VERSION_PREFIX}/{state_n}/{county_slug}/{hoa_slug}"
    src = slugify(source_slug) if source_slug else "_unknown-source"
    return f"{VERSION_PREFIX}/_unverified/{src}/{hoa_slug}"


# ---------------------------------------------------------------------------
# PDF inspection (replaces scripts/hoa_precheck.py for in-process use)
# ---------------------------------------------------------------------------

def _inspect_pdf(pdf_bytes: bytes, filename: str, hoa_name: str) -> dict:
    """Same shape as scripts/hoa_precheck.py output, but in-memory."""
    import pypdf
    from .cost_tracker import COST_DOCAI_PER_PAGE
    from .doc_classifier import (
        REJECT_JUNK,
        REJECT_PII,
        VALID_CATEGORIES,
        classify_from_filename,
        classify_from_text,
        classify_with_llm,
    )

    sha = hashlib.sha256(pdf_bytes).hexdigest()
    page_count: int | None = None
    text_extractable: bool | None = None
    suggested_category: str | None = None
    method: str | None = None
    confidence: float | None = None
    rationale: str | None = None
    model: str | None = None
    error: str | None = None
    full_text = ""

    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
        if page_count:
            first = reader.pages[0].extract_text() or ""
            text_extractable = len(first.strip()) >= 50
            if text_extractable:
                parts: list[str] = []
                for i in range(min(5, page_count)):
                    try:
                        parts.append(reader.pages[i].extract_text() or "")
                    except Exception:
                        pass
                full_text = "\n".join(parts)
                clf = classify_from_text(full_text, hoa_name)
                if clf:
                    suggested_category = clf["category"]
                    method = clf["method"]
                    confidence = clf["confidence"]
    except Exception as exc:
        error = f"PyPDF inspection failed: {exc}"

    if not suggested_category:
        clf = classify_from_filename(filename)
        if clf:
            suggested_category = clf["category"]
            method = clf["method"]
            confidence = clf["confidence"]

    if (
        not suggested_category
        and full_text.strip()
        and os.environ.get("HOA_ENABLE_LLM_CLASSIFIER", "0") in {"1", "true", "True"}
    ):
        try:
            clf = classify_with_llm(full_text, hoa_name, filename=filename)
            if clf:
                suggested_category = clf["category"]
                method = clf["method"]
                confidence = clf["confidence"]
                rationale = clf.get("rationale")
                model = clf.get("model")
        except Exception as exc:
            rationale = f"llm_classifier_failed:{type(exc).__name__}"

    is_valid = suggested_category in VALID_CATEGORIES
    is_pii = suggested_category in REJECT_PII
    is_junk = suggested_category in REJECT_JUNK
    est_pages = (page_count or 0) if text_extractable is False else 0

    return {
        "ok": error is None,
        "error": error,
        "filename": filename,
        "sha256": sha,
        "file_size_bytes": len(pdf_bytes),
        "page_count": page_count,
        "text_extractable": text_extractable,
        "suggested_category": suggested_category,
        "classification_method": method,
        "classification_confidence": confidence,
        "classification_model": model,
        "classification_rationale": rationale,
        "is_valid_governing_doc": is_valid,
        "is_pii_risk": is_pii,
        "is_junk": is_junk,
        "est_docai_pages": est_pages,
        "est_docai_cost_usd": round(est_pages * COST_DOCAI_PER_PAGE, 6),
    }


# ---------------------------------------------------------------------------
# Manifest merge
# ---------------------------------------------------------------------------

def _better_status(a: str, b: str) -> str:
    return a if _STATUS_RANK.get(a, -1) >= _STATUS_RANK.get(b, -1) else b


def _dedup_append(existing: list, new: list, key: str) -> list:
    seen = {item.get(key) for item in existing if isinstance(item, dict)}
    out = list(existing)
    for item in new:
        if isinstance(item, dict) and item.get(key) not in seen:
            seen.add(item.get(key))
            out.append(item)
    return out


def _merge_scalar_dict(
    existing: dict,
    new: dict,
    fields: Iterable[str],
    newest_wins: Iterable[str] = (),
) -> dict:
    """Merge per-field. ``newest_wins`` fields always overwrite if new value present
    (state-of-knowledge — e.g. probe-time fingerprint). Other fields are
    first-write-wins; conflicting overwrites are logged but kept stable."""
    merged = dict(existing)
    conflicts = list(existing.get("_conflicts", []))
    nw = set(newest_wins)
    for f in fields:
        ev = existing.get(f)
        nv = new.get(f)
        if nv is None or nv == "":
            continue
        if f in nw:
            merged[f] = nv
        elif ev in (None, ""):
            merged[f] = nv
        elif ev != nv:
            conflicts.append(
                {"field": f, "existing": ev, "new": nv, "at": _now()}
            )
    if conflicts:
        merged["_conflicts"] = conflicts
    return merged


def _merge_manifest(existing: dict, new: dict) -> dict:
    """Merge ``new`` into ``existing``. Returns a new dict; never mutates inputs."""
    merged: dict = dict(existing) if existing else {}
    merged["schema_version"] = SCHEMA_VERSION
    merged["name"] = (existing.get("name") if existing else None) or new.get("name")

    # name_aliases — union (and demote any older "name" if new call canonicalised)
    aliases: set[str] = set(existing.get("name_aliases") or []) if existing else set()
    for alias in new.get("name_aliases") or []:
        if alias and alias != merged["name"]:
            aliases.add(alias)
    if (
        existing
        and existing.get("name")
        and new.get("name")
        and existing["name"] != new["name"]
    ):
        aliases.add(new["name"])
    merged["name_aliases"] = sorted(aliases)

    # metadata_type — prefer non-"unknown"
    et = (existing or {}).get("metadata_type") or "unknown"
    nt = new.get("metadata_type") or "unknown"
    merged["metadata_type"] = et if et != "unknown" else nt

    addr_fields = ("street", "city", "state", "postal_code", "county", "country")
    merged["address"] = _merge_scalar_dict(
        (existing or {}).get("address") or {}, new.get("address") or {}, addr_fields
    )

    geo_fields = ("latitude", "longitude", "boundary_geojson", "centroid_source")
    merged["geometry"] = _merge_scalar_dict(
        (existing or {}).get("geometry") or {}, new.get("geometry") or {}, geo_fields
    )

    web_fields = ("url", "platform", "is_walled")
    merged["website"] = _merge_scalar_dict(
        (existing or {}).get("website") or {},
        new.get("website") or {},
        web_fields,
        newest_wins=("platform", "is_walled"),
    )

    e_disc = (existing or {}).get("discovery") or {}
    n_disc = new.get("discovery") or {}
    last_probed = max(filter(None, [e_disc.get("last_probed"), n_disc.get("last_probed"), _now()]))
    merged["discovery"] = {
        "first_seen": e_disc.get("first_seen") or n_disc.get("first_seen") or _now(),
        "last_probed": last_probed,
        "status": _better_status(
            e_disc.get("status") or "stub_unverified",
            n_disc.get("status") or "stub_unverified",
        ),
        "state_verified_via": (
            n_disc.get("state_verified_via")
            or e_disc.get("state_verified_via")
            or "name-only-unverified"
        ),
    }

    merged["metadata_sources"] = ((existing or {}).get("metadata_sources") or []) + (
        new.get("metadata_sources") or []
    )

    merged["documents"] = _dedup_append(
        (existing or {}).get("documents") or [], new.get("documents") or [], key="sha256"
    )
    merged["skipped_documents"] = _dedup_append(
        (existing or {}).get("skipped_documents") or [],
        new.get("skipped_documents") or [],
        key="source_url",
    )

    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class DocumentInput:
    """A PDF a discovery agent fetched and wants to bank."""

    pdf_bytes: bytes
    source_url: str
    filename: str | None = None  # used for category fallback when text inspection misses
    category_hint: str | None = None  # ccr | bylaws | articles | rules | amendment | resolution
    text_extractable_hint: bool | None = None


def _derive_status(
    *, doc_records: list[dict], website: dict, state: str | None
) -> str:
    if doc_records:
        return "ready_with_docs"
    if not state:
        return "stub_state_unknown"
    if website.get("is_walled"):
        return "stub_walled"
    return "stub_no_docs"


def bank_hoa(
    *,
    name: str,
    metadata_type: str = "unknown",
    address: dict | None = None,
    geometry: dict | None = None,
    website: dict | None = None,
    metadata_source: dict,
    documents: list[DocumentInput] | None = None,
    skipped_documents: list[dict] | None = None,
    name_aliases: Iterable[str] | None = None,
    state_verified_via: str | None = None,
    bucket_name: str = DEFAULT_BUCKET,
    client: gcs.Client | None = None,
) -> str:
    """Write or merge an HOA record into the bank. Returns the manifest GCS URI.

    ``metadata_source`` is required: every call records who saw what, so future
    sessions can reconcile conflicts and re-probe stale entries.

    ``documents`` may be empty — it's normal to bank a name-only stub. PDFs
    that we considered but rejected (login-walled, oversize, wrong state, etc.)
    go in ``skipped_documents`` so the audit trail is preserved.
    """
    if not name:
        raise ValueError("name is required")
    if not metadata_source or not metadata_source.get("source"):
        raise ValueError("metadata_source.source is required")

    address = address or {}
    geometry = geometry or {}
    website = website or {}
    documents = documents or []
    skipped_documents = skipped_documents or []
    name_aliases = list(name_aliases or [])

    state = _state_norm(address.get("state"))
    hoa_slug = slugify(name)
    if not hoa_slug:
        raise ValueError(f"could not slugify name: {name!r}")
    prefix = _path_for(
        state=state,
        county=address.get("county"),
        hoa_slug=hoa_slug,
        source_slug=metadata_source.get("source"),
    )

    client = client or gcs.Client()
    bucket = client.bucket(bucket_name)

    # Upload PDFs + precheck under doc-{sha[:12]}/. Idempotent: skip blobs that
    # already exist (sha-keyed paths make repeated banks cheap).
    doc_records: list[dict] = []
    for doc in documents:
        sha = hashlib.sha256(doc.pdf_bytes).hexdigest()
        doc_id = sha[:12]
        doc_prefix = f"{prefix}/doc-{doc_id}"
        pdf_blob = bucket.blob(f"{doc_prefix}/original.pdf")
        if not pdf_blob.exists():
            pdf_blob.upload_from_string(doc.pdf_bytes, content_type="application/pdf")

        precheck_blob = bucket.blob(f"{doc_prefix}/precheck.json")
        if precheck_blob.exists():
            try:
                precheck = json.loads(precheck_blob.download_as_bytes())
            except Exception:
                precheck = {}
        else:
            filename = doc.filename or doc.source_url.rsplit("/", 1)[-1] or f"{doc_id}.pdf"
            try:
                precheck = _inspect_pdf(doc.pdf_bytes, filename, name)
            except Exception as exc:  # pragma: no cover — defensive
                precheck = {"ok": False, "error": str(exc)}
            precheck_blob.upload_from_string(
                json.dumps(precheck, indent=2, sort_keys=True),
                content_type="application/json",
            )

        doc_records.append(
            {
                "doc_id": doc_id,
                "sha256": sha,
                "size_bytes": len(doc.pdf_bytes),
                "page_count": precheck.get("page_count"),
                "text_extractable": precheck.get("text_extractable"),
                "text_extractable_hint": doc.text_extractable_hint,
                "suggested_category": precheck.get("suggested_category"),
                "category_hint": doc.category_hint,
                "is_pii_risk": precheck.get("is_pii_risk"),
                "is_junk": precheck.get("is_junk"),
                "source_url": doc.source_url,
                "filename": doc.filename,
                "fetched_at": _now(),
                "gcs_path": f"gs://{bucket_name}/{doc_prefix}/original.pdf",
            }
        )

    # Stamp this call's contribution as a partial manifest, then merge into
    # whatever's already at the path under a generation precondition.
    new_manifest = {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "name_aliases": name_aliases,
        "metadata_type": metadata_type,
        "address": address,
        "geometry": geometry,
        "website": website,
        "discovery": {
            "first_seen": _now(),
            "last_probed": _now(),
            "status": _derive_status(doc_records=doc_records, website=website, state=state),
            "state_verified_via": state_verified_via
            or ("homepage-html" if state else "name-only-unverified"),
        },
        "metadata_sources": [
            {
                **metadata_source,
                "fetched_at": metadata_source.get("fetched_at") or _now(),
            }
        ],
        "documents": doc_records,
        "skipped_documents": skipped_documents,
    }

    manifest_blob = bucket.blob(f"{prefix}/manifest.json")
    for attempt in range(3):
        try:
            data = manifest_blob.download_as_bytes()
            existing = json.loads(data)
            manifest_blob.reload()
            generation = manifest_blob.generation
        except gcs_exceptions.NotFound:
            existing = None
            generation = 0  # if-generation-match=0 means "create only"
        merged = _merge_manifest(existing or {}, new_manifest)
        try:
            manifest_blob.upload_from_string(
                json.dumps(merged, indent=2, sort_keys=True),
                content_type="application/json",
                if_generation_match=generation,
            )
            break
        except gcs_exceptions.PreconditionFailed:
            if attempt == 2:
                raise

    return f"gs://{bucket_name}/{prefix}/manifest.json"
