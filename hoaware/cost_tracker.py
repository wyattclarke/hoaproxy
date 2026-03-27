"""Cost tracking helpers — pricing constants and logging wrappers."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from hoaware import db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pricing constants (override via env vars when prices change)
# ---------------------------------------------------------------------------

# OpenAI text-embedding-3-small: $0.02 per 1M tokens
COST_OPENAI_EMBED_PER_1M = float(os.environ.get("COST_OPENAI_EMBED_PER_1M", "0.02"))
# OpenAI chat input/output (gpt-5-mini defaults; adjust for other models)
COST_OPENAI_CHAT_INPUT_PER_1M = float(os.environ.get("COST_OPENAI_CHAT_INPUT_PER_1M", "0.15"))
COST_OPENAI_CHAT_OUTPUT_PER_1M = float(os.environ.get("COST_OPENAI_CHAT_OUTPUT_PER_1M", "0.60"))
# Google Document AI OCR: $1.50 per 1000 pages
COST_DOCAI_PER_PAGE = float(os.environ.get("COST_DOCAI_PER_PAGE", "0.0015"))
# Resend: free tier 100/day, then $0.00 (adjust when on paid plan)
COST_RESEND_PER_EMAIL = float(os.environ.get("COST_RESEND_PER_EMAIL", "0.0"))
# SMTP: typically no per-email cost
COST_SMTP_PER_EMAIL = float(os.environ.get("COST_SMTP_PER_EMAIL", "0.0"))


def _get_db_path() -> Path:
    return Path(os.environ.get("HOA_DB_PATH", "data/hoa_index.db"))


def log_embedding_usage(total_tokens: int, model: str = "text-embedding-3-small") -> None:
    est_cost = (total_tokens / 1_000_000) * COST_OPENAI_EMBED_PER_1M
    try:
        with db.get_connection(_get_db_path()) as conn:
            db.log_api_usage(
                conn,
                service="openai_embedding",
                operation="embed",
                units=total_tokens,
                unit_type="tokens",
                est_cost_usd=round(est_cost, 8),
                metadata={"model": model},
            )
    except Exception:
        logger.debug("Failed to log embedding usage", exc_info=True)


def log_chat_usage(
    prompt_tokens: int,
    completion_tokens: int,
    model: str = "gpt-5-mini",
) -> None:
    est_cost = (
        (prompt_tokens / 1_000_000) * COST_OPENAI_CHAT_INPUT_PER_1M
        + (completion_tokens / 1_000_000) * COST_OPENAI_CHAT_OUTPUT_PER_1M
    )
    try:
        with db.get_connection(_get_db_path()) as conn:
            db.log_api_usage(
                conn,
                service="openai_chat",
                operation="chat_completion",
                units=prompt_tokens + completion_tokens,
                unit_type="tokens",
                est_cost_usd=round(est_cost, 8),
                metadata={
                    "model": model,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                },
            )
    except Exception:
        logger.debug("Failed to log chat usage", exc_info=True)


def log_docai_usage(pages: int, document: str | None = None) -> None:
    est_cost = pages * COST_DOCAI_PER_PAGE
    try:
        with db.get_connection(_get_db_path()) as conn:
            db.log_api_usage(
                conn,
                service="docai",
                operation="ocr",
                units=pages,
                unit_type="pages",
                est_cost_usd=round(est_cost, 8),
                metadata={"document": document} if document else None,
            )
    except Exception:
        logger.debug("Failed to log docai usage", exc_info=True)


def log_email_usage(provider: str, recipient_count: int = 1) -> None:
    per_email = COST_RESEND_PER_EMAIL if provider == "resend" else COST_SMTP_PER_EMAIL
    est_cost = recipient_count * per_email
    try:
        with db.get_connection(_get_db_path()) as conn:
            db.log_api_usage(
                conn,
                service=provider,
                operation="send_email",
                units=recipient_count,
                unit_type="emails",
                est_cost_usd=round(est_cost, 8),
            )
    except Exception:
        logger.debug("Failed to log email usage", exc_info=True)
