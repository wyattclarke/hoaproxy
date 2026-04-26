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

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {
    "ccr", "bylaws", "articles", "rules", "amendment",
    "resolution", "minutes", "financial", "insurance",
}
REJECT_JUNK = {"court", "tax", "government", "real_estate", "unrelated"}
REJECT_PII = {"membership_list", "ballot", "violation"}

ALL_CATEGORIES = VALID_CATEGORIES | REJECT_JUNK | REJECT_PII

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
    r'covenant|cc.?r|bylaw|article|incorporat|declaration|'
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
    (re.compile(r'cc.?r|covenant|declaration', re.I), "ccr"),
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
    if re.search(r'declaration\s+of\s+(?:protective\s+)?covenants|covenants,?\s+conditions,?\s+and\s+restrictions|cc\s*&\s*r', t):
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

    # Last resort: vision classification
    if result is None:
        result = classify_with_vision(pdf_path, hoa_name, api_key)

    # Add convenience flags
    result["is_valid"] = result["category"] in VALID_CATEGORIES
    result["is_pii_risk"] = result["category"] in REJECT_PII

    return result
