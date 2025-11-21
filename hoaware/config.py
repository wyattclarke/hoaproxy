from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass
class Settings:
    docs_root: Path
    db_path: Path
    qdrant_url: str
    qdrant_api_key: str | None
    openai_api_key: str | None
    embedding_model: str
    chunk_char_limit: int
    chunk_overlap: int
    collection_prefix: str
    enable_ocr: bool
    ocr_dpi: int
    enable_docai: bool
    docai_project_id: str | None
    docai_location: str
    docai_processor_id: str | None
    docai_endpoint: str | None
    docai_chunk_pages: int


def load_settings() -> Settings:
    docs_root = Path(os.environ.get("HOA_DOCS_ROOT", "casnc_hoa_docs"))
    db_path = Path(os.environ.get("HOA_DB_PATH", "data/hoa_index.db"))
    return Settings(
        docs_root=docs_root,
        db_path=db_path,
        qdrant_url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        qdrant_api_key=os.environ.get("QDRANT_API_KEY"),
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        embedding_model=os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        chunk_char_limit=int(os.environ.get("HOA_CHUNK_CHAR_LIMIT", "1800")),
        chunk_overlap=int(os.environ.get("HOA_CHUNK_OVERLAP", "200")),
        collection_prefix=os.environ.get("HOA_QDRANT_PREFIX", "hoa"),
        enable_ocr=os.environ.get("HOA_ENABLE_OCR", "1") not in {"0", "false", "False"},
        ocr_dpi=int(os.environ.get("HOA_OCR_DPI", "300")),
        enable_docai=os.environ.get("HOA_ENABLE_DOCAI", "0") not in {"0", "false", "False"},
        docai_project_id=os.environ.get("HOA_DOCAI_PROJECT_ID"),
        docai_location=os.environ.get("HOA_DOCAI_LOCATION", "us"),
        docai_processor_id=os.environ.get("HOA_DOCAI_PROCESSOR_ID"),
        docai_endpoint=os.environ.get("HOA_DOCAI_ENDPOINT"),
        docai_chunk_pages=int(os.environ.get("HOA_DOCAI_CHUNK_PAGES", "10")),
    )


def normalize_hoa_name(name: str) -> str:
    """Create a consistent slug for Qdrant namespaces."""
    slug = name.strip().lower().replace(" ", "_")
    return "".join(ch for ch in slug if ch.isalnum() or ch in ("_", "-"))
