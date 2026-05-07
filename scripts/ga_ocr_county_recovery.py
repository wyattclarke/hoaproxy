#!/usr/bin/env python3
"""DocAI-based county recovery for the GA `_unknown-county/` backlog.

For every banked GA manifest still stuck under `_unknown-county/`,
this worker:

  1. Skips the manifest if any document is already text-extractable
     and we've already detected a county from that text — those are
     handled by `scripts/ga_cleanup_unknown_county.py`.
  2. Picks the first document with text_extractable=false (scanned)
     and DocAI-OCRs its first --max-pages pages (default 3).
  3. Writes the OCR text as a sidecar at
     `gs://hoaproxy-bank/v1/GA/_unknown-county/<slug>/doc-{sha[:12]}/ocr-page1-3.txt`
     so the prepare-for-ingest worker can re-use it without re-OCRing.
  4. Re-runs the existing detect_state_county / city heuristics over
     (manifest name + slug + URL + OCR text). If a county pops out,
     GCS-rewrites the manifest under v1/GA/<county>/<slug>/ via
     server-side copy + delete (same machinery as the heuristic
     backfill).
  5. Logs every decision (page count + dollars) to a JSONL ledger so
     spend is auditable.

Hard caps:
  --max-docai-cost-usd: stop OCR once cumulative spend hits the cap.
  --max-pages: per-doc OCR page cap (default 3).
  --limit: process at most N manifests.

This script never sends document text to OpenRouter; it uses DocAI
for OCR and the deterministic state/county detector to route. No
prompts or PDF text leave the bank/DocAI pipeline.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from google.cloud import storage as gcs  # noqa: E402

from hoaware.bank import slugify  # noqa: E402
from hoaware.config import load_settings  # noqa: E402
from hoaware.cost_tracker import COST_DOCAI_PER_PAGE  # noqa: E402

# Reuse the heuristic helpers and the cross-state cleaner's detector.
from scripts.ga_county_backfill import (  # noqa: E402
    BUCKET_NAME,
    UNKNOWN_PREFIX,
    copy_prefix,
    delete_prefix,
    extract_pdf_text,
    infer_county_from_city_in_text,
    infer_county_from_text,
    list_unknown_county_manifests,
    update_manifest_county,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_ledger(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _ocr_first_pages(pdf_bytes: bytes, *, max_pages: int) -> tuple[str, int]:
    """DocAI OCR the first `max_pages` of a PDF. Returns (text, pages_used)."""
    settings = load_settings()
    if not (settings.enable_docai and settings.docai_project_id and settings.docai_processor_id):
        raise RuntimeError("DocAI not configured (HOA_ENABLE_DOCAI / HOA_DOCAI_PROJECT_ID / HOA_DOCAI_PROCESSOR_ID)")

    from hoaware.docai import extract_with_document_ai

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp.flush()
        path = Path(tmp.name)
    try:
        pages = extract_with_document_ai(
            path,
            project_id=settings.docai_project_id,
            location=settings.docai_location,
            processor_id=settings.docai_processor_id,
            endpoint=settings.docai_endpoint,
            max_pages_per_call=max_pages,
            page_numbers=list(range(1, max_pages + 1)),
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    text = "\n".join(p.text for p in pages if p.text and p.text.strip())
    return text, len(pages)


def _heuristic_county(name: str, slug: str, source_url: str | None,
                      pdf_text: str) -> str | None:
    haystack = " ".join(filter(None, [name, slug.replace("-", " "), source_url, pdf_text[:25000]]))
    county = infer_county_from_text(haystack)
    if county:
        return county
    if pdf_text:
        return infer_county_from_city_in_text(pdf_text)
    return None


def _has_extractable_text(manifest: dict) -> bool:
    for doc in manifest.get("documents") or []:
        if doc.get("text_extractable") is True:
            return True
    return False


def _has_existing_ocr_sidecar(client: gcs.Client, manifest: dict, bucket_name: str) -> str | None:
    """Return cached OCR text from a previous run if any sidecar exists."""
    bucket = client.bucket(bucket_name)
    for doc in manifest.get("documents") or []:
        gcs_path = doc.get("gcs_path", "")
        if not gcs_path.startswith(f"gs://{bucket_name}/"):
            continue
        prefix = "/".join(gcs_path[len(f"gs://{bucket_name}/"):].split("/")[:-1])
        sidecar_blob = bucket.blob(f"{prefix}/ocr-page1-3.txt")
        if sidecar_blob.exists():
            try:
                return sidecar_blob.download_as_text()
            except Exception:
                continue
    return None


def _scanned_doc(manifest: dict) -> dict | None:
    for doc in manifest.get("documents") or []:
        if doc.get("text_extractable") is False and doc.get("gcs_path"):
            return doc
    return None


def process_manifest(
    client: gcs.Client,
    manifest_blob: gcs.Blob,
    *,
    bank_bucket_name: str,
    max_pages: int,
    cost_remaining: float,
    dry_run: bool,
) -> tuple[dict, float]:
    """Returns (result_record, cost_consumed)."""
    bucket = client.bucket(bank_bucket_name)
    name_parts = manifest_blob.name.split("/")
    if len(name_parts) < 5:
        return {"status": "skip_bad_path", "blob": manifest_blob.name}, 0.0
    hoa_slug = name_parts[3]
    old_prefix = "/".join(name_parts[:4])

    try:
        manifest = json.loads(manifest_blob.download_as_bytes())
    except Exception as exc:
        return {"status": "skip_bad_manifest", "slug": hoa_slug, "error": str(exc)}, 0.0

    name = manifest.get("name") or hoa_slug
    metadata_sources = manifest.get("metadata_sources") or []
    source_url = next(
        (s.get("source_url") for s in metadata_sources if s.get("source_url")),
        None,
    )

    # Step 0: cached OCR sidecar from a previous run.
    cached_ocr = _has_existing_ocr_sidecar(client, manifest, bank_bucket_name)
    if cached_ocr:
        county = _heuristic_county(name, hoa_slug, source_url, cached_ocr)
        if county:
            return _try_reroute(client, bucket, manifest_blob, manifest,
                                hoa_slug, old_prefix, name, county, "cached_ocr", dry_run), 0.0
        return {"status": "cached_ocr_no_county", "slug": hoa_slug}, 0.0

    # Step 1: skip if there's already extractable text we just need to re-check.
    if _has_extractable_text(manifest):
        # Earlier passes already had text and still couldn't route — re-OCR
        # would not help. Caller can re-run the cleanup script if heuristics
        # changed.
        return {"status": "skip_text_extractable", "slug": hoa_slug}, 0.0

    # Step 2: pick the first scanned doc to OCR.
    doc = _scanned_doc(manifest)
    if not doc:
        return {"status": "no_scanned_doc", "slug": hoa_slug}, 0.0

    page_count = int(doc.get("page_count") or max_pages)
    pages_to_ocr = min(max_pages, max(1, page_count))
    projected_cost = pages_to_ocr * COST_DOCAI_PER_PAGE
    if projected_cost > cost_remaining:
        return {
            "status": "cost_cap_reached", "slug": hoa_slug,
            "pages_to_ocr": pages_to_ocr, "projected_cost": projected_cost,
            "cost_remaining": cost_remaining,
        }, 0.0

    # Step 3: download + OCR.
    gcs_path = doc.get("gcs_path", "")
    raw = gcs_path[len(f"gs://{bank_bucket_name}/"):]
    pdf_blob = bucket.blob(raw)
    if not pdf_blob.exists():
        return {"status": "missing_pdf", "slug": hoa_slug, "gcs_path": gcs_path}, 0.0
    try:
        pdf_bytes = pdf_blob.download_as_bytes()
    except Exception as exc:
        return {"status": "download_error", "slug": hoa_slug, "error": str(exc)[:200]}, 0.0

    try:
        ocr_text, pages_used = _ocr_first_pages(pdf_bytes, max_pages=pages_to_ocr)
    except Exception as exc:
        return {"status": "ocr_error", "slug": hoa_slug, "error": str(exc)[:200]}, 0.0

    spent = pages_used * COST_DOCAI_PER_PAGE

    if not dry_run and ocr_text:
        # Persist OCR sidecar so re-runs and the prepare worker reuse it.
        sidecar_prefix = "/".join(raw.split("/")[:-1])
        sidecar_blob = bucket.blob(f"{sidecar_prefix}/ocr-page1-3.txt")
        sidecar_blob.upload_from_string(
            ocr_text, content_type="text/plain; charset=utf-8",
        )

    # Step 4: re-detect county from OCR + name + URL.
    county = _heuristic_county(name, hoa_slug, source_url, ocr_text)
    if not county:
        return {
            "status": "no_county_after_ocr", "slug": hoa_slug,
            "pages_ocrd": pages_used, "spent": spent,
        }, spent

    # Step 5: re-route.
    result = _try_reroute(client, bucket, manifest_blob, manifest,
                          hoa_slug, old_prefix, name, county, "ocr_recovered", dry_run)
    result["pages_ocrd"] = pages_used
    result["spent"] = spent
    return result, spent


def _try_reroute(
    client: gcs.Client,
    bucket,
    manifest_blob: gcs.Blob,
    manifest: dict,
    hoa_slug: str,
    old_prefix: str,
    name: str,
    county: str,
    source_label: str,
    dry_run: bool,
) -> dict:
    county_slug = slugify(county)
    new_prefix = f"v1/GA/{county_slug}/{hoa_slug}"
    if new_prefix == old_prefix:
        return {"status": "already_routed", "slug": hoa_slug, "county": county, "source": source_label}
    new_manifest = bucket.blob(f"{new_prefix}/manifest.json")
    if new_manifest.exists():
        return {"status": "collision", "slug": hoa_slug, "county": county, "source": source_label}
    if dry_run:
        return {"status": "dry_would_move", "slug": hoa_slug, "county": county, "source": source_label}
    copied = copy_prefix(client, old_prefix, new_prefix)
    new_gcs_paths: dict[str, str] = {}
    for old_blob in client.list_blobs(bucket, prefix=old_prefix + "/"):
        if old_blob.name.endswith("/original.pdf"):
            old_uri = f"gs://{BUCKET_NAME}/{old_blob.name}"
            new_uri = f"gs://{BUCKET_NAME}/{new_prefix}/{old_blob.name[len(old_prefix) + 1:]}"
            new_gcs_paths[old_uri] = new_uri
    update_manifest_county(client, new_prefix, county, new_gcs_paths)
    deleted = delete_prefix(client, old_prefix)
    return {
        "status": "moved", "slug": hoa_slug, "county": county, "source": source_label,
        "copied": len(copied), "deleted": deleted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="DocAI county recovery for stuck GA _unknown-county/ manifests")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-pages", type=int, default=3,
                        help="Per-doc OCR page cap (default 3).")
    parser.add_argument("--max-docai-cost-usd", type=float, default=10.0,
                        help="Stop OCRing once cumulative DocAI spend hits this cap.")
    parser.add_argument("--bank-bucket", default=os.environ.get("HOA_BANK_GCS_BUCKET", BUCKET_NAME))
    parser.add_argument("--ledger", type=Path, default=Path("data/ga_ocr_county_recovery_ledger.jsonl"))
    args = parser.parse_args()

    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "hoaware"

    client = gcs.Client()
    manifests = list_unknown_county_manifests(client)
    if args.limit:
        manifests = manifests[: args.limit]
    print(f"Found {len(manifests)} _unknown-county manifests under v1/GA/", file=sys.stderr)

    summary: dict[str, int] = {}
    total_spent = 0.0
    cost_remaining = max(0.0, args.max_docai_cost_usd - total_spent)
    for i, blob in enumerate(manifests, 1):
        result, spent = process_manifest(
            client, blob,
            bank_bucket_name=args.bank_bucket,
            max_pages=args.max_pages,
            cost_remaining=cost_remaining,
            dry_run=args.dry_run,
        )
        total_spent += spent
        cost_remaining = max(0.0, args.max_docai_cost_usd - total_spent)
        summary[result["status"]] = summary.get(result["status"], 0) + 1
        ledger_record = {
            "ts": _now(), "i": i, "manifest": blob.name,
            "total_spent_usd": round(total_spent, 4),
            **result,
        }
        _append_ledger(args.ledger, ledger_record)
        print(json.dumps({"i": i, "spent": round(spent, 4), **result}))
        if cost_remaining <= 0:
            print(json.dumps({"warning": "cost_cap_reached", "total_spent_usd": round(total_spent, 4)}), file=sys.stderr)
            break
        # gentle pace so DocAI quota stays happy
        if not args.dry_run and spent > 0:
            time.sleep(0.4)

    print(json.dumps({"summary": summary, "total_spent_usd": round(total_spent, 4)}, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
