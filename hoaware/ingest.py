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
from .chunker import chunk_pages
from .config import Settings, load_settings, normalize_hoa_name
from .embeddings import batch_embeddings
from .pdf_utils import compute_checksum, extract_pages
from .vector_store import (
    build_client,
    delete_points,
    ensure_collection,
    points_exist,
    upsert_chunks,
)

logger = logging.getLogger(__name__)
console = Console()


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
    extract_start = perf_counter()
    pages = extract_pages(
        pdf_path,
        enable_ocr=settings.enable_ocr,
        ocr_dpi=settings.ocr_dpi,
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

    embeddings = batch_embeddings(
        [chunk.text for chunk in chunks],
        client=openai_client,
        model=settings.embedding_model,
    )
    logger.info("Embeddings complete for %s (chunks=%s)", rel_path, len(embeddings))
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
) -> IngestStats:
    settings = settings or load_settings()
    paths = list(pdf_paths)
    stats = IngestStats()
    if not paths:
        return stats
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for ingestion.")

    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, hoa_name)
        openai_client = OpenAI(api_key=settings.openai_api_key)
        qdrant_client = build_client(
            settings.qdrant_url,
            settings.qdrant_api_key,
            local_path=settings.qdrant_local_path,
        )
        collection = f"{settings.collection_prefix}_{normalize_hoa_name(hoa_name)}"
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
