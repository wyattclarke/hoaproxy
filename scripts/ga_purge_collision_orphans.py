#!/usr/bin/env python3
"""Delete `_unknown-county/<slug>/` GA manifests whose cleanup pass
found a collision — meaning the same HOA already exists at the
correct (county, slug) prefix. The cleanup script left those
orphan copies behind because GCS rewrite would have overwritten the
canonical manifest.

This script reads the cleanup log lines, picks up the orphan slug +
detected (county, new_slug) target, verifies the canonical manifest
exists, double-checks every PDF in the orphan is also banked at the
canonical prefix (sha-keyed), and only then deletes the orphan.

Idempotent. Doesn't touch anything outside `_unknown-county/`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from google.cloud import storage as gcs  # noqa: E402

from hoaware.bank import slugify  # noqa: E402

BUCKET_NAME = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")


def list_orphan_shas(client: gcs.Client, orphan_prefix: str) -> set[str]:
    bucket = client.bucket(BUCKET_NAME)
    out: set[str] = set()
    for blob in client.list_blobs(bucket, prefix=orphan_prefix + "/"):
        if blob.name.endswith("/original.pdf"):
            # doc-{sha[:12]}/original.pdf — pull the sha[:12].
            parts = blob.name.split("/")
            for p in parts:
                if p.startswith("doc-"):
                    out.add(p[len("doc-"):])
                    break
    return out


def list_canonical_shas(client: gcs.Client, canonical_prefix: str) -> set[str]:
    bucket = client.bucket(BUCKET_NAME)
    out: set[str] = set()
    for blob in client.list_blobs(bucket, prefix=canonical_prefix + "/"):
        if blob.name.endswith("/original.pdf"):
            parts = blob.name.split("/")
            for p in parts:
                if p.startswith("doc-"):
                    out.add(p[len("doc-"):])
                    break
    return out


def delete_prefix(client: gcs.Client, prefix: str) -> int:
    bucket = client.bucket(BUCKET_NAME)
    n = 0
    for blob in client.list_blobs(bucket, prefix=prefix + "/"):
        blob.delete()
        n += 1
    return n


def _merge_unique_pdfs(
    client: gcs.Client,
    orphan_prefix: str,
    canonical_prefix: str,
    unique_shas: set[str],
) -> list[dict]:
    """Copy each missing doc-{sha[:12]}/ folder from orphan to canonical.

    Returns the list of document records (manifest-shape) for the new
    PDFs so the caller can append them to the canonical manifest's
    documents array.
    """
    bucket = client.bucket(BUCKET_NAME)
    new_records: list[dict] = []
    for sha12 in unique_shas:
        orphan_doc_prefix = f"{orphan_prefix}/doc-{sha12}"
        canonical_doc_prefix = f"{canonical_prefix}/doc-{sha12}"
        # Skip if (improbably) the canonical already has this doc folder.
        if bucket.blob(f"{canonical_doc_prefix}/original.pdf").exists():
            continue
        # Server-side copy every blob under orphan_doc_prefix/.
        copied_any = False
        for blob in client.list_blobs(bucket, prefix=orphan_doc_prefix + "/"):
            rel = blob.name[len(orphan_doc_prefix) + 1 :]
            new_name = f"{canonical_doc_prefix}/{rel}"
            bucket.copy_blob(blob, bucket, new_name=new_name)
            copied_any = True
        if not copied_any:
            continue

        # Reconstruct a document record from precheck.json + GCS metadata.
        precheck = {}
        precheck_blob = bucket.blob(f"{canonical_doc_prefix}/precheck.json")
        if precheck_blob.exists():
            try:
                precheck = json.loads(precheck_blob.download_as_bytes())
            except Exception:
                precheck = {}
        pdf_blob = bucket.blob(f"{canonical_doc_prefix}/original.pdf")
        try:
            pdf_blob.reload()
            size_bytes = pdf_blob.size
        except Exception:
            size_bytes = None
        new_records.append({
            "doc_id": sha12,
            "sha256": precheck.get("sha256"),
            "size_bytes": precheck.get("file_size_bytes") or size_bytes,
            "page_count": precheck.get("page_count"),
            "text_extractable": precheck.get("text_extractable"),
            "text_extractable_hint": None,
            "suggested_category": precheck.get("suggested_category"),
            "category_hint": None,
            "is_pii_risk": precheck.get("is_pii_risk"),
            "is_junk": precheck.get("is_junk"),
            "source_url": None,  # original orphan record had this; we drop it.
            "filename": precheck.get("filename"),
            "fetched_at": None,
            "gcs_path": f"gs://{BUCKET_NAME}/{canonical_doc_prefix}/original.pdf",
        })
    return new_records


def _append_documents_to_manifest(
    client: gcs.Client,
    canonical_prefix: str,
    new_records: list[dict],
) -> None:
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(f"{canonical_prefix}/manifest.json")
    try:
        manifest = json.loads(blob.download_as_bytes())
    except Exception:
        return
    existing_ids = {d.get("doc_id") for d in manifest.get("documents") or []}
    docs = manifest.get("documents") or []
    for rec in new_records:
        if rec["doc_id"] in existing_ids:
            continue
        docs.append(rec)
    manifest["documents"] = docs
    blob.upload_from_string(
        json.dumps(manifest, indent=2, sort_keys=True),
        content_type="application/json",
    )


def parse_collisions(log_path: Path) -> list[dict]:
    rows: list[dict] = []
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") != "collision":
                continue
            if not row.get("slug") or not row.get("county") or not row.get("new_slug"):
                continue
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete orphan _unknown-county/ manifests whose duplicate exists at the right prefix")
    parser.add_argument("log_path", help="Path to a cleanup-pass log (jsonl per row).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--merge", action="store_true",
                        help="Merge unique-PDF orphans into the canonical manifest before deleting (instead of refusing).")
    args = parser.parse_args()

    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "hoaware"

    client = gcs.Client()
    bucket = client.bucket(BUCKET_NAME)
    rows = parse_collisions(Path(args.log_path))
    print(f"Found {len(rows)} collision rows in {args.log_path}", file=sys.stderr)

    summary: dict[str, int] = {}
    for i, row in enumerate(rows, 1):
        orphan_prefix = f"v1/GA/_unknown-county/{row['slug']}"
        county_slug = slugify(row["county"])
        canonical_prefix = f"v1/GA/{county_slug}/{row['new_slug']}"

        if not bucket.blob(f"{orphan_prefix}/manifest.json").exists():
            summary["orphan_already_gone"] = summary.get("orphan_already_gone", 0) + 1
            print(json.dumps({"i": i, **{k: v for k, v in row.items() if k != "status"}, "status": "orphan_already_gone"}))
            continue
        if not bucket.blob(f"{canonical_prefix}/manifest.json").exists():
            summary["canonical_missing"] = summary.get("canonical_missing", 0) + 1
            print(json.dumps({"i": i, **{k: v for k, v in row.items() if k != "status"}, "status": "canonical_missing"}))
            continue

        orphan_shas = list_orphan_shas(client, orphan_prefix)
        canonical_shas = list_canonical_shas(client, canonical_prefix)

        # If the orphan has PDFs the canonical lacks, merge them
        # before deleting (with --merge). Otherwise refuse so we don't
        # lose data.
        unique_to_orphan = orphan_shas - canonical_shas
        if unique_to_orphan and not args.merge:
            summary["has_unique_pdfs"] = summary.get("has_unique_pdfs", 0) + 1
            print(json.dumps({
                "i": i,
                **{k: v for k, v in row.items() if k != "status"},
                "status": "has_unique_pdfs",
                "unique_to_orphan": list(unique_to_orphan)[:5],
            }))
            continue

        if args.dry_run:
            label = "dry_would_merge_then_delete" if unique_to_orphan else "dry_would_delete"
            summary[label] = summary.get(label, 0) + 1
            print(json.dumps({
                "i": i, **{k: v for k, v in row.items() if k != "status"},
                "status": label,
                "unique_to_orphan": list(unique_to_orphan)[:5] if unique_to_orphan else [],
            }))
            continue

        # Merge: copy each missing doc-{sha[:12]}/ folder into canonical.
        merged_docs = 0
        if unique_to_orphan:
            new_doc_records = _merge_unique_pdfs(
                client, orphan_prefix, canonical_prefix, unique_to_orphan,
            )
            if new_doc_records:
                _append_documents_to_manifest(client, canonical_prefix, new_doc_records)
                merged_docs = len(new_doc_records)

        deleted = delete_prefix(client, orphan_prefix)
        label = "merged_and_deleted" if merged_docs else "deleted"
        summary[label] = summary.get(label, 0) + 1
        print(json.dumps({
            "i": i, **{k: v for k, v in row.items() if k != "status"},
            "status": label,
            "deleted": deleted,
            "merged_docs": merged_docs,
        }))

    print(json.dumps({"summary": summary}, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
