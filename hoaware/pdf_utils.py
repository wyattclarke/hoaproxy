"""PDF text extraction.

Three routing modes, controlled by the agent's `text_extractable` hint:

  text_extractable=True   → PyPDF only. Never call DocAI.
  text_extractable=False  → DocAI only (skip PyPDF — agent already verified blank).
  text_extractable=None   → PyPDF first; DocAI for any blank pages.

DocAI is the sole OCR provider. Tesseract has been removed (see PR-1 / Phase C).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from time import perf_counter
from typing import Iterable

from pypdf import PdfReader

from .chunker import PageContent
from .docai import extract_with_document_ai

logger = logging.getLogger(__name__)


# Hard cap to refuse OCR on absurdly large PDFs (entire HOA archives accidentally
# bundled into one file). Real governing docs do not exceed this.
MAX_PAGES_FOR_OCR = 200


def _pypdf_pages(reader: PdfReader) -> tuple[list[PageContent], list[int]]:
    """Extract text via PyPDF. Returns (pages, blank_page_numbers)."""
    pages: list[PageContent] = []
    missing: list[int] = []
    for idx, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            logger.exception("PyPDF extract failed on page %s", idx)
            text = ""
        if not text.strip():
            missing.append(idx)
        pages.append(PageContent(number=idx, text=text))
    return pages, missing


def extract_pages(
    path: Path,
    *,
    text_extractable: bool | None = None,
    enable_docai: bool = False,
    docai_project_id: str | None = None,
    docai_location: str = "us",
    docai_processor_id: str | None = None,
    docai_endpoint: str | None = None,
    docai_chunk_pages: int = 10,
) -> list[PageContent]:
    start_time = perf_counter()
    reader = PdfReader(str(path))
    total_pages = len(reader.pages)
    docai_configured = bool(enable_docai and docai_project_id and docai_processor_id)

    logger.info(
        "Extract start for %s (pages=%s, text_extractable=%s, docai=%s)",
        path,
        total_pages,
        text_extractable,
        docai_configured,
    )

    # Mode 1: agent says text is extractable → PyPDF only, no OCR
    if text_extractable is True:
        pages, missing = _pypdf_pages(reader)
        if missing:
            logger.warning(
                "Agent said text_extractable=True but PyPDF found %d blank pages in %s",
                len(missing),
                path,
            )
        logger.info(
            "Extract end (pypdf-only) for %s (blank=%d/%d, elapsed_s=%.2f)",
            path,
            len(missing),
            total_pages,
            perf_counter() - start_time,
        )
        return pages

    # Mode 2: agent says scanned → DocAI for whole document, skip PyPDF
    if text_extractable is False:
        if not docai_configured:
            logger.warning(
                "Cannot OCR %s: text_extractable=False but DocAI not configured", path
            )
            return [PageContent(number=i, text="") for i in range(1, total_pages + 1)]
        if total_pages > MAX_PAGES_FOR_OCR:
            logger.warning(
                "Skipping OCR for %s: %d pages exceeds cap of %d",
                path,
                total_pages,
                MAX_PAGES_FOR_OCR,
            )
            return [PageContent(number=i, text="") for i in range(1, total_pages + 1)]
        try:
            docai_pages = extract_with_document_ai(
                path,
                project_id=docai_project_id,
                location=docai_location,
                processor_id=docai_processor_id,
                endpoint=docai_endpoint,
                max_pages_per_call=docai_chunk_pages,
            )
        except Exception:
            logger.exception("Document AI OCR failed for %s", path)
            return [PageContent(number=i, text="") for i in range(1, total_pages + 1)]
        # Fill any pages DocAI didn't return
        by_number = {p.number: p for p in docai_pages}
        result = [by_number.get(i, PageContent(number=i, text="")) for i in range(1, total_pages + 1)]
        logger.info(
            "Extract end (docai-only) for %s (returned=%d/%d, elapsed_s=%.2f)",
            path,
            len(docai_pages),
            total_pages,
            perf_counter() - start_time,
        )
        return result

    # Mode 3 (legacy/no hint): PyPDF first, DocAI for blank pages only
    pages, missing = _pypdf_pages(reader)
    logger.info(
        "PyPDF pass complete for %s (blank=%d/%d)",
        path,
        len(missing),
        total_pages,
    )

    if missing and docai_configured:
        if total_pages > MAX_PAGES_FOR_OCR:
            logger.warning(
                "Skipping OCR for %s: %d pages exceeds cap of %d",
                path,
                total_pages,
                MAX_PAGES_FOR_OCR,
            )
        else:
            ocr_start = perf_counter()
            try:
                docai_pages = extract_with_document_ai(
                    path,
                    project_id=docai_project_id,
                    location=docai_location,
                    processor_id=docai_processor_id,
                    endpoint=docai_endpoint,
                    max_pages_per_call=docai_chunk_pages,
                    page_numbers=missing,
                )
                for item in docai_pages:
                    if item.text.strip():
                        pages[item.number - 1] = item
                final_blank = sum(1 for p in pages if not p.text.strip())
                logger.info(
                    "DocAI fill complete for %s (input_blank=%d, output_blank=%d, elapsed_s=%.2f)",
                    path,
                    len(missing),
                    final_blank,
                    perf_counter() - ocr_start,
                )
            except Exception:
                logger.exception("Document AI OCR failed for %s", path)

    logger.info(
        "Extract end for %s (elapsed_s=%.2f)",
        path,
        perf_counter() - start_time,
    )
    return pages


def compute_checksum(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def detect_text_extractable(path: Path, min_chars: int = 50) -> bool:
    """Quick check used by the agent precheck. True if PyPDF gets meaningful text from page 1."""
    try:
        reader = PdfReader(str(path))
        if not reader.pages:
            return False
        text = reader.pages[0].extract_text() or ""
        return len(text.strip()) >= min_chars
    except Exception:
        return False
