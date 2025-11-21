from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List


@dataclass
class PageContent:
    number: int
    text: str


@dataclass
class Chunk:
    index: int
    text: str
    start_page: int | None
    end_page: int | None


def chunk_pages(
    pages: Iterable[PageContent],
    max_chars: int = 1800,
    overlap_chars: int = 200,
) -> List[Chunk]:
    chunks: List[Chunk] = []
    buffer: list[str] = []
    start_page: int | None = None
    end_page: int | None = None
    current_length = 0
    index = 0

    def flush():
        nonlocal buffer, current_length, start_page, end_page, index
        if not buffer:
            return
        text = "\n".join(buffer).strip()
        if not text:
            buffer = []
            current_length = 0
            start_page = None
            end_page = None
            return
        chunks.append(
            Chunk(
                index=index,
                text=text,
                start_page=start_page,
                end_page=end_page,
            )
        )
        index += 1
        if overlap_chars > 0 and len(text) > overlap_chars:
            overlap_text = text[-overlap_chars:]
            buffer = [overlap_text]
            current_length = len(overlap_text)
            start_page = start_page if start_page == end_page else end_page
        else:
            buffer = []
            current_length = 0
            start_page = None
        end_page = None

    for page in pages:
        page_text = page.text.strip()
        if not page_text:
            continue
        if start_page is None:
            start_page = page.number
        end_page = page.number
        for paragraph in page_text.split("\n"):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if current_length + len(paragraph) + 1 > max_chars:
                flush()
            buffer.append(paragraph)
            current_length += len(paragraph) + 1
    flush()
    return chunks
