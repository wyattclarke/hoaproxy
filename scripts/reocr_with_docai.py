#!/usr/bin/env python3
"""Re-OCR documents flagged by score_ocr_quality.py using Document AI.

PR-4 of the agent-paradigm cleanup. Pairs with `score_ocr_quality.py`:

    python scripts/score_ocr_quality.py --category ccr --limit 0 --output data/reocr_candidates.json
    python scripts/reocr_with_docai.py --manifest data/reocr_candidates.json --max-cost-usd 50

For each document in the manifest:
  - Reads the original PDF from ${HOA_DOCS_ROOT}/${HOA}/${file}
  - Calls DocAI OCR on the whole PDF (the agent's text_extractable=False path)
  - Re-chunks, re-embeds via OpenAI, replaces chunks/embeddings in SQLite
  - Logs cost via the existing api_usage tracker

Stops when the running estimated cost exceeds --max-cost-usd to prevent
runaway spend. Use --dry-run to print the plan and cost estimate without
making any DocAI calls.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openai import OpenAI

from hoaware import db
from hoaware.chunker import chunk_pages
from hoaware.config import UNIFIED_COLLECTION, load_settings
from hoaware.cost_tracker import COST_DOCAI_PER_PAGE
from hoaware.embeddings import batch_embeddings
from hoaware.pdf_utils import compute_checksum, extract_pages
from hoaware.vector_store import (
    build_client,
    delete_points,
    ensure_collection,
    upsert_chunks,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("reocr")


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-OCR low-quality docs via DocAI")
    ap.add_argument("--manifest", type=Path, required=True,
                    help="JSON manifest from score_ocr_quality.py")
    ap.add_argument("--max-cost-usd", type=float, default=50.0,
                    help="Hard ceiling on total est. DocAI cost (default $50)")
    ap.add_argument("--dry-run", action="store_true", help="Print plan + cost; do nothing")
    args = ap.parse_args()

    if not args.manifest.exists():
        print(f"ERROR: manifest not found: {args.manifest}", file=sys.stderr)
        return 1

    candidates = json.loads(args.manifest.read_text())
    print(f"Loaded {len(candidates)} candidates from {args.manifest}")

    settings = load_settings()
    if not settings.enable_docai or not settings.docai_project_id or not settings.docai_processor_id:
        print("ERROR: DocAI is not configured (HOA_ENABLE_DOCAI / HOA_DOCAI_PROJECT_ID / "
              "HOA_DOCAI_PROCESSOR_ID).", file=sys.stderr)
        return 1
    if not settings.openai_api_key:
        print("ERROR: OPENAI_API_KEY required for re-embedding.", file=sys.stderr)
        return 1

    # Filter to candidates with a known page_count and a file on disk
    runnable: list[dict] = []
    skipped_missing = 0
    for c in candidates:
        pdf_path = settings.docs_root / c["relative_path"]
        if not pdf_path.exists():
            skipped_missing += 1
            continue
        runnable.append({**c, "_pdf_path": pdf_path})

    print(f"Runnable: {len(runnable)}  (skipped {skipped_missing} with missing files)")

    # Cost gate
    total_pages = sum((c.get("page_count") or 0) for c in runnable)
    total_est = round(total_pages * COST_DOCAI_PER_PAGE, 2)
    print(f"Estimated DocAI cost if all runnable: {total_pages:,} pages → ${total_est:.2f}")
    if total_est > args.max_cost_usd:
        print(f"Plan exceeds --max-cost-usd ${args.max_cost_usd}; will process docs in"
              f" priority order until budget exhausted.")

    if args.dry_run:
        print("Dry-run; exiting.")
        return 0

    openai_client = OpenAI(api_key=settings.openai_api_key)
    qdrant_client = build_client(
        settings.qdrant_url,
        settings.qdrant_api_key,
        local_path=settings.qdrant_local_path,
    )
    ensure_collection(qdrant_client, UNIFIED_COLLECTION)

    spent_pages = 0
    spent_usd = 0.0
    indexed = 0
    failed = 0

    with db.get_connection(settings.db_path) as conn:
        for c in runnable:
            pages_for_doc = c.get("page_count") or 0
            doc_cost = pages_for_doc * COST_DOCAI_PER_PAGE
            if spent_usd + doc_cost > args.max_cost_usd:
                print(f"Budget reached at ${spent_usd:.2f}; stopping. "
                      f"Skipping remaining {len(runnable) - indexed - failed} docs.")
                break

            pdf_path = c["_pdf_path"]
            hoa_name = c["hoa"]
            rel_path = c["relative_path"]
            doc_id = int(c["doc_id"])
            print(f"  [{indexed + failed + 1}] {hoa_name} / {rel_path} "
                  f"({pages_for_doc}p, est ${doc_cost:.4f})", flush=True)

            try:
                pages = extract_pages(
                    pdf_path,
                    text_extractable=False,
                    enable_docai=True,
                    docai_project_id=settings.docai_project_id,
                    docai_location=settings.docai_location,
                    docai_processor_id=settings.docai_processor_id,
                    docai_endpoint=settings.docai_endpoint,
                    docai_chunk_pages=settings.docai_chunk_pages,
                )
                spent_pages += pages_for_doc
                spent_usd += doc_cost

                chunks = chunk_pages(
                    pages,
                    max_chars=settings.chunk_char_limit,
                    overlap_chars=settings.chunk_overlap,
                )
                if not chunks:
                    print(f"     no chunks produced; skipping embedding")
                    failed += 1
                    continue

                import numpy as np
                embeddings = batch_embeddings(
                    [ch.text for ch in chunks],
                    client=openai_client,
                    model=settings.embedding_model,
                )
                embedding_blobs = [
                    np.array(vec, dtype=np.float32).tobytes() for vec in embeddings
                ]

                # Replace Qdrant points (best effort)
                old_point_ids = db.list_chunk_point_ids(conn, doc_id)
                point_ids = [""] * len(chunks)
                try:
                    payloads = [
                        (
                            ch.text, vec,
                            {
                                "hoa": hoa_name,
                                "document": rel_path,
                                "chunk_index": ch.index,
                                "start_page": ch.start_page,
                                "end_page": ch.end_page,
                                "text": ch.text,
                            },
                        )
                        for ch, vec in zip(chunks, embeddings, strict=True)
                    ]
                    point_ids = upsert_chunks(qdrant_client, UNIFIED_COLLECTION, payloads)
                    delete_points(qdrant_client, UNIFIED_COLLECTION, old_point_ids)
                except Exception:
                    logger.info("Qdrant upsert skipped; embeddings live in SQLite only")

                db.replace_chunks(
                    conn,
                    doc_id,
                    [
                        (ch.index, ch.start_page, ch.end_page, ch.text, pid)
                        for ch, pid in zip(chunks, point_ids, strict=True)
                    ],
                    embeddings=embedding_blobs,
                )
                # Update checksum since extraction method changed (chunks differ)
                checksum = compute_checksum(pdf_path)
                conn.execute(
                    "UPDATE documents SET checksum = ?, last_ingested = CURRENT_TIMESTAMP WHERE id = ?",
                    (checksum, doc_id),
                )
                conn.commit()

                indexed += 1
            except Exception as exc:
                logger.exception("Re-OCR failed for %s/%s: %s", hoa_name, rel_path, exc)
                failed += 1

    print(f"\nDone. Indexed: {indexed}, Failed: {failed}, "
          f"DocAI pages used: {spent_pages:,} (~${spent_usd:.2f})")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
