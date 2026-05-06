"""Pre-ingestion document classifier using vision LLM.

Classifies HOA documents into categories to filter out junk and PII-containing
documents before spending money on OCR and embeddings.

Two-stage approach:
  1. Digital PDFs (pdfminer extracts text) → classify from text (fast, free)
  2. Scanned PDFs (no text) → screenshot page 1 → classify with Haiku vision

Categories:
  VALID (ingest):
    ccr              — CC&Rs / Declaration of Covenants
    bylaws           — Bylaws
    articles         — Articles of Incorporation
    rules            — Rules & Regulations / Architectural Guidelines / Design Standards
    amendment        — Amendments or Supplements to governing docs
    resolution       — Board Resolutions / Policy changes
    minutes          — Meeting Minutes / Newsletter
    financial        — Budget / Financial Statement / Assessment Schedule
    insurance        — Insurance Certificate / Policy

  REJECT (junk):
    court            — Court / Legal Filing / Lawsuit / Judgment
    tax              — Tax Document / IRS Form 990
    government       — Government / Municipal Document / City Agenda
    real_estate      — Real Estate Listing / MLS / Market Report
    unrelated        — Anything else not HOA-related

  REJECT (PII risk):
    membership_list  — Membership Directory / Owner List with contact info
    ballot           — Filled-out Ballot / Proxy Form with personal info
    violation        — Violation Notice / Collections with individual owner info
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .model_usage import CallTimer, log_llm_call

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / "settings.env", override=False)
load_dotenv(_REPO_ROOT / ".env", override=False)

VALID_CATEGORIES = {
    "ccr", "bylaws", "articles", "rules", "amendment",
    "resolution", "minutes", "financial", "insurance",
}
REJECT_JUNK = {"court", "tax", "government", "real_estate", "unrelated"}
REJECT_PII = {"membership_list", "ballot", "violation"}

ALL_CATEGORIES = VALID_CATEGORIES | REJECT_JUNK | REJECT_PII

DEFAULT_CLASSIFIER_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_CLASSIFIER_BLOCKLIST = "qwen/qwen3.5-flash,qwen/qwen3.6-flash"

CATEGORY_DESCRIPTIONS = """
VALID categories (documents to keep):
- ccr: CC&Rs, Declaration of Covenants, Conditions & Restrictions
- bylaws: Bylaws of the association
- articles: Articles of Incorporation
- rules: Rules & Regulations, Architectural Guidelines, Design Standards, Community Guidelines
- amendment: Amendments or Supplements to any governing document
- resolution: Board Resolutions, formal policy changes
- minutes: Meeting Minutes, Newsletters, Annual Meeting summaries
- financial: Budgets, Financial Statements, Assessment Schedules, Reserve Studies
- insurance: Insurance Certificates, Liability Policies

REJECT categories (junk — not useful):
- court: Court filings, lawsuits, judgments, liens, legal complaints
- tax: IRS Form 990, tax returns, tax sale notices
- government: City council agendas, planning commission docs, environmental reports, water district docs
- real_estate: Property listings, MLS documents, market reports
- unrelated: Anything not related to an HOA

REJECT categories (PII risk — contain personal information):
- membership_list: Membership directories, owner lists with names/addresses/phone/email
- ballot: Filled-out ballots or proxy forms with personal information
- violation: Violation notices or collection letters naming specific owners
""".strip()

# Filename-based pre-filter for scanned PDFs (avoids Haiku calls)
_VALID_FILENAME = re.compile(
    r'covenant|restriction|cc.?r|bylaw|article|incorporat|declaration|'
    r'rule|regulation|guideline|architectural|design.?standard|'
    r'amendment|supplement|amend|restat|'
    r'resolution|policy|'
    r'minute|meeting|'
    r'budget|financial|reserve.?stud|assessment|'
    r'insurance|certificate|management.?cert',
    re.IGNORECASE,
)

_REJECT_FILENAME = re.compile(
    r'990|tax|irs|court|lawsuit|judgment|lien|'
    r'agenda|city.?council|planning.?commission|'
    r'reappraisal|bond|listing|mls|'
    r'ballot|voter|directory|member.?list|roster',
    re.IGNORECASE,
)

# Map filename keywords to specific categories
_FILENAME_CATEGORY_MAP = [
    (re.compile(r'cc.?r|covenant|restriction|declaration', re.I), "ccr"),
    (re.compile(r'bylaw', re.I), "bylaws"),
    (re.compile(r'article|incorporat', re.I), "articles"),
    (re.compile(r'rule|regulation|guideline|architectural|design.?standard', re.I), "rules"),
    (re.compile(r'amendment|supplement|amend|restat', re.I), "amendment"),
    (re.compile(r'resolution', re.I), "resolution"),
    (re.compile(r'minute|meeting', re.I), "minutes"),
    (re.compile(r'budget|financial|reserve.?stud|assessment', re.I), "financial"),
    (re.compile(r'insurance|certificate|management.?cert', re.I), "insurance"),
    (re.compile(r'policy', re.I), "rules"),
    (re.compile(r'990|tax|irs', re.I), "tax"),
    (re.compile(r'court|lawsuit|judgment|lien', re.I), "court"),
    (re.compile(r'agenda|city.?council|planning.?commission|reappraisal|bond', re.I), "government"),
    (re.compile(r'listing|mls', re.I), "real_estate"),
    (re.compile(r'ballot|voter', re.I), "ballot"),
    (re.compile(r'directory|member.?list|roster', re.I), "membership_list"),
]


def classify_from_filename(filename: str) -> dict | None:
    """Classify a scanned PDF from its filename alone. Returns result or None if uncertain."""
    name = filename.lower()
    for pattern, category in _FILENAME_CATEGORY_MAP:
        if pattern.search(name):
            return {
                "category": category,
                "confidence": 0.7,
                "method": "filename",
            }
    return None


CLASSIFY_PROMPT = """You are classifying a document that was found while searching for HOA (Homeowners Association) governing documents.

Based on the content provided, classify this document into exactly ONE of these categories:

{categories}

The document is associated with: {hoa_name}

Respond with ONLY a JSON object: {{"category": "<category>", "confidence": <0.0-1.0>}}
Do not include any other text."""

SNIPPET_CLASSIFY_SYSTEM = """You classify public HOA document candidates from metadata and short extracted snippets.
Do not browse, fetch URLs, or infer facts not present in the provided fields.
Return only a JSON object with keys: category, confidence, rationale."""

SNIPPET_CLASSIFY_USER = """Classify this candidate into exactly one category.

Categories:
{categories}

HOA name: {hoa_name}
URL: {source_url}
Title: {title}
PDF filename: {filename}
Nearby anchor text: {link_text}
Extracted text snippet:
{snippet}

JSON only. The rationale must be under 20 words."""


def _env_truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default) in {"1", "true", "True"}


def _classifier_api_key(explicit: str | None = None) -> str | None:
    return (
        explicit
        or os.environ.get("HOA_CLASSIFIER_API_KEY")
        or os.environ.get("QA_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
    )


def _classifier_api_base_url(explicit: str | None = None) -> str:
    return (
        explicit
        or os.environ.get("HOA_CLASSIFIER_API_BASE_URL")
        or os.environ.get("QA_API_BASE_URL")
        or "https://openrouter.ai/api/v1"
    )


def _blocked_classifier_models() -> list[str]:
    raw = os.environ.get("HOA_CLASSIFIER_BLOCKLIST", DEFAULT_CLASSIFIER_BLOCKLIST)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _classifier_model_allowed(model: str) -> bool:
    if _env_truthy("HOA_ALLOW_BLOCKLISTED_CLASSIFIER_MODELS"):
        return True
    model_lower = model.lower()
    return not any(blocked in model_lower for blocked in _blocked_classifier_models())


def _classifier_models(explicit: str | None = None, fallback: str | None = None) -> list[str]:
    primary = explicit or os.environ.get("HOA_CLASSIFIER_MODEL") or DEFAULT_CLASSIFIER_MODEL
    fallback_model = fallback if fallback is not None else os.environ.get("HOA_CLASSIFIER_FALLBACK_MODEL")
    models = [m for m in (primary, fallback_model) if m]
    allowed: list[str] = []
    for candidate in models:
        if _classifier_model_allowed(candidate):
            allowed.append(candidate)
        else:
            logger.warning(
                "Skipping blocklisted classifier model %s; set HOA_ALLOW_BLOCKLISTED_CLASSIFIER_MODELS=1 to override",
                candidate,
            )
    return allowed


def _parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    return json.loads(text)


def _coerce_llm_result(payload: dict[str, Any], *, model: str) -> dict | None:
    category = str(payload.get("category") or "").strip().lower()
    if category not in ALL_CATEGORIES:
        return None
    try:
        confidence = float(payload.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    out = {
        "category": category,
        "confidence": confidence,
        "method": "llm",
        "model": model,
    }
    rationale = str(payload.get("rationale") or "").strip()
    if rationale:
        out["rationale"] = rationale[:240]
    return out


def classify_with_llm(
    text: str,
    hoa_name: str = "",
    *,
    source_url: str = "",
    title: str = "",
    filename: str = "",
    link_text: str = "",
    api_key: str | None = None,
    api_base_url: str | None = None,
    model: str | None = None,
    fallback_model: str | None = None,
    max_chars: int = 2000,
) -> dict | None:
    """Classify an ambiguous public document candidate via OpenAI-compatible chat.

    This is intentionally snippet-only: callers should pass small public text
    extracts and provenance fields, never credentials, cookies, private portal
    content, or resident data.
    """
    key = _classifier_api_key(api_key)
    if not key:
        raise ValueError("HOA_CLASSIFIER_API_KEY, QA_API_KEY, or OPENROUTER_API_KEY is required")

    from openai import OpenAI

    base_url = _classifier_api_base_url(api_base_url)
    client = OpenAI(api_key=key, base_url=base_url)
    snippet = (text or "").strip()[:max_chars]
    messages = [
        {"role": "system", "content": SNIPPET_CLASSIFY_SYSTEM},
        {
            "role": "user",
            "content": SNIPPET_CLASSIFY_USER.format(
                categories=CATEGORY_DESCRIPTIONS,
                hoa_name=hoa_name,
                source_url=source_url,
                title=title,
                filename=filename,
                link_text=link_text,
                snippet=snippet,
            ),
        },
    ]

    models = _classifier_models(model, fallback_model)
    if not models:
        raise ValueError("No allowed classifier models configured after applying HOA_CLASSIFIER_BLOCKLIST")

    last_error: Exception | None = None
    for candidate_model in models:
        timer = CallTimer()
        try:
            kwargs: dict[str, Any] = {
                "model": candidate_model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": 180,
                "response_format": {"type": "json_object"},
            }
            if "openrouter.ai" in base_url:
                kwargs["extra_body"] = {"include_reasoning": False}
            response = client.chat.completions.create(
                **kwargs,
            )
            log_llm_call(
                operation="doc_classifier.classify_with_llm",
                model=candidate_model,
                api_base_url=base_url,
                api_key=key,
                response=response,
                elapsed_ms=timer.elapsed_ms(),
                metadata={
                    "hoa_name": hoa_name,
                    "source_url": source_url,
                    "title": title[:160],
                    "filename": filename,
                    "link_text": link_text[:160],
                    "max_chars": max_chars,
                },
            )
            content = response.choices[0].message.content or "{}"
            parsed = _parse_json_object(content)
            result = _coerce_llm_result(parsed, model=candidate_model)
            if result:
                return result
        except Exception as exc:
            last_error = exc
            logger.info("LLM classification failed with %s: %s", candidate_model, exc)
            log_llm_call(
                operation="doc_classifier.classify_with_llm",
                model=candidate_model,
                api_base_url=base_url,
                api_key=key,
                status="error",
                error=str(exc),
                elapsed_ms=timer.elapsed_ms(),
                metadata={
                    "hoa_name": hoa_name,
                    "source_url": source_url,
                    "title": title[:160],
                    "filename": filename,
                    "link_text": link_text[:160],
                    "max_chars": max_chars,
                },
            )

    if last_error:
        raise last_error
    return None


def classify_from_text(text: str, hoa_name: str = "") -> dict:
    """Classify a document from its extracted text using regex patterns.

    Returns {category, confidence, method} or None if uncertain.
    Fast path for digital PDFs — no API call needed.
    """
    t = text.lower()

    # PII patterns — check first
    # Membership list: many Name + Address pairs
    name_addr_pattern = re.findall(
        r'(?:mr|mrs|ms|dr)?\.?\s*[A-Z][a-z]+\s+[A-Z][a-z]+\s*\n\s*\d+\s+\w+',
        text,
    )
    if len(name_addr_pattern) >= 5:
        return {"category": "membership_list", "confidence": 0.85, "method": "regex"}

    if re.search(r'(?:ballot|proxy\s+form).*(?:signature|sign\s+here|owner\s+name)', t):
        if re.search(r'(?:unit\s*(?:#|no|num)|lot\s*(?:#|no|num)).*\d', t):
            return {"category": "ballot", "confidence": 0.8, "method": "regex"}

    if re.search(r'violation\s+notice|notice\s+of\s+(?:violation|hearing)', t):
        if re.search(r'(?:dear\s+(?:mr|mrs|ms|homeowner)|property\s+address)', t):
            return {"category": "violation", "confidence": 0.8, "method": "regex"}

    # Junk patterns
    if re.search(r'(?:bankruptcy|district)\s+court|plaintiff|defendant|docket\s+no', t):
        return {"category": "court", "confidence": 0.9, "method": "regex"}

    if re.search(r'form\s+990|return\s+of\s+organization\s+exempt|internal\s+revenue', t):
        return {"category": "tax", "confidence": 0.9, "method": "regex"}

    if re.search(r'(?:planning|city)\s+commission|city\s+council\s+agenda|environmental\s+impact', t):
        return {"category": "government", "confidence": 0.85, "method": "regex"}

    # Valid patterns
    if re.search(
        r'declaration\s+of\s+(?:protective\s+)?(?:covenants?|restrictions?)|'
        r'declaration\s+of\s+covenants?,?\s+conditions,?\s+and\s+restrictions|'
        r'covenants,?\s+conditions,?\s+and\s+restrictions|'
        r'conditions,?\s+covenants,?\s+and\s+restrictions|'
        r'protective\s+covenants|'
        r'restrictive\s+covenants|'
        r'cc\s*&\s*r',
        t,
    ):
        return {"category": "ccr", "confidence": 0.95, "method": "regex"}

    if re.search(r'by-?laws?\s+of|article\s+[ivx\d]+.*(?:members|board|officers|meetings)', t):
        return {"category": "bylaws", "confidence": 0.9, "method": "regex"}

    if re.search(r'articles\s+of\s+incorporation|certificate\s+of\s+incorporation', t):
        return {"category": "articles", "confidence": 0.9, "method": "regex"}

    if re.search(r'(?:architectural|design)\s+(?:guidelines?|standards?|review)|rules\s+and\s+regulations', t):
        return {"category": "rules", "confidence": 0.9, "method": "regex"}

    if re.search(r'(?:first|second|third|\d+(?:st|nd|rd|th))\s+amendment|supplemental\s+declaration|amended\s+and\s+restated', t):
        return {"category": "amendment", "confidence": 0.85, "method": "regex"}

    if re.search(r'resolution\s+(?:no\.?|of\s+the\s+board|#)', t):
        return {"category": "resolution", "confidence": 0.8, "method": "regex"}

    if re.search(r'(?:annual|board|special)\s+meeting\s+minutes|minutes\s+of\s+(?:the\s+)?(?:annual|board)', t):
        return {"category": "minutes", "confidence": 0.85, "method": "regex"}

    if re.search(r'(?:proposed\s+)?budget|financial\s+statement|reserve\s+(?:study|fund)|assessment\s+schedule', t):
        return {"category": "financial", "confidence": 0.8, "method": "regex"}

    if re.search(r'certificate\s+of\s+(?:insurance|liability)|insurance\s+(?:policy|certificate)', t):
        return {"category": "insurance", "confidence": 0.8, "method": "regex"}

    return None  # uncertain — needs vision classification


def classify_with_vision(
    pdf_path: Path,
    hoa_name: str = "",
    api_key: str | None = None,
) -> dict:
    """Classify a scanned PDF by screenshotting page 1 and sending to Haiku.

    Returns {category, confidence, method}.
    """
    import anthropic

    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY required for vision classification")

    # Render page 1 to PNG
    image_data = _render_page_1(pdf_path)
    if image_data is None:
        return {"category": "unrelated", "confidence": 0.5, "method": "vision_render_failed"}

    client = anthropic.Anthropic(api_key=api_key)
    prompt = CLASSIFY_PROMPT.format(categories=CATEGORY_DESCRIPTIONS, hoa_name=hoa_name)

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(image_data).decode(),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text = response.content[0].text.strip()

        # Parse JSON response
        # Handle potential markdown wrapping
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        result = json.loads(text)
        category = result.get("category", "unrelated")
        confidence = float(result.get("confidence", 0.5))

        if category not in ALL_CATEGORIES:
            category = "unrelated"

        return {"category": category, "confidence": confidence, "method": "vision"}

    except Exception as e:
        logger.warning("Vision classification failed for %s: %s", pdf_path, e)
        return {"category": "unrelated", "confidence": 0.0, "method": "vision_error"}


def _render_page_1(pdf_path: Path) -> bytes | None:
    """Render page 1 of a PDF to a PNG byte string."""
    try:
        from pdf2image import convert_from_path
        images = convert_from_path(
            str(pdf_path), dpi=150, first_page=1, last_page=1,
        )
        if not images:
            return None
        buf = io.BytesIO()
        images[0].save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        # Fallback: try pypdf rendering
        logger.debug("pdf2image failed for %s: %s, trying pypdf", pdf_path, e)
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(pdf_path))
            if not reader.pages:
                return None
            # pypdf can't render to image — return None
            return None
        except Exception:
            return None


def classify_pdf(pdf_path: Path, hoa_name: str = "", api_key: str | None = None) -> dict:
    """Classify a PDF document. Tries text-based regex first, falls back to vision.

    Returns {category, confidence, method, is_valid, is_pii_risk}.
    """
    # Try pdfminer text extraction first (free)
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(str(pdf_path), page_numbers=[0], maxpages=1)
    except Exception:
        text = ""

    result = None
    if text and text.strip() and len(text.strip()) > 50:
        result = classify_from_text(text, hoa_name)

    # For scanned PDFs or uncertain text: try filename before vision
    if result is None:
        result = classify_from_filename(pdf_path.name)

    # Optional OpenAI-compatible snippet classifier for public documents.
    if result is None and text and _env_truthy("HOA_ENABLE_LLM_CLASSIFIER"):
        try:
            result = classify_with_llm(text, hoa_name, filename=pdf_path.name)
        except Exception as exc:
            logger.info("LLM classification skipped for %s: %s", pdf_path, exc)

    # Last resort: vision classification
    if result is None:
        result = classify_with_vision(pdf_path, hoa_name, api_key)

    # Add convenience flags
    result["is_valid"] = result["category"] in VALID_CATEGORIES
    result["is_pii_risk"] = result["category"] in REJECT_PII

    return result
