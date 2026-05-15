"""Build a v2 chunks sidecar (pre-chunked text + OpenAI embeddings).

Runs OUTSIDE Hetzner. Given a v1 prepared bundle's text sidecar (already
contains OCR-extracted pages), produce a v2 chunks sidecar that the live
server can ingest with zero external API calls.

Pure function: caller is responsible for IO (read text sidecar, write chunks
sidecar, upload to GCS). Keeps this module easy to unit-test.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import time

from openai import OpenAI, RateLimitError

from ..chunker import PageContent, chunk_pages
from ..embeddings import batch_embeddings


def _embed_with_retry(
    texts: list[str],
    *,
    client: OpenAI,
    model: str,
    max_retries: int = 5,
) -> list[list[float]]:
    """``batch_embeddings`` + exponential backoff on OpenAI 429s."""
    delay = 2.0
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return batch_embeddings(texts, client=client, model=model)
        except RateLimitError as e:
            last_err = e
            if attempt >= max_retries:
                break
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    assert last_err is not None
    raise last_err


def bake_chunks_sidecar(
    *,
    doc_sha256: str,
    pages: list[PageContent],
    embedding_model: str,
    embedding_dimensions: int = 1536,
    chunk_char_limit: int = 1800,
    chunk_overlap: int = 200,
    openai_client: OpenAI | None = None,
) -> dict[str, Any]:
    """Return the chunks-sidecar JSON-ready dict for one document.

    Caller writes the returned dict as ``chunks-{sha256}.json`` next to the
    bundle's existing ``bundle.json`` and ``texts/{sha256}.json``. The
    returned dict validates cleanly against
    ``hoaware.prepared_ingest.validate_chunks_sidecar``.
    """
    if not pages:
        raise ValueError("bake_chunks_sidecar: pages must be non-empty")
    if openai_client is None:
        openai_client = OpenAI()

    chunks = chunk_pages(pages, max_chars=chunk_char_limit, overlap_chars=chunk_overlap)
    if not chunks:
        raise ValueError(
            f"bake_chunks_sidecar: no chunks produced for doc {doc_sha256}"
        )

    texts = [c.text for c in chunks]
    embeddings = _embed_with_retry(
        texts, client=openai_client, model=embedding_model
    )
    if len(embeddings) != len(chunks):
        raise ValueError(
            f"bake_chunks_sidecar: embeddings count {len(embeddings)} "
            f"!= chunks count {len(chunks)}"
        )
    if any(len(vec) != embedding_dimensions for vec in embeddings):
        raise ValueError(
            f"bake_chunks_sidecar: embedding length mismatch "
            f"(expected {embedding_dimensions} per vector)"
        )

    return {
        "schema_version": 2,
        "doc_sha256": doc_sha256.lower(),
        "produced_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        "chunker": {
            "max_chars": chunk_char_limit,
            "overlap_chars": chunk_overlap,
        },
        "embedder": {
            "provider": "openai",
            "model": embedding_model,
            "dimensions": embedding_dimensions,
        },
        "chunks": [
            {
                "idx": c.index,
                "text": c.text,
                "page_start": c.start_page,
                "page_end": c.end_page,
                "embedding": list(vec),
            }
            for c, vec in zip(chunks, embeddings)
        ],
    }
