from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import logging
import shutil
from typing import Iterable

from pypdf import PdfReader
from pdf2image import convert_from_path
import pytesseract

from .chunker import PageContent
from .docai import extract_with_document_ai

logger = logging.getLogger(__name__)


def _tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def _ocr_page(path: Path, page_number: int, dpi: int) -> str:
    images = convert_from_path(
        str(path),
        dpi=dpi,
        first_page=page_number,
        last_page=page_number,
    )
    text_parts: list[str] = []
    for image in images:
        text_parts.append(pytesseract.image_to_string(image))
    return "\n".join(text_parts)


def extract_pages(
    path: Path,
    enable_ocr: bool = True,
    ocr_dpi: int = 300,
    enable_docai: bool = False,
    docai_project_id: str | None = None,
    docai_location: str = "us",
    docai_processor_id: str | None = None,
    docai_endpoint: str | None = None,
    docai_chunk_pages: int = 10,
) -> list[PageContent]:
    reader = PdfReader(str(path))
    pages: list[PageContent] = []
    missing: list[int] = []
    for idx, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # pragma: no cover - PyPDF may throw per page
            logger.exception("Failed to extract text from %s page %s", path, idx)
            text = ""
        if not text.strip():
            missing.append(idx)
        pages.append(PageContent(number=idx, text=text))

    if enable_docai and docai_project_id and docai_processor_id:
        try:
            docai_pages = extract_with_document_ai(
                path,
                project_id=docai_project_id,
                location=docai_location,
                processor_id=docai_processor_id,
                endpoint=docai_endpoint,
                max_pages_per_call=docai_chunk_pages,
            )
            for item in docai_pages:
                if item.text.strip():
                    pages[item.number - 1] = item
            missing = [p.number for p in pages if not p.text.strip()]
        except Exception:
            logger.exception("Document AI OCR failed for %s", path)

    if enable_ocr and missing:
        if not _tesseract_available():
            logger.warning("Tesseract not found; cannot OCR pages %s in %s", missing, path)
            return pages
        for page_number in missing:
            try:
                ocr_text = _ocr_page(path, page_number, dpi=ocr_dpi)
                if ocr_text.strip():
                    pages[page_number - 1] = PageContent(number=page_number, text=ocr_text)
                else:
                    logger.warning("OCR produced no text for %s page %s", path, page_number)
            except Exception:
                logger.exception("OCR failed for %s page %s", path, page_number)
    return pages


def compute_checksum(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()
