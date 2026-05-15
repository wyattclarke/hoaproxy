#!/usr/bin/env python3
"""Bake a single v1 prepared bundle into v2 with pre-computed embeddings.

Reads a bundle's existing v1 manifest + text sidecars from GCS, calls OpenAI
to embed the chunks, writes one ``chunks-{sha256}.json`` sidecar per
document next to the bundle, then rewrites ``bundle.json`` to schema v2 with
``chunks_gcs_path`` set on each document.

Idempotent: if a chunks sidecar already exists for a doc sha and validates
cleanly, skip re-embedding it. Re-running on an already-v2 bundle is a no-op.

Usage::

    set -a; source settings.env; set +a
    .venv/bin/python scripts/prepare/bake_bundle.py \\
        --prefix v1/FL/palm-beach/pine-point/9920c4b70217f09a

Cost: ~$0.00002 / 1k tokens × text-embedding-3-small. A 50-chunk doc
(~50 × 1500 chars) ≈ 18k tokens ≈ $0.0004 per doc.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import storage as gcs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hoaware import prepared_ingest  # noqa: E402
from hoaware.prepare.embed import bake_chunks_sidecar  # noqa: E402
from openai import OpenAI  # noqa: E402

DEFAULT_BUCKET = os.environ.get(
    "HOA_PREPARED_GCS_BUCKET", prepared_ingest.DEFAULT_PREPARED_BUCKET
)
DEFAULT_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")


def bake_one_bundle(
    *,
    bucket: gcs.Bucket,
    prefix: str,
    embedding_model: str,
    chunk_char_limit: int,
    chunk_overlap: int,
    openai_client: OpenAI,
    force: bool = False,
) -> dict:
    """Bake a single bundle prefix; return outcome stats."""
    bundle_blob_name = prepared_ingest.bundle_blob_name(prefix)
    bundle_payload = prepared_ingest.load_json_blob(bucket, bundle_blob_name)
    bundle = prepared_ingest.validate_bundle(bundle_payload)

    docs_baked = 0
    docs_skipped = 0
    new_chunks_paths: dict[str, str] = {}  # sha → gs:// URI
    for doc in bundle.documents:
        chunks_blob_name = prepared_ingest.chunks_sidecar_blob_name(prefix, doc.sha256)
        chunks_blob = bucket.blob(chunks_blob_name)

        if not force and chunks_blob.exists():
            try:
                existing = json.loads(chunks_blob.download_as_bytes())
                sidecar = prepared_ingest.validate_chunks_sidecar(
                    existing,
                    expected_sha256=doc.sha256,
                    expected_model=embedding_model,
                )
                new_chunks_paths[doc.sha256] = prepared_ingest.gcs_uri(
                    bucket.name, chunks_blob_name
                )
                docs_skipped += 1
                continue
            except prepared_ingest.PreparedIngestError:
                # Stale or wrong-model sidecar — re-bake.
                pass

        text_uri = prepared_ingest.parse_gcs_uri(doc.text_gcs_path)
        if text_uri.bucket != bucket.name:
            raise prepared_ingest.PreparedIngestError(
                f"text sidecar bucket mismatch: {text_uri.bucket} != {bucket.name}"
            )
        text_payload = prepared_ingest.load_json_blob(bucket, text_uri.blob)
        pages, _ = prepared_ingest.validate_text_sidecar(text_payload)

        sidecar_dict = bake_chunks_sidecar(
            doc_sha256=doc.sha256,
            pages=pages,
            embedding_model=embedding_model,
            chunk_char_limit=chunk_char_limit,
            chunk_overlap=chunk_overlap,
            openai_client=openai_client,
        )
        chunks_blob.upload_from_string(
            json.dumps(sidecar_dict, separators=(",", ":")),
            content_type="application/json",
        )
        new_chunks_paths[doc.sha256] = prepared_ingest.gcs_uri(
            bucket.name, chunks_blob_name
        )
        docs_baked += 1

    # Rewrite bundle.json to v2 with chunks_gcs_path set on each doc.
    updated = dict(bundle_payload)
    updated["schema_version"] = 2
    docs_out = []
    for doc_raw in updated.get("documents") or []:
        sha = (doc_raw.get("sha256") or "").lower()
        new_doc = dict(doc_raw)
        if sha in new_chunks_paths:
            new_doc["chunks_gcs_path"] = new_chunks_paths[sha]
        docs_out.append(new_doc)
    updated["documents"] = docs_out

    bucket.blob(bundle_blob_name).upload_from_string(
        json.dumps(updated, indent=2, sort_keys=True),
        content_type="application/json",
    )
    return {
        "prefix": prefix,
        "documents": len(bundle.documents),
        "baked": docs_baked,
        "skipped": docs_skipped,
        "status": "ok",
    }


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefix", required=True, help="Bundle prefix, e.g. v1/FL/...")
    ap.add_argument("--bucket", default=DEFAULT_BUCKET)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--chunk-char-limit",
                    type=int,
                    default=int(os.environ.get("HOA_CHUNK_CHAR_LIMIT", "1800")))
    ap.add_argument("--chunk-overlap",
                    type=int,
                    default=int(os.environ.get("HOA_CHUNK_OVERLAP", "200")))
    ap.add_argument("--force", action="store_true",
                    help="Re-bake even if a valid chunks sidecar already exists")
    args = ap.parse_args()

    client = gcs.Client()
    bucket = client.bucket(args.bucket)
    openai_client = OpenAI()

    result = bake_one_bundle(
        bucket=bucket,
        prefix=args.prefix,
        embedding_model=args.model,
        chunk_char_limit=args.chunk_char_limit,
        chunk_overlap=args.chunk_overlap,
        openai_client=openai_client,
        force=args.force,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
