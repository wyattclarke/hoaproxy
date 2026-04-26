from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / "settings.env", override=False)
load_dotenv(_REPO_ROOT / ".env", override=False)


@dataclass
class Settings:
    docs_root: Path
    db_path: Path
    qdrant_url: str
    qdrant_api_key: str | None
    qdrant_local_path: Path
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
    legal_source_map_path: Path
    legal_corpus_root: Path
    jwt_secret: str
    jwt_algorithm: str
    jwt_expiry_days: int
    # Email delivery
    email_provider: str   # "stub" | "resend" | "smtp"
    email_from: str
    resend_api_key: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    # Data retention
    proxy_retention_days: int
    # App base URL for email links
    app_base_url: str
    # Cost report
    cost_report_email: str | None
    ga4_property_id: str | None
    # Google OAuth
    google_client_id: str | None
    google_client_secret: str | None
    # Q&A LLM (chat completions). Defaults to Groq + Llama-3.3-70B for fast,
    # cheap, OpenAI-compatible inference. Override via env to use any
    # OpenAI-compatible endpoint (Cerebras, Fireworks, DeepInfra, OpenAI, etc.).
    qa_api_base_url: str
    qa_api_key: str | None
    qa_model: str


def load_settings() -> Settings:
    docs_root = Path(os.environ.get("HOA_DOCS_ROOT", "hoa_docs"))
    db_path = Path(os.environ.get("HOA_DB_PATH", "data/hoa_index.db"))
    return Settings(
        docs_root=docs_root,
        db_path=db_path,
        qdrant_url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        qdrant_api_key=os.environ.get("QDRANT_API_KEY"),
        qdrant_local_path=Path(os.environ.get("HOA_QDRANT_LOCAL_PATH", "data/qdrant_local")),
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
        legal_source_map_path=Path(os.environ.get("HOA_LEGAL_SOURCE_MAP_PATH", "data/legal/source_map.json")),
        legal_corpus_root=Path(os.environ.get("HOA_LEGAL_CORPUS_ROOT", "legal_corpus")),
        jwt_secret=os.environ.get("JWT_SECRET", "dev-secret-change-in-production"),
        jwt_algorithm=os.environ.get("JWT_ALGORITHM", "HS256"),
        jwt_expiry_days=int(os.environ.get("JWT_EXPIRY_DAYS", "30")),
        email_provider=os.environ.get("EMAIL_PROVIDER", "stub"),
        email_from=os.environ.get("EMAIL_FROM", "noreply@hoaproxy.org"),
        resend_api_key=os.environ.get("RESEND_API_KEY"),
        smtp_host=os.environ.get("SMTP_HOST"),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_user=os.environ.get("SMTP_USER"),
        smtp_password=os.environ.get("SMTP_PASSWORD"),
        proxy_retention_days=int(os.environ.get("PROXY_RETENTION_DAYS", "90")),
        app_base_url=os.environ.get("APP_BASE_URL", "https://hoaproxy.org"),
        cost_report_email=os.environ.get("COST_REPORT_EMAIL"),
        ga4_property_id=os.environ.get("GA4_PROPERTY_ID"),
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID"),
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
        qa_api_base_url=os.environ.get("QA_API_BASE_URL", "https://api.groq.com/openai/v1"),
        qa_api_key=os.environ.get("QA_API_KEY"),
        qa_model=os.environ.get("QA_MODEL", "llama-3.3-70b-versatile"),
    )


def normalize_hoa_name(name: str) -> str:
    """Create a consistent slug for Qdrant namespaces."""
    slug = name.strip().lower().replace(" ", "_")
    return "".join(ch for ch in slug if ch.isalnum() or ch in ("_", "-"))


# Single unified Qdrant collection for all HOAs.
# Using one collection instead of per-HOA collections reduces memory usage
# from O(n_hoas) to O(1) — critical for the 512 MB Render instance.
UNIFIED_COLLECTION = "hoa_all"
