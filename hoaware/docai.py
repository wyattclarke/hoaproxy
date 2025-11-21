from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import List

from google.api_core.client_options import ClientOptions
from google.cloud import documentai
from pypdf import PdfReader, PdfWriter

from .chunker import PageContent

logger = logging.getLogger(__name__)


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


def extract_with_document_ai(
    path: Path,
    project_id: str,
    location: str,
    processor_id: str,
    endpoint: str | None = None,
    max_pages_per_call: int = 10,
) -> List[PageContent]:
    reader = PdfReader(str(path))
    total_pages = len(reader.pages)
    if total_pages == 0:
        return []

    client_options = ClientOptions(api_endpoint=endpoint or f"{location}-documentai.googleapis.com")
    client = documentai.DocumentProcessorServiceClient(client_options=client_options)
    processor_name = client.processor_path(project_id, location, processor_id)

    all_pages: list[PageContent] = []

    for start in range(1, total_pages + 1, max_pages_per_call):
        end = min(start + max_pages_per_call - 1, total_pages)
        pdf_bytes = _build_pdf_chunk(reader, start, end)
        raw_document = documentai.RawDocument(content=pdf_bytes, mime_type="application/pdf")
        request = documentai.ProcessRequest(name=processor_name, raw_document=raw_document)
        try:
            result = client.process_document(request=request)
        except Exception:
            logger.exception("Document AI OCR failed for %s pages %s-%s", path, start, end)
            continue

        document = result.document
        for page in document.pages:
            number = start + (page.page_number - 1)
            text = _page_text(document, page)
            all_pages.append(PageContent(number=number, text=text))

    all_pages.sort(key=lambda p: p.number)
    return all_pages
