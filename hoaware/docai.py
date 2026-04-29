from __future__ import annotations

import io
import logging
from pathlib import Path
from time import perf_counter
from typing import List

from google.api_core import exceptions as gcp_exceptions
from google.api_core.client_options import ClientOptions
from google.cloud import documentai
from pypdf import PdfReader, PdfWriter

from .chunker import PageContent
from .cost_tracker import log_docai_usage

logger = logging.getLogger(__name__)


class OCRFailedError(RuntimeError):
    """Raised when OCR is required but cannot produce usable text.

    `reason` is one of: 'not_configured', 'page_cap_exceeded',
    'quota_exceeded', 'docai_failed'. Lets the caller distinguish
    transient (quota) from permanent (page_cap_exceeded) failures.
    """

    def __init__(self, reason: str, message: str | None = None):
        super().__init__(message or reason)
        self.reason = reason


def _extract_layout_text(text: str, layout: documentai.Document.Page.Layout) -> str:
    if not layout.text_anchor:
        return ""
    segments: list[str] = []
    for segment in layout.text_anchor.text_segments:
        start = int(segment.start_index or 0)
        end = int(segment.end_index)
        segments.append(text[start:end])
    return "".join(segments).strip()


def _page_text(doc: documentai.Document, page: documentai.Document.Page) -> str:
    if page.paragraphs:
        parts = [_extract_layout_text(doc.text, paragraph.layout) for paragraph in page.paragraphs]
    elif page.lines:
        parts = [_extract_layout_text(doc.text, line.layout) for line in page.lines]
    else:
        parts = [_extract_layout_text(doc.text, page.layout)]
    return "\n".join(part for part in parts if part).strip()


def _build_pdf_chunk(reader: PdfReader, start_page: int, end_page: int) -> bytes:
    writer = PdfWriter()
    for page_number in range(start_page - 1, end_page):
        writer.add_page(reader.pages[page_number])
    buffer = io.BytesIO()
    writer.write(buffer)
    buffer.seek(0)
    return buffer.read()


def _build_pdf_subset(reader: PdfReader, page_numbers: list[int]) -> bytes:
    """Build a PDF containing only the specified 1-indexed pages, in order."""
    writer = PdfWriter()
    for page_number in page_numbers:
        writer.add_page(reader.pages[page_number - 1])
    buffer = io.BytesIO()
    writer.write(buffer)
    buffer.seek(0)
    return buffer.read()


def extract_with_document_ai(
    path: Path,
    project_id: str,
    location: str,
    processor_id: str,
    endpoint: str | None = None,
    max_pages_per_call: int = 10,
    page_numbers: list[int] | None = None,
) -> List[PageContent]:
    """OCR a PDF (or specific pages) via Google Document AI.

    If `page_numbers` is provided, only those 1-indexed pages are sent.
    Returned PageContent.number reflects the original page number in the source PDF.
    """
    overall_start = perf_counter()
    reader = PdfReader(str(path))
    total_pages = len(reader.pages)
    if total_pages == 0:
        return []

    if page_numbers is None:
        target_pages = list(range(1, total_pages + 1))
    else:
        target_pages = sorted({n for n in page_numbers if 1 <= n <= total_pages})
    if not target_pages:
        return []

    client_options = ClientOptions(api_endpoint=endpoint or f"{location}-documentai.googleapis.com")
    client = documentai.DocumentProcessorServiceClient(client_options=client_options)
    processor_name = client.processor_path(project_id, location, processor_id)

    all_pages: list[PageContent] = []
    logger.info(
        "Document AI OCR start for %s (target_pages=%d/%d, chunk_pages=%s, location=%s)",
        path,
        len(target_pages),
        total_pages,
        max_pages_per_call,
        location,
    )

    chunks_attempted = 0
    chunks_failed = 0
    last_error: Exception | None = None
    for chunk_start_idx in range(0, len(target_pages), max_pages_per_call):
        chunk_pages = target_pages[chunk_start_idx : chunk_start_idx + max_pages_per_call]
        chunk_start = perf_counter()
        chunks_attempted += 1
        pdf_bytes = _build_pdf_subset(reader, chunk_pages)
        raw_document = documentai.RawDocument(content=pdf_bytes, mime_type="application/pdf")
        request = documentai.ProcessRequest(name=processor_name, raw_document=raw_document)
        try:
            result = client.process_document(request=request)
        except gcp_exceptions.ResourceExhausted as exc:
            # Quota / rate limit — abort the whole document loudly. Continuing
            # would just produce a partially-OCR'd doc with no signal that
            # OCR was throttled.
            logger.error(
                "Document AI quota exhausted for %s pages %s..%s: %s",
                path, chunk_pages[0], chunk_pages[-1], exc,
            )
            raise OCRFailedError(
                "quota_exceeded",
                f"Document AI quota exhausted ({exc})",
            ) from exc
        except Exception as exc:
            chunks_failed += 1
            last_error = exc
            logger.exception(
                "Document AI OCR failed for %s pages %s..%s",
                path,
                chunk_pages[0],
                chunk_pages[-1],
            )
            continue

        document = result.document
        log_docai_usage(len(chunk_pages), document=str(path.name))
        returned = 0
        for page in document.pages:
            # page.page_number is 1-indexed within the submitted sub-PDF
            sub_idx = int(page.page_number) - 1
            if 0 <= sub_idx < len(chunk_pages):
                original_page_number = chunk_pages[sub_idx]
            else:
                continue
            text = _page_text(document, page)
            all_pages.append(PageContent(number=original_page_number, text=text))
            returned += 1
        logger.info(
            "Document AI chunk complete for %s (pages=%s..%s, returned=%s, elapsed_s=%.2f)",
            path,
            chunk_pages[0],
            chunk_pages[-1],
            returned,
            perf_counter() - chunk_start,
        )

    all_pages.sort(key=lambda p: p.number)
    logger.info(
        "Document AI OCR end for %s (pages_returned=%s, elapsed_s=%.2f)",
        path,
        len(all_pages),
        perf_counter() - overall_start,
    )
    # Every chunk failed — bubble up rather than returning empty. An empty
    # return upstream becomes a 0-chunk document that looks like success.
    if chunks_attempted > 0 and chunks_failed == chunks_attempted:
        raise OCRFailedError(
            "docai_failed",
            f"All {chunks_attempted} Document AI chunks failed for {path.name}: {last_error}",
        )
    return all_pages
