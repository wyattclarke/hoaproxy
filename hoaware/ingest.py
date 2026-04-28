from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import logging
from time import perf_counter

from openai import OpenAI
from rich.console import Console
from rich.progress import Progress

from . import db
from .chunker import PageContent, chunk_pages
from .config import Settings, load_settings, normalize_hoa_name, UNIFIED_COLLECTION
from .embeddings import batch_embeddings
from .pdf_utils import compute_checksum, extract_pages
from .vector_store import (
    build_client,
    delete_points,
    ensure_collection,
    points_exist,
    upsert_chunks,
)

import json as _json

logger = logging.getLogger(__name__)
console = Console()

_PROXY_KEYWORDS = {"proxy", "proxies", "absentee ballot", "vote in person", "in-person voting"}


def _detect_proxy_rules(
    chunks: list,
    openai_client,
    hoa_id: int,
    conn,
) -> None:
    """Scan document chunks for proxy-related language and update HOA proxy_status.

    Uses GPT-4o-mini to classify whether the governing documents allow, prohibit,
    or are silent on proxy voting. Only runs when proxy-relevant text is found.
    Updates hoas.proxy_status and hoas.proxy_citation in-place.
    """
    relevant = [
        c.text for c in chunks
        if any(kw in c.text.lower() for kw in _PROXY_KEYWORDS)
    ]
    if not relevant:
        return

    excerpt = "\n\n---\n\n".join(relevant[:6])  # cap at 6 chunks to limit tokens
    prompt = (
        "You are analyzing excerpts from HOA (Homeowners Association) governing documents "
        "(bylaws, CC&Rs, or rules). Based only on these excerpts, determine whether proxy "
        "voting is:\n"
        '- "allowed": The documents explicitly permit members to vote by proxy\n'
        '- "not_allowed": The documents explicitly prohibit or restrict proxy voting, '
        "or require in-person voting\n"
        '- "unknown": The excerpts don\'t clearly address proxy voting\n\n'
        f"Excerpts:\n{excerpt}\n\n"
        'Respond with JSON only, no other text: '
        '{"status": "allowed" | "not_allowed" | "unknown", '
        '"citation": "exact quote from the text that supports your determination, or null"}'
    )
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content or ""
        # Strip markdown code fences if present
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = _json.loads(raw)
        status = result.get("status", "unknown")
        citation = result.get("citation") or None
        if status in ("allowed", "not_allowed", "unknown"):
            db.set_hoa_proxy_status(conn, hoa_id, status, citation)
            logger.info("HOA %d proxy_status set to '%s'", hoa_id, status)
    except Exception:
        logger.exception("Proxy rule detection failed for HOA %d", hoa_id)


@dataclass
class IngestStats:
    processed: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0


def list_pdfs(hoa_directory: Path) -> list[Path]:
    return sorted(hoa_directory.glob("*.pdf"))


def _ingest_pdf(
    *,
    pdf_path: Path,
    hoa_name: str,
    settings: Settings,
    conn,
    hoa_id: int,
    openai_client: OpenAI,
    qdrant_client,
    collection: str,
    category: str | None = None,
    text_extractable: bool | None = None,
    source_url: str | None = None,
    pre_extracted_pages: list[PageContent] | None = None,
) -> bool:
    ingest_start = perf_counter()
    rel_path = pdf_path.relative_to(settings.docs_root).as_posix()
    logger.info("Ingest start for %s (hoa=%s)", rel_path, hoa_name)
    checksum = compute_checksum(pdf_path)
    existing = db.get_document_record(conn, hoa_id, rel_path)
    force_reindex = False
    if existing is not None and str(existing["checksum"]) == checksum:
        existing_doc_id = int(existing["id"])
        existing_point_ids = db.list_chunk_point_ids(conn, existing_doc_id)
        if existing_point_ids and points_exist(qdrant_client, collection, existing_point_ids):
            logger.info("Ingest skip for %s (checksum unchanged and points present)", rel_path)
            return False
        logger.info(
            "Reindexing unchanged document %s because vector points are missing in %s",
            rel_path,
            collection,
        )
        force_reindex = True

    byte_size = pdf_path.stat().st_size
    if pre_extracted_pages is not None:
        pages = pre_extracted_pages
        logger.info(
            "Using agent-supplied extracted text for %s (pages=%s, server skipped extract_pages)",
            rel_path,
            len(pages),
        )
    else:
        extract_start = perf_counter()
        pages = extract_pages(
            pdf_path,
            text_extractable=text_extractable,
            enable_docai=settings.enable_docai,
            docai_project_id=settings.docai_project_id,
            docai_location=settings.docai_location,
            docai_processor_id=settings.docai_processor_id,
            docai_endpoint=settings.docai_endpoint,
            docai_chunk_pages=settings.docai_chunk_pages,
        )
        logger.info(
            "Text extraction complete for %s (pages=%s, elapsed_s=%.2f)",
            rel_path,
            len(pages),
            perf_counter() - extract_start,
        )
    page_count = len(pages)
    doc_id, changed = db.upsert_document(
        conn,
        hoa_id,
        rel_path,
        checksum,
        byte_size,
        page_count,
        category=category,
        text_extractable=text_extractable,
        source_url=source_url,
    )
    if not changed and not force_reindex:
        logger.info("Ingest skip for %s (document metadata unchanged)", rel_path)
        return False

    old_point_ids = db.list_chunk_point_ids(conn, doc_id)
    chunk_start = perf_counter()
    chunks = chunk_pages(
        pages,
        max_chars=settings.chunk_char_limit,
        overlap_chars=settings.chunk_overlap,
    )
    logger.info(
        "Chunking complete for %s (chunks=%s, elapsed_s=%.2f)",
        rel_path,
        len(chunks),
        perf_counter() - chunk_start,
    )
    if not chunks:
        logger.warning("No chunks extracted from %s", rel_path)
        delete_points(qdrant_client, collection, old_point_ids)
        db.replace_chunks(conn, doc_id, [])
        return True

    _detect_proxy_rules(chunks, openai_client, hoa_id, conn)

    import numpy as np

    embeddings = batch_embeddings(
        [chunk.text for chunk in chunks],
        client=openai_client,
        model=settings.embedding_model,
    )
    logger.info("Embeddings complete for %s (chunks=%s)", rel_path, len(embeddings))

    # Store embeddings as BLOBs in SQLite for inline vector search
    embedding_blobs = [np.array(vec, dtype=np.float32).tobytes() for vec in embeddings]

    # Qdrant upsert (optional, for backward compatibility during migration)
    point_ids = [""] * len(chunks)
    try:
        payloads = []
        for chunk, vector in zip(chunks, embeddings, strict=True):
            payloads.append(
                (
                    chunk.text,
                    vector,
                    {
                        "hoa": hoa_name,
                        "document": rel_path,
                        "chunk_index": chunk.index,
                        "start_page": chunk.start_page,
                        "end_page": chunk.end_page,
                        "text": chunk.text,
                    },
                )
            )
        point_ids = upsert_chunks(qdrant_client, collection, payloads)
        delete_points(qdrant_client, collection, old_point_ids)
    except Exception:
        logger.info("Qdrant upsert skipped (not available); embeddings stored in SQLite")

    db.replace_chunks(
        conn,
        doc_id,
        [
            (
                chunk.index,
                chunk.start_page,
                chunk.end_page,
                chunk.text,
                point_id,
            )
            for chunk, point_id in zip(chunks, point_ids, strict=True)
        ],
        embeddings=embedding_blobs,
    )
    logger.info(
        "Ingest end for %s (bytes=%s, chunks=%s, elapsed_s=%.2f)",
        rel_path,
        byte_size,
        len(chunks),
        perf_counter() - ingest_start,
    )
    return True


def ingest_pdf_paths(
    hoa_name: str,
    pdf_paths: Iterable[Path],
    settings: Settings | None = None,
    *,
    show_progress: bool = False,
    metadata_by_path: dict[Path, dict] | None = None,
) -> IngestStats:
    """Ingest PDFs. `metadata_by_path` may carry per-file agent hints:
    {path: {"category": str, "text_extractable": bool, "source_url": str,
            "pre_extracted_pages": list[PageContent]}}

    When `pre_extracted_pages` is supplied, the server skips its own text
    extraction (PyPDF/DocAI) and trusts the agent's pages directly. Used to
    move the OCR memory load off the API host.
    """
    settings = settings or load_settings()
    paths = list(pdf_paths)
    stats = IngestStats()
    if not paths:
        return stats
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for ingestion.")

    metadata_by_path = metadata_by_path or {}

    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, hoa_name)
        openai_client = OpenAI(api_key=settings.openai_api_key)
        qdrant_client = build_client(
            settings.qdrant_url,
            settings.qdrant_api_key,
            local_path=settings.qdrant_local_path,
        )
        collection = UNIFIED_COLLECTION
        ensure_collection(qdrant_client, collection)

        progress_context = Progress(console=console) if show_progress else nullcontext()
        with progress_context as progress:
            task_id = (
                progress.add_task(f"Ingesting {hoa_name}", total=len(paths))
                if show_progress and progress is not None
                else None
            )
            for pdf_path in paths:
                stats.processed += 1
                if show_progress and progress is not None and task_id is not None:
                    progress.update(task_id, description=f"Processing {pdf_path.name}")
                meta = metadata_by_path.get(pdf_path) or {}
                try:
                    changed = _ingest_pdf(
                        pdf_path=pdf_path,
                        hoa_name=hoa_name,
                        settings=settings,
                        conn=conn,
                        hoa_id=hoa_id,
                        openai_client=openai_client,
                        qdrant_client=qdrant_client,
                        collection=collection,
                        category=meta.get("category"),
                        text_extractable=meta.get("text_extractable"),
                        source_url=meta.get("source_url"),
                        pre_extracted_pages=meta.get("pre_extracted_pages"),
                    )
                    if changed:
                        stats.indexed += 1
                    else:
                        stats.skipped += 1
                except Exception:
                    logger.exception("Failed to ingest %s for HOA %s", pdf_path, hoa_name)
                    rel_path = pdf_path.relative_to(settings.docs_root).as_posix()
                    db.mark_document_for_reindex(conn, hoa_id, rel_path)
                    stats.failed += 1
                if show_progress and progress is not None and task_id is not None:
                    progress.advance(task_id)
    return stats


def ingest_hoa(hoa_name: str, settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    hoa_dir = settings.docs_root / hoa_name
    if not hoa_dir.exists():
        raise FileNotFoundError(f"HOA folder {hoa_dir} does not exist")

    pdf_paths = list_pdfs(hoa_dir)
    if not pdf_paths:
        console.print(f"[yellow]No PDFs found for {hoa_name}[/yellow]")
        return

    stats = ingest_pdf_paths(hoa_name, pdf_paths, settings=settings, show_progress=True)
    console.print(
        f"[green]Completed ingestion for {hoa_name}[/green] "
        f"(indexed={stats.indexed}, skipped={stats.skipped}, failed={stats.failed})"
    )
