from __future__ import annotations

from pathlib import Path
from typing import Iterable
import logging

from openai import OpenAI
from rich.console import Console
from rich.progress import Progress

from . import db
from .chunker import chunk_pages
from .config import Settings, load_settings, normalize_hoa_name
from .embeddings import batch_embeddings
from .pdf_utils import compute_checksum, extract_pages
from .vector_store import build_client, ensure_collection, upsert_chunks

logger = logging.getLogger(__name__)
console = Console()


def list_pdfs(hoa_directory: Path) -> list[Path]:
    return sorted(hoa_directory.glob("*.pdf"))


def ingest_hoa(hoa_name: str, settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    hoa_dir = settings.docs_root / hoa_name
    if not hoa_dir.exists():
        raise FileNotFoundError(f"HOA folder {hoa_dir} does not exist")

    pdf_paths = list_pdfs(hoa_dir)
    if not pdf_paths:
        console.print(f"[yellow]No PDFs found for {hoa_name}[/yellow]")
        return

    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, hoa_name)
        openai_client = OpenAI(api_key=settings.openai_api_key)
        qdrant_client = build_client(settings.qdrant_url, settings.qdrant_api_key)
        collection = f"{settings.collection_prefix}_{normalize_hoa_name(hoa_name)}"
        ensure_collection(qdrant_client, collection)

        with Progress(console=console) as progress:
            task = progress.add_task(f"Ingesting {hoa_name}", total=len(pdf_paths))
            for pdf_path in pdf_paths:
                progress.update(task, description=f"Processing {pdf_path.name}")
                rel_path = pdf_path.relative_to(settings.docs_root).as_posix()
                checksum = compute_checksum(pdf_path)
                byte_size = pdf_path.stat().st_size
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
                page_count = len(pages)
                doc_id, changed = db.upsert_document(
                    conn,
                    hoa_id,
                    rel_path,
                    checksum,
                    byte_size,
                    page_count,
                )
                if not changed:
                    progress.advance(task)
                    continue

                chunks = chunk_pages(
                    pages,
                    max_chars=settings.chunk_char_limit,
                    overlap_chars=settings.chunk_overlap,
                )
                if not chunks:
                    logger.warning("No chunks extracted from %s", rel_path)
                    progress.advance(task)
                    continue

                embeddings = batch_embeddings(
                    [chunk.text for chunk in chunks],
                    client=openai_client,
                    model=settings.embedding_model,
                )
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
                progress.advance(task)
        console.print(f"[green]Completed ingestion for {hoa_name}[/green]")
