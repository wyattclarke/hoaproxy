from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
import functools
import json
import logging
import math
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote, unquote, urlparse


# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_obj["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


_json_handler = logging.StreamHandler()
_json_handler.setFormatter(_JsonFormatter())
logging.root.setLevel(logging.INFO)
logging.root.handlers = [_json_handler]

import hashlib
import io
import requests
from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hoaware import db
from hoaware.auth import get_current_user, hash_password, verify_password, create_access_token, optional_current_user
from hoaware.config import load_settings
from hoaware.cost_tracker import COST_DOCAI_PER_PAGE
from hoaware.doc_classifier import (
    VALID_CATEGORIES,
    REJECT_PII,
    classify_from_filename,
    classify_from_text,
)
from hoaware.ingest import ingest_pdf_paths
from hoaware.pdf_utils import detect_text_extractable
from hoaware import participation as participation_mod
from hoaware.qa import get_answer, get_answer_multi, retrieve_context, retrieve_context_multi

_LAW_IMPORT_ERROR: Exception | None = None
try:
    from hoaware.law import (
        answer_electronic_proxy_questions,
        answer_law_question,
        electronic_proxy_summary,
        list_jurisdictions,
        list_profiles,
    )
except Exception as exc:  # pragma: no cover - only used when optional module is missing
    _LAW_IMPORT_ERROR = exc


# ---------------------------------------------------------------------------
# Rate limiting (simple in-memory, per-IP sliding window)
# ---------------------------------------------------------------------------

_rate_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW = 60.0   # seconds
_RATE_LIMIT   = 20    # max requests per window per IP


def _check_rate_limit(request: Request, limit: int = _RATE_LIMIT) -> None:
    ip = request.client.host if request.client else "unknown"
    # TestClient uses "testclient" as host — skip rate limiting in tests
    if ip == "testclient":
        return
    now = time.monotonic()
    _rate_buckets[ip] = [t for t in _rate_buckets[ip] if now - t < _RATE_WINDOW]
    if len(_rate_buckets[ip]) >= limit:
        raise HTTPException(status_code=429, detail="Too many requests — try again in a minute")
    _rate_buckets[ip].append(now)


# ---------------------------------------------------------------------------
# Data retention: expire and purge old proxy assignments
# ---------------------------------------------------------------------------

def _run_expiry_sweep() -> None:
    """Mark expired proxy assignments and soft-delete old ones."""
    settings = load_settings()
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        with db.get_connection(settings.db_path) as conn:
            # Mark as 'expired' if expires_at < today and status is non-terminal
            terminal = ("signed", "delivered", "acknowledged", "revoked", "expired", "purged")
            placeholders = ",".join("?" * len(terminal))
            conn.execute(
                f"""
                UPDATE proxy_assignments
                SET status = 'expired'
                WHERE expires_at IS NOT NULL
                  AND expires_at < ?
                  AND status NOT IN ({placeholders})
                """,
                (today, *terminal),
            )
            # Soft-delete (purge) assignments where expires_at + retention_days < today
            retention_days = settings.proxy_retention_days
            conn.execute(
                f"""
                UPDATE proxy_assignments
                SET status = 'purged'
                WHERE expires_at IS NOT NULL
                  AND date(expires_at, '+' || ? || ' days') < ?
                  AND status NOT IN ({placeholders})
                """,
                (retention_days, today, *terminal),
            )
            conn.commit()
    except Exception:
        logger.exception("Expiry sweep failed")


def _run_verification_link_backfill() -> None:
    """Repair legacy verification URLs embedded in stored signed proxy HTML."""
    legacy_hosts = ("https://hoaware.app", "http://hoaware.app")
    canonical_base = "https://hoaproxy.org"
    try:
        with db.get_connection(load_settings().db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, form_html, verification_code
                FROM proxy_assignments
                WHERE verification_code IS NOT NULL
                  AND form_html IS NOT NULL
                """
            ).fetchall()
            updated = 0
            for row in rows:
                code = row["verification_code"]
                html = row["form_html"] or ""
                new_url = f"{canonical_base}/verify-proxy?code={code}"
                original = html
                for host in legacy_hosts:
                    old_url = f"{host}/verify-proxy?code={code}"
                    html = html.replace(
                        f"Verify at: {old_url}",
                        'Verify at: '
                        f'<a href="{new_url}" target="_blank" rel="noopener noreferrer">{new_url}</a>',
                    )
                    html = html.replace(old_url, new_url)
                if html != original:
                    conn.execute(
                        "UPDATE proxy_assignments SET form_html = ? WHERE id = ?",
                        (html, row["id"]),
                    )
                    updated += 1
            if updated:
                conn.commit()
                logger.info("Repaired verification links for %d signed proxies", updated)
    except Exception:
        logger.exception("Verification link backfill failed")


# ---------------------------------------------------------------------------
# Startup: ensure all DB tables exist (safe to run on existing DB)
# ---------------------------------------------------------------------------

def _run_proxy_status_backfill() -> None:
    """Classify proxy allowance for HOAs that don't yet have a status.

    Runs at startup in a background thread. Skips HOAs already classified,
    so this is a no-op after the first run on a given database.
    """
    import json as _json
    from openai import OpenAI
    settings = load_settings()
    if not settings.openai_api_key:
        return
    _PROXY_KW = {"proxy", "proxies", "absentee ballot", "vote in person", "in-person voting"}

    with db.get_connection(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT id, name FROM hoas WHERE proxy_status IS NULL OR proxy_status = 'unknown'"
        ).fetchall()

    if not rows:
        return

    client = OpenAI(api_key=settings.openai_api_key)
    logger.info("Proxy status backfill: %d HOAs to classify", len(rows))

    for row in rows:
        hoa_id, name = row["id"], row["name"]
        with db.get_connection(settings.db_path) as conn:
            texts = db.get_chunk_text_for_hoa(conn, name, limit=200)
        relevant = [t for t in texts if any(kw in t.lower() for kw in _PROXY_KW)]
        if not relevant:
            continue
        excerpt = "\n\n---\n\n".join(relevant[:6])
        prompt = (
            "You are analyzing excerpts from HOA governing documents. "
            "Determine whether proxy voting is: "
            '"allowed" (explicitly permitted), '
            '"not_allowed" (explicitly prohibited or in-person only required), or '
            '"unknown" (not clearly addressed).\n\n'
            f"Excerpts:\n{excerpt}\n\n"
            'Respond with JSON only: {"status": "allowed"|"not_allowed"|"unknown", '
            '"citation": "exact supporting quote or null"}'
        )
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=200,
            )
            raw = (resp.choices[0].message.content or "").strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            result = _json.loads(raw)
            status = result.get("status", "unknown")
            citation = result.get("citation") or None
            if status not in ("allowed", "not_allowed", "unknown"):
                status = "unknown"
            with db.get_connection(settings.db_path) as conn:
                db.set_hoa_proxy_status(conn, hoa_id, status, citation)
            logger.info("HOA %d (%s) proxy_status = %s", hoa_id, name, status)
        except Exception:
            logger.exception("Proxy status classification failed for HOA %d (%s)", hoa_id, name)


def _cost_report_scheduler() -> None:
    """Send a weekly cost report email every Sunday at 18:00 UTC."""
    import time as _time
    from hoaware.email_service import send_cost_report

    while True:
        now = datetime.now(timezone.utc)
        # Find next Sunday 18:00 UTC
        days_until_sunday = (6 - now.weekday()) % 7
        if days_until_sunday == 0 and now.hour >= 18:
            days_until_sunday = 7
        next_sunday = now.replace(hour=18, minute=0, second=0, microsecond=0)
        next_sunday = next_sunday + __import__("datetime").timedelta(days=days_until_sunday)
        wait_seconds = (next_sunday - now).total_seconds()
        logger.info("Cost report scheduler: next run at %s (%.0f seconds)", next_sunday.isoformat(), wait_seconds)
        _time.sleep(wait_seconds)

        settings = load_settings()
        to_email = settings.cost_report_email
        if not to_email:
            logger.info("COST_REPORT_EMAIL not set, skipping weekly report")
            continue
        try:
            send_cost_report(to_email=to_email)
            logger.info("Weekly cost report sent to %s", to_email)
        except Exception:
            logger.exception("Failed to send weekly cost report")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    try:
        with db.get_connection(settings.db_path) as conn:
            conn.executescript(db.SCHEMA)
            seeded = db.seed_legal_data(conn)
            if seeded:
                logger.info("Seeded %d legal rows from bundled seed files", seeded)
            archived = db.archive_stale_proposals(conn, days=60)
            if archived:
                logger.info("Archived %d stale proposals on startup", archived)
        _run_expiry_sweep()
        _run_verification_link_backfill()
        import threading
        # Backfill the sqlite-vec index from existing embeddings (one-time
        # after upgrade; idempotent thereafter). Run in a thread so a slow
        # backfill doesn't block app startup.
        def _vec_backfill():
            try:
                with db.get_connection(settings.db_path) as conn:
                    n = db.backfill_vec_index(conn)
                    if n:
                        logger.info("sqlite-vec backfill: indexed %d chunks", n)
            except Exception:
                logger.exception("sqlite-vec backfill failed")
        threading.Thread(target=_vec_backfill, daemon=True).start()
        threading.Thread(target=_run_proxy_status_backfill, daemon=True).start()
        threading.Thread(target=_cost_report_scheduler, daemon=True).start()
    except Exception as exc:
        logger.error("Startup migration error (non-fatal): %s", exc)
    yield


app = FastAPI(title="HOA QA API", version="0.2.0", lifespan=lifespan)

# Session middleware required by authlib for OAuth state parameter
from starlette.middleware.sessions import SessionMiddleware
_oauth_session_secret = os.environ.get("JWT_SECRET", "dev-secret-change-in-production")
app.add_middleware(SessionMiddleware, secret_key=_oauth_session_secret)
_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_CITY_STATE_ZIP_RE = re.compile(
    r"\b([A-Z][A-Za-z]+(?:[\s-][A-Z][A-Za-z]+)*),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b"
)
_STREET_RE = re.compile(
    r"\b\d{2,6}\s+[A-Za-z0-9][A-Za-z0-9 .'-]{3,80}\b"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Way|Circle|Cir|Parkway|Pkwy|Trail|Trl)\b",
    re.IGNORECASE,
)
_EARTH_RADIUS_M = 6371000.0
_NEAR_BOUNDARY_M = 3219.0   # 2 miles — initial search radius for boundaries
_NEAR_POINT_M = 3219.0      # 2 miles — initial search radius for point markers
_EXPANDED_RADIUS_M = 8047.0  # 5 miles — expanded radius when fewer than 3 results
_MIN_SUGGESTIONS = 3         # expand radius if fewer than this many results
_MAX_SUGGESTIONS_DEFAULT = 10
STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger(__name__)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class QARequest(BaseModel):
    hoa: str
    question: str
    k: int = Field(default=6, ge=1, le=20)
    model: str = ""


class QAResponse(BaseModel):
    answer: str
    sources: List[dict]


class SearchRequest(BaseModel):
    hoa: str
    query: str
    k: int = Field(default=5, ge=1, le=20)


class SearchResult(BaseModel):
    score: float
    document: str
    pages: str
    excerpt: str


class SearchResponse(BaseModel):
    results: List[SearchResult]


class MultiSearchRequest(BaseModel):
    hoas: list[str]
    query: str
    k: int = Field(default=8, ge=1, le=20)


class MultiSearchResult(BaseModel):
    score: float
    hoa: str
    document: str
    pages: str
    excerpt: str


class MultiSearchResponse(BaseModel):
    results: List[MultiSearchResult]


class MultiQARequest(BaseModel):
    hoas: list[str]
    question: str
    k: int = Field(default=8, ge=1, le=20)
    model: str = ""


class HoaMatch(BaseModel):
    hoa: str
    match_reason: str


class AddressLookup(BaseModel):
    resolved: bool
    display_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class AddressSuggestion(BaseModel):
    hoa: str
    match_type: str
    confidence: str
    default_selected: bool
    distance_m: float
    reason: str
    latitude: float | None = None
    longitude: float | None = None
    boundary_geojson: dict | None = None


class UniversalLookupRequest(BaseModel):
    query: str
    max_suggestions: int = Field(default=12, ge=1, le=50)


class UniversalLookupResponse(BaseModel):
    query: str
    hoa_matches: list[HoaMatch]
    address_lookup: AddressLookup
    address_suggestions: list[AddressSuggestion]


class UploadResponse(BaseModel):
    hoa: str
    saved_files: List[str]
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    queued: bool = False
    location_saved: bool = False


class AgentPrecheckRequest(BaseModel):
    url: str | None = None
    sha256: str | None = None
    filename: str | None = None
    hoa: str | None = None


class AgentPrecheckResponse(BaseModel):
    page_count: int | None
    file_size_bytes: int | None
    sha256: str | None
    text_extractable: bool | None
    suggested_category: str | None
    is_valid_governing_doc: bool
    is_pii_risk: bool
    duplicate_of: str | None
    est_docai_pages: int
    est_docai_cost_usd: float
    notes: List[str]


def _is_full_name(value: str | None) -> bool:
    if not value:
        return False
    parts = [part for part in re.split(r"\s+", value.strip()) if part]
    if len(parts) < 2:
        return False
    return all(any(ch.isalpha() for ch in part) for part in parts[:2])


class LawJurisdictionSummary(BaseModel):
    jurisdiction: str
    community_types: int
    profile_count: int
    last_verified_date: str | None = None
    rule_count: int


class LawProfile(BaseModel):
    id: int
    jurisdiction: str
    community_type: str
    entity_form: str
    governing_law_stack: list[dict]
    records_access_summary: str | None = None
    records_sharing_limits_summary: str | None = None
    proxy_voting_summary: str | None = None
    conflict_resolution_notes: str | None = None
    known_gaps: list[str]
    confidence: str
    last_verified_date: str | None = None
    source_rule_count: int
    created_at: str
    updated_at: str


class LawQARequest(BaseModel):
    jurisdiction: str = Field(..., min_length=2, max_length=2)
    community_type: str
    question_family: str
    entity_form: str = "unknown"


class LawQAResponse(BaseModel):
    answer: str
    checklist: list[str]
    citations: list[dict]
    known_unknowns: list[str]
    confidence: str
    last_verified_date: str | None = None
    disclaimer: str


class ElectronicProxyQuestionResponse(BaseModel):
    jurisdiction: str
    community_type: str
    entity_form: str
    electronic_assignment: dict
    electronic_signature: dict
    known_unknowns: list[str]
    confidence: str
    last_verified_date: str | None = None
    disclaimer: str


class ElectronicProxySummaryItem(BaseModel):
    jurisdiction: str
    community_type: str
    entity_form: str
    electronic_assignment_status: str
    electronic_signature_status: str
    confidence: str
    last_verified_date: str | None = None
    known_unknowns: list[str]


class DocumentSummary(BaseModel):
    relative_path: str
    bytes: int
    page_count: int | None
    chunk_count: int
    last_ingested: str


class HoaLocation(BaseModel):
    hoa: str
    metadata_type: str | None = None
    display_name: str | None = None
    website_url: str | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    boundary_geojson: dict | None = None
    source: str | None = None
    updated_at: str | None = None


class HoaSummary(BaseModel):
    hoa_id: int | None = None
    hoa: str
    metadata_type: str | None = None
    doc_count: int
    chunk_count: int
    total_bytes: int
    last_ingested: str | None = None
    website_url: str | None = None
    city: str | None = None
    state: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    boundary_geojson: dict | None = None


class HoaSummaryPage(BaseModel):
    results: List[HoaSummary]
    total: int


class HoaMapPoint(BaseModel):
    hoa: str
    latitude: float | None = None
    longitude: float | None = None
    state: str | None = None
    doc_count: int
    boundary_geojson: dict | None = None


# ---------------------------------------------------------------------------
# Auth models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: str
    password: str = Field(..., min_length=8)
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    user_id: int
    token: str


class UserMeResponse(BaseModel):
    user_id: int
    email: str
    display_name: str | None = None
    hoas: list[dict] = []
    email_verified: bool = False


class UserUpdateRequest(BaseModel):
    display_name: str | None = None
    email: str | None = None
    current_password: str | None = None
    new_password: str | None = None


class MembershipClaimRequest(BaseModel):
    unit_number: str | None = None


class MembershipClaimResponse(BaseModel):
    id: int
    user_id: int
    hoa_id: int
    hoa_name: str
    unit_number: str | None = None
    status: str


class DelegateRegisterRequest(BaseModel):
    hoa_id: int
    bio: str | None = None
    contact_email: str | None = None


class DelegateUpdateRequest(BaseModel):
    bio: str | None = None
    contact_email: str | None = None


class DelegateResponse(BaseModel):
    id: int
    user_id: int
    hoa_id: int
    hoa_name: str
    display_name: str | None = None
    bio: str | None = None
    contact_email: str | None = None
    created_at: str | None = None


# ---------------------------------------------------------------------------
# Proxy models
# ---------------------------------------------------------------------------

class CreateProxyRequest(BaseModel):
    delegate_user_id: int
    hoa_id: int
    direction: str = "undirected"
    voting_instructions: str | None = None
    for_meeting_date: str | None = None


class SignProxyRequest(BaseModel):
    pass  # No additional data needed; IP/UA captured from request


class RevokeProxyRequest(BaseModel):
    reason: str | None = None


class ProxyResponse(BaseModel):
    id: int
    grantor_user_id: int
    delegate_user_id: int
    hoa_id: int
    hoa_name: str | None = None
    grantor_name: str | None = None
    delegate_name: str | None = None
    jurisdiction: str
    community_type: str
    direction: str
    voting_instructions: str | None = None
    for_meeting_date: str | None = None
    expires_at: str | None = None
    status: str
    signed_at: str | None = None
    delivered_at: str | None = None
    revoked_at: str | None = None
    revoke_reason: str | None = None
    created_at: str | None = None


class ProxyStatsResponse(BaseModel):
    total: int
    signed: int
    delivered: int


# ---------------------------------------------------------------------------
# Proposal models
# ---------------------------------------------------------------------------

PROPOSAL_CATEGORIES = {"Maintenance", "Amenities", "Rules", "Safety", "Other"}


class CreateProposalRequest(BaseModel):
    hoa_id: int
    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10, max_length=5000)
    category: str = "Other"
    lat: float | None = None
    lng: float | None = None
    location_description: str | None = Field(None, max_length=200)


class ProposalResponse(BaseModel):
    id: int
    hoa_id: int
    hoa_name: str | None = None
    creator_user_id: int
    title: str
    description: str
    category: str
    status: str
    cosigner_count: int = 0
    upvote_count: int = 0
    share_code: str | None = None
    cosigners: list[str] = []
    user_cosigned: bool = False
    user_upvoted: bool = False
    created_at: str | None = None
    published_at: str | None = None
    lat: float | None = None
    lng: float | None = None
    location_description: str | None = None


class ParticipationRequest(BaseModel):
    meeting_date: str           # YYYY-MM-DD
    meeting_type: str = "annual"
    total_units: int
    votes_cast: int
    quorum_required: Optional[int] = None
    quorum_met: Optional[bool] = None
    notes: Optional[str] = None


def _normalize_hoa_name(raw_name: str) -> str:
    cleaned = " ".join(raw_name.split()).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="hoa is required")
    if "/" in cleaned or "\\" in cleaned or cleaned in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid HOA name")
    return cleaned


def _ensure_law_module_available() -> None:
    if _LAW_IMPORT_ERROR is None:
        return
    logger.warning("Law module unavailable: %s", _LAW_IMPORT_ERROR)
    raise HTTPException(status_code=503, detail="Law endpoints are temporarily unavailable.")


def _resolve_hoa_name(raw_name: str) -> str:
    cleaned = _normalize_hoa_name(raw_name)
    settings = load_settings()
    known: list[str] = []
    if settings.docs_root.exists():
        known.extend([p.name for p in settings.docs_root.iterdir() if p.is_dir()])
    with db.get_connection(settings.db_path) as conn:
        known.extend(db.list_hoa_names(conn))
    for existing in known:
        if existing.casefold() == cleaned.casefold():
            return existing
    return cleaned


def _safe_pdf_filename(raw_name: str | None) -> str:
    if not raw_name:
        raise HTTPException(status_code=400, detail="Uploaded file is missing a filename")
    safe = _FILENAME_RE.sub("_", Path(raw_name).name).strip()
    if not safe or safe in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not safe.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail=f"{raw_name} is not a PDF")
    return safe


def _normalize_website_url(raw_url: str | None) -> str | None:
    if raw_url is None:
        return None
    cleaned = raw_url.strip()
    if not cleaned:
        return None
    if not re.match(r"^https?://", cleaned, re.IGNORECASE):
        cleaned = f"https://{cleaned}"
    parsed = urlparse(cleaned)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid HOA website URL")
    return cleaned


def _normalize_metadata_type(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    cleaned = raw_value.strip().lower()
    if not cleaned:
        return None
    allowed = {"hoa", "condo", "coop", "timeshare"}
    if cleaned not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"metadata_type must be one of: {', '.join(sorted(allowed))}",
        )
    return cleaned


_ingest_semaphore = threading.Semaphore(1)  # only one ingestion at a time


_TEXT_EXTRACTABLE_TRUE = {"true", "1", "yes", "y"}
_TEXT_EXTRACTABLE_FALSE = {"false", "0", "no", "n"}

# Daily DocAI spend ceiling across the whole app. Hits trigger 429 on
# /upload until the rolling-24h spend drops below the cap. Default $20/day.
DAILY_DOCAI_BUDGET_USD = float(os.environ.get("DAILY_DOCAI_BUDGET_USD", "20.0"))


def _projected_docai_pages(
    paths: list[Path], metadata_by_path: dict[Path, dict]
) -> int:
    """Sum page counts for files the agent flagged text_extractable=False."""
    import pypdf as _pypdf
    total = 0
    for p in paths:
        meta = metadata_by_path.get(p) or {}
        if meta.get("text_extractable") is False:
            try:
                total += len(_pypdf.PdfReader(str(p)).pages)
            except Exception:
                pass
    return total


def _check_daily_docai_budget(projected_pages: int) -> None:
    """Refuse the upload if last-24h DocAI cost + projected exceeds the cap.

    `projected_pages` is the agent's worst-case page count for this upload
    (sum of pages where text_extractable=False). We don't know the true
    figure for hint-omitted PDFs, so we don't count them here.
    """
    if projected_pages <= 0:
        return
    from hoaware.cost_tracker import COST_DOCAI_PER_PAGE

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        recent = db.get_recent_service_cost_usd(conn, "docai", hours=24)
    projected_cost = projected_pages * COST_DOCAI_PER_PAGE
    if recent + projected_cost > DAILY_DOCAI_BUDGET_USD:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Daily DocAI budget would be exceeded: "
                f"${recent:.2f} spent in last 24h + ${projected_cost:.2f} "
                f"projected > ${DAILY_DOCAI_BUDGET_USD:.2f} cap. "
                f"Either set text_extractable=true for digital PDFs, "
                f"raise DAILY_DOCAI_BUDGET_USD, or wait for the rolling window."
            ),
        )


def _parse_per_file_metadata(
    file_count: int,
    *,
    categories: list[str] | None,
    text_extractable: list[str] | None,
    source_urls: list[str] | None,
) -> list[dict]:
    """Validate and normalize per-file agent metadata. Returns one dict per file.

    Each dict has keys: category (str|None), text_extractable (bool|None), source_url (str|None).
    Raises HTTPException on invalid category or PII-flagged category.
    """
    def _normalize_array(arr: list[str] | None, name: str) -> list[str | None]:
        if arr is None or len(arr) == 0:
            return [None] * file_count
        if len(arr) != file_count:
            raise HTTPException(
                status_code=400,
                detail=f"{name} length ({len(arr)}) must equal number of files ({file_count})",
            )
        return [(v if v not in (None, "") else None) for v in arr]

    cats = _normalize_array(categories, "categories")
    tex = _normalize_array(text_extractable, "text_extractable")
    urls = _normalize_array(source_urls, "source_urls")

    out: list[dict] = []
    for i, (c, t, u) in enumerate(zip(cats, tex, urls)):
        if c is not None:
            cat = c.strip().lower()
            if cat in REJECT_PII:
                raise HTTPException(
                    status_code=400,
                    detail=f"file {i}: category '{cat}' is rejected (PII risk)",
                )
            if cat not in VALID_CATEGORIES and cat != "unknown":
                raise HTTPException(
                    status_code=400,
                    detail=f"file {i}: category '{cat}' is not a valid governing-doc category",
                )
        else:
            cat = None

        te: bool | None = None
        if t is not None:
            tv = t.strip().lower()
            if tv in _TEXT_EXTRACTABLE_TRUE:
                te = True
            elif tv in _TEXT_EXTRACTABLE_FALSE:
                te = False
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"file {i}: text_extractable must be true/false, got '{t}'",
                )

        out.append({"category": cat, "text_extractable": te, "source_url": u})
    return out


def _ingest_uploaded_files(
    hoa_name: str,
    saved_paths: list[Path],
    metadata_by_path: dict[Path, dict] | None = None,
) -> None:
    with _ingest_semaphore:
        settings = load_settings()
        try:
            stats = ingest_pdf_paths(
                hoa_name,
                saved_paths,
                settings=settings,
                show_progress=False,
                metadata_by_path=metadata_by_path,
            )
            logger.info(
                "Background ingest complete for %s: indexed=%s skipped=%s failed=%s",
                hoa_name,
                stats.indexed,
                stats.skipped,
                stats.failed,
            )
        except Exception:
            logger.exception("Background ingest failed for HOA %s", hoa_name)


def _safe_relative_document_path(raw_path: str) -> str:
    candidate = unquote(raw_path).strip().replace("\\", "/")
    if not candidate:
        raise HTTPException(status_code=400, detail="document path is required")
    if candidate.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid document path")
    if ".." in Path(candidate).parts:
        raise HTTPException(status_code=400, detail="Invalid document path")
    return candidate


def _infer_location_parts(chunks: list[str]) -> dict:
    text = "\n".join(chunks)
    match_city = _CITY_STATE_ZIP_RE.search(text)
    match_street = _STREET_RE.search(text)
    city = match_city.group(1) if match_city else None
    state = match_city.group(2) if match_city else None
    postal = match_city.group(3) if match_city else None
    street = match_street.group(0) if match_street else None
    return {
        "street": street,
        "city": city,
        "state": state,
        "postal_code": postal,
    }


def _geocode_from_parts(*, street: str | None, city: str | None, state: str | None, postal_code: str | None) -> tuple[float, float] | None:
    parts = [street, city, state, postal_code, "USA"]
    query = ", ".join([p for p in parts if p])
    if not query:
        return None
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": "hoaproxy/0.2 (local-ui-location)"},
            timeout=20,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        return float(rows[0]["lat"]), float(rows[0]["lon"])
    except Exception:
        logger.exception("Geocoding failed for query: %s", query)
        return None


def _geocode_from_query(query: str) -> dict | None:
    cleaned = query.strip()
    if not cleaned:
        return None
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": cleaned, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": "hoaproxy/0.2 (local-ui-location)"},
            timeout=20,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return None
        row = rows[0]
        return {
            "display_name": str(row.get("display_name") or cleaned),
            "latitude": float(row["lat"]),
            "longitude": float(row["lon"]),
        }
    except Exception:
        logger.exception("Geocoding failed for free-form query: %s", cleaned)
        return None


def _normalize_geojson_ring(ring: object) -> list[tuple[float, float]] | None:
    if not isinstance(ring, list):
        return None
    points: list[tuple[float, float]] = []
    for coord in ring:
        if not isinstance(coord, (list, tuple)) or len(coord) < 2:
            return None
        try:
            lon = float(coord[0])
            lat = float(coord[1])
        except Exception:
            return None
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            return None
        pt = (lon, lat)
        if points and points[-1] == pt:
            continue  # skip consecutive duplicate vertices
        points.append(pt)
    if len(points) < 3:
        return None
    if points[0] != points[-1]:
        points.append(points[0])
    if len(points) < 4:
        return None
    return points


def _extract_geojson_polygons(boundary_geojson: object) -> list[list[list[tuple[float, float]]]]:
    if not isinstance(boundary_geojson, dict):
        return []
    geo_type = boundary_geojson.get("type")
    coords = boundary_geojson.get("coordinates")
    polygons: list[list[list[tuple[float, float]]]] = []
    if geo_type == "Polygon":
        if not isinstance(coords, list):
            return []
        rings: list[list[tuple[float, float]]] = []
        for ring in coords:
            normalized = _normalize_geojson_ring(ring)
            if normalized is None:
                return []
            rings.append(normalized)
        if rings:
            polygons.append(rings)
        return polygons
    if geo_type == "MultiPolygon":
        if not isinstance(coords, list):
            return []
        for polygon in coords:
            if not isinstance(polygon, list):
                return []
            rings: list[list[tuple[float, float]]] = []
            for ring in polygon:
                normalized = _normalize_geojson_ring(ring)
                if normalized is None:
                    return []
                rings.append(normalized)
            if rings:
                polygons.append(rings)
        return polygons
    return []


def _point_on_segment(
    point: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
    eps: float = 1e-12,
) -> bool:
    (px, py), (ax, ay), (bx, by) = point, a, b
    sq_len = (bx - ax) ** 2 + (by - ay) ** 2
    if sq_len <= eps * eps:
        # Degenerate zero-length segment: only "on" it if the point is at that vertex
        return (px - ax) ** 2 + (py - ay) ** 2 <= eps * eps
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    if abs(cross) > eps:
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    if dot < -eps:
        return False
    if dot - sq_len > eps:
        return False
    return True


def _point_in_ring(point_lon: float, point_lat: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if _point_on_segment((point_lon, point_lat), (x1, y1), (x2, y2)):
            return True
        intersects = ((y1 > point_lat) != (y2 > point_lat)) and (
            point_lon < ((x2 - x1) * (point_lat - y1) / ((y2 - y1) or 1e-15) + x1)
        )
        if intersects:
            inside = not inside
    return inside


def _point_in_polygon(point_lon: float, point_lat: float, polygon: list[list[tuple[float, float]]]) -> bool:
    if not polygon:
        return False
    outer = polygon[0]
    if not _point_in_ring(point_lon, point_lat, outer):
        return False
    for hole in polygon[1:]:
        if _point_in_ring(point_lon, point_lat, hole):
            return False
    return True


def _distance_point_to_segment_m(
    point_x: float,
    point_y: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
) -> float:
    dx = bx - ax
    dy = by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(point_x - ax, point_y - ay)
    t = ((point_x - ax) * dx + (point_y - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    closest_x = ax + t * dx
    closest_y = ay + t * dy
    return math.hypot(point_x - closest_x, point_y - closest_y)


def _project_lon_lat_m(lon: float, lat: float, ref_lon: float, ref_lat: float) -> tuple[float, float]:
    x = math.radians(lon - ref_lon) * _EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(lat - ref_lat) * _EARTH_RADIUS_M
    return x, y


def _distance_to_polygon_boundary_m(point_lon: float, point_lat: float, polygon: list[list[tuple[float, float]]]) -> float:
    nearest = float("inf")
    for ring in polygon:
        for i in range(len(ring) - 1):
            ax, ay = _project_lon_lat_m(ring[i][0], ring[i][1], point_lon, point_lat)
            bx, by = _project_lon_lat_m(ring[i + 1][0], ring[i + 1][1], point_lon, point_lat)
            distance = _distance_point_to_segment_m(0.0, 0.0, ax, ay, bx, by)
            if distance < nearest:
                nearest = distance
    return nearest


def _distance_to_geojson_boundary_m(point_lon: float, point_lat: float, boundary_geojson: object) -> float | None:
    polygons = _extract_geojson_polygons(boundary_geojson)
    if not polygons:
        return None
    nearest = float("inf")
    for polygon in polygons:
        distance = _distance_to_polygon_boundary_m(point_lon, point_lat, polygon)
        if distance < nearest:
            nearest = distance
    return nearest if math.isfinite(nearest) else None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    dlat = lat2r - lat1r
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))
    return _EARTH_RADIUS_M * c


def _ring_area_m2(ring: list[tuple[float, float]]) -> float:
    if len(ring) < 4:
        return 0.0
    ref_lat = sum(lat for _, lat in ring[:-1]) / max(1, len(ring) - 1)
    scale_x = _EARTH_RADIUS_M * math.cos(math.radians(ref_lat)) * math.pi / 180.0
    scale_y = _EARTH_RADIUS_M * math.pi / 180.0
    total = 0.0
    for i in range(len(ring) - 1):
        x1 = ring[i][0] * scale_x
        y1 = ring[i][1] * scale_y
        x2 = ring[i + 1][0] * scale_x
        y2 = ring[i + 1][1] * scale_y
        total += x1 * y2 - x2 * y1
    return abs(total) * 0.5


def _polygon_area_m2(polygon: list[list[tuple[float, float]]]) -> float:
    if not polygon:
        return 0.0
    outer = _ring_area_m2(polygon[0])
    holes = sum(_ring_area_m2(ring) for ring in polygon[1:])
    return max(0.0, outer - holes)


def _collect_nearby_candidates(
    point_lat: float,
    point_lon: float,
    rows: list[dict],
    boundary_radius_m: float,
    point_radius_m: float,
) -> list[dict]:
    """Collect all HOA candidates within the given radii."""
    suggestions: list[dict] = []
    for row in rows:
        hoa_name = str(row.get("hoa") or "").strip()
        if not hoa_name:
            continue
        boundary = row.get("boundary_geojson")
        polygons = _extract_geojson_polygons(boundary) if boundary else []
        row_lat = row.get("latitude")
        row_lon = row.get("longitude")
        geo_fields = {
            "latitude": float(row_lat) if row_lat is not None else None,
            "longitude": float(row_lon) if row_lon is not None else None,
            "boundary_geojson": boundary if boundary else None,
        }
        inside_areas: list[float] = []
        for polygon in polygons:
            if _point_in_polygon(point_lon, point_lat, polygon):
                inside_areas.append(_polygon_area_m2(polygon))
        if inside_areas:
            suggestions.append(
                {
                    "hoa": hoa_name,
                    "match_type": "inside_boundary",
                    "confidence": "high",
                    "default_selected": True,
                    "distance_m": 0.0,
                    "reason": "Address point is inside HOA boundary polygon.",
                    "_rank_area": min(inside_areas),
                    "_rank_distance": 0.0,
                    **geo_fields,
                }
            )
            continue

        if polygons:
            boundary_distance = _distance_to_geojson_boundary_m(point_lon, point_lat, boundary)
            if boundary_distance is not None and boundary_distance <= boundary_radius_m:
                suggestions.append(
                    {
                        "hoa": hoa_name,
                        "match_type": "near_boundary",
                        "confidence": "medium",
                        "default_selected": False,
                        "distance_m": round(float(boundary_distance), 1),
                        "reason": "Address point is close to HOA boundary polygon.",
                        "_rank_area": float("inf"),
                        "_rank_distance": float(boundary_distance),
                        **geo_fields,
                    }
                )
                continue

        # Fall through to point-distance check for HOAs with or without
        # polygons — an HOA whose boundary is far away may still have a
        # point marker within a mile.
        if row_lat is None or row_lon is None:
            continue
        try:
            point_distance = _haversine_m(point_lat, point_lon, float(row_lat), float(row_lon))
        except Exception:
            continue
        if point_distance <= point_radius_m:
            suggestions.append(
                {
                    "hoa": hoa_name,
                    "match_type": "nearby_point",
                    "confidence": "low",
                    "default_selected": False,
                    "distance_m": round(float(point_distance), 1),
                    "reason": "HOA has a nearby mapped point location.",
                    "_rank_area": float("inf"),
                    "_rank_distance": float(point_distance),
                    **geo_fields,
                }
            )
    return suggestions


def _suggestions_for_point(
    point_lat: float,
    point_lon: float,
    rows: list[dict],
    max_suggestions: int,
) -> list[dict]:
    # First pass: 2-mile radius
    suggestions = _collect_nearby_candidates(
        point_lat, point_lon, rows, _NEAR_BOUNDARY_M, _NEAR_POINT_M,
    )

    # If too few results, expand to 5 miles
    if len(suggestions) < _MIN_SUGGESTIONS:
        suggestions = _collect_nearby_candidates(
            point_lat, point_lon, rows, _EXPANDED_RADIUS_M, _EXPANDED_RADIUS_M,
        )

    cap = min(max_suggestions, _MAX_SUGGESTIONS_DEFAULT)

    priority = {"inside_boundary": 0, "near_boundary": 1, "nearby_point": 2}
    suggestions.sort(
        key=lambda item: (
            priority.get(str(item.get("match_type")), 99),
            float(item.get("_rank_area", float("inf"))),
            float(item.get("_rank_distance", float("inf"))),
            str(item.get("hoa", "")).casefold(),
        )
    )

    cleaned: list[dict] = []
    for item in suggestions[:cap]:
        entry: dict = {
            "hoa": str(item["hoa"]),
            "match_type": str(item["match_type"]),
            "confidence": str(item["confidence"]),
            "default_selected": bool(item["default_selected"]),
            "distance_m": float(item["distance_m"]),
            "reason": str(item["reason"]),
        }
        if item.get("latitude") is not None:
            entry["latitude"] = float(item["latitude"])
        if item.get("longitude") is not None:
            entry["longitude"] = float(item["longitude"])
        if item.get("boundary_geojson"):
            entry["boundary_geojson"] = item["boundary_geojson"]
        cleaned.append(entry)
    return cleaned


def _sanitize_geojson_position(position: object) -> list[float]:
    if not isinstance(position, (list, tuple)) or len(position) < 2:
        raise HTTPException(status_code=400, detail="boundary_geojson positions must be [longitude, latitude]")
    try:
        longitude = float(position[0])
        latitude = float(position[1])
    except Exception as exc:
        raise HTTPException(status_code=400, detail="boundary_geojson positions must contain numeric longitude/latitude") from exc
    if not (-180 <= longitude <= 180):
        raise HTTPException(status_code=400, detail="boundary_geojson longitude must be between -180 and 180")
    if not (-90 <= latitude <= 90):
        raise HTTPException(status_code=400, detail="boundary_geojson latitude must be between -90 and 90")
    return [round(longitude, 6), round(latitude, 6)]


def _sanitize_geojson_ring(ring: object) -> list[list[float]]:
    if not isinstance(ring, list) or not ring:
        raise HTTPException(status_code=400, detail="boundary_geojson ring must be a non-empty array")
    cleaned = [_sanitize_geojson_position(point) for point in ring]
    if len(cleaned) < 3:
        raise HTTPException(status_code=400, detail="boundary_geojson polygon ring needs at least 3 points")
    if cleaned[0] != cleaned[-1]:
        cleaned.append([cleaned[0][0], cleaned[0][1]])
    if len(cleaned) < 4:
        raise HTTPException(status_code=400, detail="boundary_geojson polygon ring must be closed")
    return cleaned


def _parse_boundary_geojson(raw_boundary: str | None) -> str | None:
    if raw_boundary is None:
        return None
    cleaned = raw_boundary.strip()
    if not cleaned:
        return None
    try:
        parsed = json.loads(cleaned)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="boundary_geojson must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="boundary_geojson must be a GeoJSON object")

    geo_type = parsed.get("type")
    coordinates = parsed.get("coordinates")
    if geo_type not in {"Polygon", "MultiPolygon"}:
        raise HTTPException(status_code=400, detail="boundary_geojson type must be Polygon or MultiPolygon")

    if geo_type == "Polygon":
        if not isinstance(coordinates, list) or not coordinates:
            raise HTTPException(status_code=400, detail="boundary_geojson Polygon coordinates are required")
        normalized = {"type": "Polygon", "coordinates": [_sanitize_geojson_ring(ring) for ring in coordinates]}
    else:
        if not isinstance(coordinates, list) or not coordinates:
            raise HTTPException(status_code=400, detail="boundary_geojson MultiPolygon coordinates are required")
        polygons: list[list[list[list[float]]]] = []
        for polygon in coordinates:
            if not isinstance(polygon, list) or not polygon:
                raise HTTPException(status_code=400, detail="boundary_geojson MultiPolygon polygon must include rings")
            polygons.append([_sanitize_geojson_ring(ring) for ring in polygon])
        normalized = {"type": "MultiPolygon", "coordinates": polygons}

    return json.dumps(normalized, separators=(",", ":"))


def _center_from_boundary_geojson(boundary_geojson: str | None) -> tuple[float, float] | None:
    if not boundary_geojson:
        return None
    try:
        parsed = json.loads(boundary_geojson)
    except Exception:
        return None

    points: list[tuple[float, float]] = []

    def collect(coords: object) -> None:
        if not isinstance(coords, list):
            return
        if coords and isinstance(coords[0], (int, float)) and len(coords) >= 2:
            points.append((float(coords[1]), float(coords[0])))
            return
        for child in coords:
            collect(child)

    collect(parsed.get("coordinates"))
    if not points:
        return None

    min_lat = min(point[0] for point in points)
    max_lat = max(point[0] for point in points)
    min_lon = min(point[1] for point in points)
    max_lon = max(point[1] for point in points)
    return ((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)


def _infer_and_store_location(hoa_name: str) -> dict | None:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        texts = db.get_chunk_text_for_hoa(conn, hoa_name, limit=220)
        if not texts:
            return None
        parts = _infer_location_parts(texts)
        if not any(parts.values()):
            return None
        coords = _geocode_from_parts(**parts)
        latitude, longitude = (coords if coords else (None, None))
        db.upsert_hoa_location(
            conn,
            hoa_name,
            street=parts["street"],
            city=parts["city"],
            state=parts["state"],
            postal_code=parts["postal_code"],
            latitude=latitude,
            longitude=longitude,
            source="inferred",
        )
        return db.get_hoa_location(conn, hoa_name)


def _find_hoa_matches(query: str, hoa_names: list[str]) -> list[dict]:
    needle = query.strip().casefold()
    if not needle:
        return []
    rows: list[dict] = []
    for name in hoa_names:
        hay = name.casefold()
        pos = hay.find(needle)
        if pos >= 0:
            rows.append({"hoa": name, "match_reason": "name_contains", "_exact": hay == needle, "_pos": pos, "_len": len(name)})
    rows.sort(key=lambda row: (0 if row["_exact"] else 1, int(row["_pos"]), int(row["_len"]), str(row["hoa"]).casefold()))
    return [{"hoa": str(row["hoa"]), "match_reason": str(row["match_reason"])} for row in rows]


def _render_searchable_document_html(hoa_name: str, relative_path: str, chunks: list[dict]) -> str:
    safe_hoa = html_escape(hoa_name)
    safe_doc = html_escape(relative_path)
    chunk_html: list[str] = []
    for row in chunks:
        start_page = row.get("start_page")
        end_page = row.get("end_page")
        if start_page is not None and end_page is not None:
            pages = f"Pages {start_page}-{end_page}" if start_page != end_page else f"Page {start_page}"
        else:
            pages = "Pages unknown"
        chunk_html.append(
            "<section class='chunk'>"
            f"<header>Chunk {row['chunk_index']} · {html_escape(pages)}</header>"
            f"<pre>{html_escape(row['text'])}</pre>"
            "</section>"
        )
    body = "\n".join(chunk_html) if chunk_html else "<p>No OCR text chunks found for this document.</p>"
    raw_pdf_href = f"/hoas/{quote(hoa_name, safe='')}/documents/file?path={quote(relative_path, safe='')}"
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Searchable OCR View</title>
    <style>
      body {{
        margin: 0;
        font-family: "Manrope", "Segoe UI", sans-serif;
        background: #f3f7ff;
        color: #16243a;
      }}
      main {{
        width: min(1100px, 94vw);
        margin: 24px auto 40px;
      }}
      .card {{
        background: #fff;
        border: 1px solid #d9e4f5;
        border-radius: 14px;
        padding: 14px;
      }}
      h1 {{
        margin: 0;
        font-size: 1.2rem;
      }}
      .meta {{
        margin-top: 4px;
        color: #5a6f90;
        font-size: 0.9rem;
      }}
      .toolbar {{
        margin-top: 12px;
        display: grid;
        gap: 8px;
        grid-template-columns: 1fr auto;
      }}
      input {{
        border: 1px solid #c8d9f2;
        border-radius: 10px;
        padding: 10px 12px;
        font: inherit;
      }}
      a {{
        border: 1px solid #c8d9f2;
        border-radius: 10px;
        background: #f8fbff;
        color: #163b7a;
        text-decoration: none;
        padding: 10px 12px;
        font-weight: 700;
      }}
      #status {{
        margin-top: 8px;
        color: #4b5f7e;
        font-size: 0.9rem;
      }}
      .chunks {{
        margin-top: 12px;
      }}
      .chunk {{
        border-top: 1px solid #ebf1fa;
        padding: 10px 0;
      }}
      .chunk:first-child {{
        border-top: 0;
      }}
      .chunk header {{
        font-size: 0.82rem;
        color: #49648b;
        font-weight: 700;
      }}
      pre {{
        margin: 8px 0 0;
        white-space: pre-wrap;
        word-wrap: break-word;
        font-family: "ui-monospace", "SFMono-Regular", Menlo, monospace;
        font-size: 0.85rem;
        line-height: 1.35;
      }}
      .hidden {{
        display: none;
      }}
    </style>
  </head>
  <body>
    <main>
      <article class="card">
        <h1>Searchable OCR View</h1>
        <div class="meta"><strong>HOA:</strong> {safe_hoa}<br><strong>Document:</strong> {safe_doc}</div>
        <div class="toolbar">
          <input id="query" type="search" placeholder="Search OCR text in this document..." />
          <a href="{raw_pdf_href}" target="_blank" rel="noopener noreferrer">Open Raw PDF</a>
        </div>
        <div id="status">Showing all chunks.</div>
        <div class="chunks" id="chunks">{body}</div>
      </article>
    </main>
    <script>
      const q = document.getElementById("query");
      const status = document.getElementById("status");
      const chunks = [...document.querySelectorAll(".chunk")];
      function applyFilter() {{
        const query = q.value.trim().toLowerCase();
        if (!query) {{
          chunks.forEach((el) => el.classList.remove("hidden"));
          status.textContent = "Showing all chunks.";
          return;
        }}
        let shown = 0;
        for (const el of chunks) {{
          const hit = el.innerText.toLowerCase().includes(query);
          el.classList.toggle("hidden", !hit);
          if (hit) shown += 1;
        }}
        status.textContent = `Showing ${{shown}} of ${{chunks.length}} chunks for '${{q.value.trim()}}'.`;
      }}
      q.addEventListener("input", applyFilter);
    </script>
  </body>
</html>"""


@app.head("/favicon.ico", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/robots.txt", include_in_schema=False)
def robots_txt() -> FileResponse:
    return FileResponse(STATIC_DIR / "robots.txt", media_type="text/plain")


_sitemap_cache: dict[str, Any] = {"xml": "", "ts": 0.0}
_SITEMAP_TTL = 3600  # 1 hour


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml() -> Response:
    """Dynamically generate sitemap covering all HOA pages."""
    now = time.time()
    if _sitemap_cache["xml"] and now - _sitemap_cache["ts"] < _SITEMAP_TTL:
        return Response(
            content=_sitemap_cache["xml"],
            media_type="application/xml",
            headers={"Cache-Control": f"public, max-age={_SITEMAP_TTL}"},
        )

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoas = db.list_hoas_for_sitemap(conn)

    urls: list[str] = []

    # Static pages
    static_pages = [
        ("/", "weekly", "1.0"),
        ("/about", "monthly", "0.8"),
        ("/login", "monthly", "0.6"),
        ("/register", "monthly", "0.6"),
        ("/legal", "monthly", "0.4"),
        ("/terms", "monthly", "0.4"),
        ("/privacy", "monthly", "0.4"),
    ]
    for path, freq, priority in static_pages:
        urls.append(
            f"  <url><loc>https://hoaproxy.org{path}</loc>"
            f"<changefreq>{freq}</changefreq><priority>{priority}</priority></url>"
        )

    # Collect states and cities
    states: dict[str, int] = {}
    cities: dict[tuple[str, str, str], int] = {}  # (state, city_slug, city_display) → count
    for h in hoas:
        s = (h["state"] or "").strip().lower()
        c = (h["city"] or "").strip()
        if not s or not c:
            continue
        states[s] = states.get(s, 0) + 1
        key = (s, db.slugify_city(c), c)
        cities[key] = cities.get(key, 0) + 1

    # State index pages
    for s in sorted(states):
        urls.append(
            f"  <url><loc>https://hoaproxy.org/hoa/{s}/</loc>"
            f"<changefreq>weekly</changefreq><priority>0.7</priority></url>"
        )

    # City index pages
    for (s, cs, _cd) in sorted(cities):
        urls.append(
            f"  <url><loc>https://hoaproxy.org/hoa/{s}/{cs}/</loc>"
            f"<changefreq>weekly</changefreq><priority>0.6</priority></url>"
        )

    # Individual HOA pages
    for h in hoas:
        s = (h["state"] or "").strip().lower()
        c = (h["city"] or "").strip()
        if not s or not c:
            continue
        cs = db.slugify_city(c)
        ns = db.slugify_name(h["hoa_name"])
        urls.append(
            f"  <url><loc>https://hoaproxy.org/hoa/{s}/{cs}/{ns}</loc>"
            f"<changefreq>weekly</changefreq><priority>0.5</priority></url>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>"
    )

    _sitemap_cache["xml"] = xml
    _sitemap_cache["ts"] = now

    return Response(
        content=xml,
        media_type="application/xml",
        headers={"Cache-Control": f"public, max-age={_SITEMAP_TTL}"},
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    if not STATIC_DIR.exists():
        raise HTTPException(status_code=404, detail="UI not available")
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/add-hoa", include_in_schema=False)
def add_hoa_page() -> FileResponse:
    if not STATIC_DIR.exists():
        raise HTTPException(status_code=404, detail="UI not available")
    page = STATIC_DIR / "add_hoa.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="Add HOA page not available")
    return FileResponse(page)


@app.get("/about", include_in_schema=False)
def about_page() -> FileResponse:
    if not STATIC_DIR.exists():
        raise HTTPException(status_code=404, detail="UI not available")
    page = STATIC_DIR / "about.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="About page not available")
    return FileResponse(page)


def _serve_static_page(filename: str) -> FileResponse:
    if not STATIC_DIR.exists():
        raise HTTPException(status_code=404, detail="UI not available")
    page = STATIC_DIR / filename
    if not page.exists():
        raise HTTPException(status_code=404, detail="Page not available")
    return FileResponse(page)


@functools.lru_cache(maxsize=1)
def _load_hoa_template() -> str:
    return (STATIC_DIR / "hoa.html").read_text()


def _render_hoa_page(
    hoa_name: str,
    hoa_id: int,
    city: str | None,
    state: str | None,
    doc_count: int,
) -> HTMLResponse:
    """Return hoa.html with server-injected SEO metadata and SSR data."""
    template = _load_hoa_template()

    # --- <title> ---
    title = html_escape(hoa_name)
    if city and state:
        title += f" | {html_escape(city)}, {html_escape(state.upper())}"
    title += " | HOAproxy"

    # --- meta description ---
    desc = f"View governing documents, CC&Rs, bylaws and rules for {html_escape(hoa_name)}"
    if city and state:
        desc += f" in {html_escape(city)}, {html_escape(state.upper())}"
    desc += f". {doc_count} document{'s' if doc_count != 1 else ''} available."

    # --- canonical URL ---
    canonical = f"https://hoaproxy.org{db.build_hoa_path(hoa_name, city, state)}"

    # --- SSR data for client JS ---
    ssr_json = json.dumps(
        {"hoaName": hoa_name, "hoaId": hoa_id, "city": city, "state": state, "docCount": doc_count},
        ensure_ascii=False,
    )

    # --- JSON-LD structured data ---
    ld = {"@context": "https://schema.org", "@type": "Organization", "name": hoa_name}
    if city and state:
        ld["address"] = {"@type": "PostalAddress", "addressLocality": city, "addressRegion": state.upper()}
    ld_json = json.dumps(ld, ensure_ascii=False)

    html = template

    # Inject title
    html = html.replace(
        "<title>HOAproxy | HOA Profile</title>",
        f"<title>{title}</title>",
    )

    # Inject meta description + canonical + JSON-LD before ga-measurement-id meta
    injected_head = (
        f'<meta name="description" content="{desc}">\n'
        f'    <link rel="canonical" href="{html_escape(canonical)}">\n'
        f'    <script type="application/ld+json">{ld_json}</script>\n'
        f'    <meta name="ga-measurement-id"'
    )
    html = html.replace('<meta name="ga-measurement-id"', injected_head)

    # Inject SSR data script before closing </head>
    html = html.replace("</head>", f'<script>window.__SSR_DATA__={ssr_json};</script>\n  </head>')

    # Pre-populate visible title
    html = html.replace(
        'id="hoaTitle">Loading HOA...</h2>',
        f'id="hoaTitle">{html_escape(hoa_name)}</h2>',
    )

    return HTMLResponse(content=html)


@app.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    return _serve_static_page("login.html")


@app.get("/register", include_in_schema=False)
def register_page() -> FileResponse:
    return _serve_static_page("register.html")


@app.get("/dashboard", include_in_schema=False)
def dashboard_page() -> FileResponse:
    return _serve_static_page("dashboard.html")


@app.get("/account", include_in_schema=False)
def account_page() -> FileResponse:
    return _serve_static_page("account.html")


@app.get("/become-delegate", include_in_schema=False)
def become_delegate_page() -> FileResponse:
    return _serve_static_page("become-delegate.html")


@app.get("/delegate/{delegate_id}", include_in_schema=False)
def delegate_profile_page(delegate_id: int) -> FileResponse:
    return _serve_static_page("delegate-profile.html")


@app.get("/assign-proxy", include_in_schema=False)
def assign_proxy_page() -> FileResponse:
    return _serve_static_page("assign-proxy.html")


@app.get("/proxy-sign/{proxy_id}", include_in_schema=False)
def proxy_sign_page(proxy_id: int) -> FileResponse:
    return _serve_static_page("proxy-sign.html")


@app.get("/my-proxies", include_in_schema=False)
def my_proxies_page() -> FileResponse:
    return _serve_static_page("my-proxies.html")


@app.get("/delegate-dashboard", include_in_schema=False)
def delegate_dashboard_page() -> FileResponse:
    return _serve_static_page("delegate-dashboard.html")


@app.get("/terms", include_in_schema=False)
def terms_page() -> FileResponse:
    return _serve_static_page("terms.html")


@app.get("/privacy", include_in_schema=False)
def privacy_page() -> FileResponse:
    return _serve_static_page("privacy.html")


@app.get("/legal", include_in_schema=False)
def legal_page() -> FileResponse:
    return _serve_static_page("legal.html")


@app.get("/verify-email", include_in_schema=False)
def verify_email_page() -> FileResponse:
    return _serve_static_page("verify-email.html")


@app.get("/forgot-password", include_in_schema=False)
def forgot_password_page() -> FileResponse:
    return _serve_static_page("forgot-password.html")


@app.get("/reset-password", include_in_schema=False)
def reset_password_page() -> FileResponse:
    return _serve_static_page("reset-password.html")


@app.get("/proxy-form", include_in_schema=False)
def proxy_form_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/assign-proxy", status_code=302)


@app.post("/hoas/{hoa_id}/participation")
def post_participation(
    hoa_id: int,
    body: ParticipationRequest,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    _check_rate_limit(request, limit=20)
    settings = load_settings()
    meeting_type = body.meeting_type.strip() if body.meeting_type else body.meeting_type
    notes = body.notes.strip() if body.notes else body.notes
    with db.get_connection(settings.db_path) as conn:
        hoa_row = conn.execute("SELECT id FROM hoas WHERE id = ?", (hoa_id,)).fetchone()
        if not hoa_row:
            raise HTTPException(status_code=404, detail="HOA not found")
        claim = db.get_membership_claim(conn, user["id"], hoa_id)
        if not claim:
            raise HTTPException(status_code=403, detail="You must be a member of this HOA to add participation data")
        record_id = participation_mod.add_participation_record(
            conn,
            hoa_id=hoa_id,
            meeting_date=body.meeting_date,
            meeting_type=meeting_type,
            total_units=body.total_units,
            votes_cast=body.votes_cast,
            quorum_required=body.quorum_required,
            quorum_met=body.quorum_met,
            entered_by_user_id=user["id"],
            notes=notes,
        )
    return {"id": record_id, "hoa_id": hoa_id}


@app.get("/hoas/{hoa_id}/participation")
def get_participation(hoa_id: int) -> list:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_row = conn.execute("SELECT id FROM hoas WHERE id = ?", (hoa_id,)).fetchone()
        if not hoa_row:
            raise HTTPException(status_code=404, detail="HOA not found")
        return participation_mod.get_participation_records(conn, hoa_id)


@app.get("/hoas/{hoa_id}/magic-number")
def get_magic_number(hoa_id: int) -> dict:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_row = conn.execute("SELECT id FROM hoas WHERE id = ?", (hoa_id,)).fetchone()
        if not hoa_row:
            raise HTTPException(status_code=404, detail="HOA not found")
        result = participation_mod.calculate_magic_number(conn, hoa_id)
    if result["data_points"] == 0:
        raise HTTPException(status_code=404, detail="No participation data yet for this HOA")
    result["hoa_id"] = hoa_id
    return result


@app.get("/add-participation", include_in_schema=False)
def add_participation_page() -> FileResponse:
    return _serve_static_page("add-participation.html")


@app.get("/participation", include_in_schema=False)
def participation_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/dashboard", status_code=302)


@app.get("/participation/{hoa_name:path}", include_in_schema=False)
def participation_page(hoa_name: str) -> FileResponse:
    return _serve_static_page("participation.html")


# ---------------------------------------------------------------------------
# HOA pages — hierarchical URLs: /hoa/{state}/{city}/{slug}
# Route order matters: specific routes first, legacy catch-all last.
# ---------------------------------------------------------------------------

@app.get("/hoa/{state}/{city}/{slug}", include_in_schema=False)
def hoa_profile_page(state: str, city: str, slug: str) -> HTMLResponse:
    """Serve an HOA profile page with server-rendered SEO content."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        result = db.resolve_hoa_by_hierarchical_slug(conn, state, city, slug)
    if result is None:
        raise HTTPException(status_code=404, detail="HOA not found")
    return _render_hoa_page(
        hoa_name=result["hoa_name"],
        hoa_id=result["hoa_id"],
        city=result["city"],
        state=result["state"],
        doc_count=result["doc_count"],
    )


_STATE_NAMES: dict[str, str] = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas",
    "ca": "California", "co": "Colorado", "ct": "Connecticut", "de": "Delaware",
    "fl": "Florida", "ga": "Georgia", "hi": "Hawaii", "id": "Idaho",
    "il": "Illinois", "in": "Indiana", "ia": "Iowa", "ks": "Kansas",
    "ky": "Kentucky", "la": "Louisiana", "me": "Maine", "md": "Maryland",
    "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota", "ms": "Mississippi",
    "mo": "Missouri", "mt": "Montana", "ne": "Nebraska", "nv": "Nevada",
    "nh": "New Hampshire", "nj": "New Jersey", "nm": "New Mexico", "ny": "New York",
    "nc": "North Carolina", "nd": "North Dakota", "oh": "Ohio", "ok": "Oklahoma",
    "or": "Oregon", "pa": "Pennsylvania", "ri": "Rhode Island", "sc": "South Carolina",
    "sd": "South Dakota", "tn": "Tennessee", "tx": "Texas", "ut": "Utah",
    "vt": "Vermont", "va": "Virginia", "wa": "Washington", "wv": "West Virginia",
    "wi": "Wisconsin", "wy": "Wyoming", "dc": "District of Columbia",
}


@app.get("/hoa/{state}/{city}/", include_in_schema=False)
def hoa_city_index(state: str, city: str) -> HTMLResponse:
    """List all HOAs in a city."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoas = db.list_hoas_in_city(conn, state, city)
    if not hoas:
        raise HTTPException(status_code=404, detail="No HOAs found for this city")
    city_display = hoas[0]["city"]
    state_upper = state.upper()
    state_full = _STATE_NAMES.get(state.lower(), state_upper)
    title = f"HOAs in {html_escape(city_display)}, {state_upper} | HOAproxy"
    desc = f"Browse {len(hoas)} homeowners associations in {html_escape(city_display)}, {state_upper}. View CC&Rs, bylaws, and governing documents."

    rows_html = []
    for h in hoas:
        href = html_escape(db.build_hoa_path(h["hoa_name"], h["city"], h["state"]))
        name = html_escape(h["hoa_name"])
        docs = h["doc_count"]
        rows_html.append(
            f'<li style="margin:8px 0"><a href="{href}" style="color:var(--accent);font-weight:700;text-decoration:none">{name}</a>'
            f' <span style="color:var(--muted);font-size:0.88rem">— {docs} doc{"s" if docs != 1 else ""}</span></li>'
        )

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{html_escape(desc)}">
<meta name="ga-measurement-id" content="G-BV7JXG4JDE">
<script src="/static/js/analytics.js"></script>
<title>{title}</title>
<link rel="stylesheet" href="/static/css/mobile.css">
<style>
@import url("https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&family=Space+Grotesk:wght@600;700&display=swap");
:root {{ --bg:#eef5ff; --ink:#12233a; --muted:#587091; --line:#d3e0f4; --accent:#1662f3; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; font-family:"Manrope","Segoe UI",sans-serif; color:var(--ink);
  background:linear-gradient(180deg,#f8fbff 0%,var(--bg) 54%,#edf3ff 100%); }}
.shell {{ width:min(860px,94vw); margin:40px auto 60px; }}
.card {{ border:1px solid var(--line); border-radius:18px; background:rgba(255,255,255,0.94);
  box-shadow:0 10px 32px rgba(16,40,73,0.09); padding:28px; }}
h1 {{ margin:0 0 6px; font-family:"Space Grotesk","Manrope",sans-serif; font-size:clamp(1.4rem,3vw,2rem); }}
.breadcrumb {{ margin-bottom:16px; font-size:0.9rem; color:var(--muted); }}
.breadcrumb a {{ color:var(--accent); text-decoration:none; font-weight:600; }}
ul {{ list-style:none; padding:0; }}
</style></head><body>
<main class="shell"><div class="card">
<div class="breadcrumb"><a href="/">HOAproxy</a> › <a href="/hoa/{state.lower()}/">{html_escape(state_full)}</a> › {html_escape(city_display)}</div>
<h1>HOAs in {html_escape(city_display)}, {state_upper}</h1>
<p style="color:var(--muted);margin:0 0 18px">{len(hoas)} homeowners association{"s" if len(hoas) != 1 else ""}</p>
<ul>{"".join(rows_html)}</ul>
</div></main></body></html>"""
    return HTMLResponse(content=html)


@app.get("/hoa/{state}/", include_in_schema=False)
def hoa_state_index(state: str) -> HTMLResponse:
    """List all cities with HOAs in a state."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        cities = db.list_cities_in_state(conn, state)
    if not cities:
        raise HTTPException(status_code=404, detail="No HOAs found for this state")
    state_upper = state.upper()
    state_full = _STATE_NAMES.get(state.lower(), state_upper)
    total = sum(c["hoa_count"] for c in cities)
    title = f"HOAs in {html_escape(state_full)} | HOAproxy"
    desc = f"Browse {total} homeowners associations across {len(cities)} cities in {html_escape(state_full)}."

    rows_html = []
    for c in cities:
        city_slug = db.slugify_city(c["city"])
        href = f"/hoa/{state.lower()}/{html_escape(city_slug)}/"
        name = html_escape(c["city"])
        count = c["hoa_count"]
        rows_html.append(
            f'<li style="margin:8px 0"><a href="{href}" style="color:var(--accent);font-weight:700;text-decoration:none">{name}</a>'
            f' <span style="color:var(--muted);font-size:0.88rem">— {count} HOA{"s" if count != 1 else ""}</span></li>'
        )

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{html_escape(desc)}">
<meta name="ga-measurement-id" content="G-BV7JXG4JDE">
<script src="/static/js/analytics.js"></script>
<title>{title}</title>
<link rel="stylesheet" href="/static/css/mobile.css">
<style>
@import url("https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&family=Space+Grotesk:wght@600;700&display=swap");
:root {{ --bg:#eef5ff; --ink:#12233a; --muted:#587091; --line:#d3e0f4; --accent:#1662f3; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; font-family:"Manrope","Segoe UI",sans-serif; color:var(--ink);
  background:linear-gradient(180deg,#f8fbff 0%,var(--bg) 54%,#edf3ff 100%); }}
.shell {{ width:min(860px,94vw); margin:40px auto 60px; }}
.card {{ border:1px solid var(--line); border-radius:18px; background:rgba(255,255,255,0.94);
  box-shadow:0 10px 32px rgba(16,40,73,0.09); padding:28px; }}
h1 {{ margin:0 0 6px; font-family:"Space Grotesk","Manrope",sans-serif; font-size:clamp(1.4rem,3vw,2rem); }}
.breadcrumb {{ margin-bottom:16px; font-size:0.9rem; color:var(--muted); }}
.breadcrumb a {{ color:var(--accent); text-decoration:none; font-weight:600; }}
ul {{ list-style:none; padding:0; }}
</style></head><body>
<main class="shell"><div class="card">
<div class="breadcrumb"><a href="/">HOAproxy</a> › {html_escape(state_full)}</div>
<h1>HOAs in {html_escape(state_full)}</h1>
<p style="color:var(--muted);margin:0 0 18px">{total} homeowners association{"s" if total != 1 else ""} across {len(cities)} cit{"ies" if len(cities) != 1 else "y"}</p>
<ul>{"".join(rows_html)}</ul>
</div></main></body></html>"""
    return HTMLResponse(content=html)


@app.get("/hoa/{old_slug}", include_in_schema=False)
def hoa_legacy_redirect(old_slug: str):
    """301 redirect from old flat URL to new hierarchical URL."""
    from fastapi.responses import RedirectResponse
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        result = db.resolve_hoa_by_slug(conn, old_slug)
    if result is None:
        raise HTTPException(status_code=404, detail="HOA not found")
    new_url = db.build_hoa_path(result["hoa_name"], result.get("city"), result.get("state"))
    # If HOA has no state/city, serve the page directly instead of redirecting to itself
    if not result.get("state") or not result.get("city"):
        with db.get_connection(settings.db_path) as conn:
            doc_count = conn.execute(
                "SELECT COUNT(*) FROM documents d JOIN hoas h ON h.id = d.hoa_id WHERE h.name = ?",
                (result["hoa_name"],),
            ).fetchone()[0]
        return _render_hoa_page(
            hoa_name=result["hoa_name"],
            hoa_id=result["hoa_id"],
            city=result.get("city"),
            state=result.get("state"),
            doc_count=doc_count,
        )
    return RedirectResponse(url=new_url, status_code=301)


@app.get("/healthz")
def health() -> dict:
    settings = load_settings()
    required_tables = {"hoas", "users", "sessions", "membership_claims", "delegates",
                       "proxy_assignments", "proxy_audit",
                       "proposals", "proposal_cosigners", "proposal_upvotes"}
    try:
        with db.get_connection(settings.db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            existing = {r["name"] for r in rows}
            missing = required_tables - existing
    except Exception as exc:
        logger.error("Health check DB error: %s", exc)
        return {"status": "degraded", "error": str(exc)}
    if missing:
        logger.warning("Health check missing tables: %s", missing)
        return {"status": "degraded", "missing": list(missing)}
    return {"status": "ok"}


# /admin/bulk-import was removed in PR-5. The per-corpus uploaders that
# consumed it (Alexandria, TREC, Breckenridge) have been deleted; new HOAs
# enter the system one at a time via /upload (agent-driven).


# ---------------------------------------------------------------------------
# Cost tracker endpoints
# ---------------------------------------------------------------------------


def _require_admin(request: Request) -> None:
    settings = load_settings()
    admin_key = settings.jwt_secret
    auth_header = request.headers.get("Authorization", "")
    if not admin_key or auth_header != f"Bearer {admin_key}":
        raise HTTPException(status_code=403, detail="Forbidden")


class FixedCostRequest(BaseModel):
    service: str
    description: Optional[str] = None
    amount_usd: float
    frequency: str = "monthly"


class FixedCostUpdateRequest(BaseModel):
    service: Optional[str] = None
    description: Optional[str] = None
    amount_usd: Optional[float] = None
    frequency: Optional[str] = None
    active: Optional[bool] = None


@app.get("/admin/costs")
def admin_costs(request: Request, month: Optional[str] = None):
    """Combined cost summary: metered API usage + fixed subscriptions."""
    _require_admin(request)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        metered = db.get_usage_summary(conn, month=month)
        fixed = db.list_fixed_costs(conn, active_only=True)

    metered_by_service = {}
    total_metered = 0.0
    for row in metered:
        metered_by_service[row["service"]] = {
            "total_units": row["total_units"],
            "unit_type": row["unit_type"],
            "est_cost_usd": round(row["total_est_cost_usd"] or 0, 6),
        }
        total_metered += row["total_est_cost_usd"] or 0

    total_fixed = sum(fc["monthly_equiv"] for fc in fixed)
    return {
        "period": month or "all-time",
        "metered": metered_by_service,
        "fixed": [
            {
                "id": fc["id"],
                "service": fc["service"],
                "description": fc["description"],
                "monthly_equiv": fc["monthly_equiv"],
            }
            for fc in fixed
        ],
        "total_metered_usd": round(total_metered, 6),
        "total_fixed_usd": round(total_fixed, 2),
        "total_usd": round(total_metered + total_fixed, 2),
    }


@app.get("/admin/costs/daily")
def admin_costs_daily(request: Request, month: Optional[str] = None):
    """Daily metered cost breakdown."""
    _require_admin(request)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        rows = db.get_usage_daily(conn, month=month)
    return {"period": month or "all-time", "daily": rows}


@app.get("/admin/costs/docai-alert")
def admin_docai_alert(
    request: Request,
    threshold_usd: float = 10.0,
    hours: int = 24,
    notify: bool = False,
):
    """Cost-alert endpoint for cron (cron-job.org).

    Returns last-N-hours DocAI spend and whether it exceeds the threshold.
    If notify=true and over threshold, sends an email via the existing
    email service to settings.cost_report_email.

    Example cron:
      curl -fsS -H "Authorization: Bearer $JWT_SECRET" \\
        "https://hoaproxy.org/admin/costs/docai-alert?threshold_usd=10&notify=true"
    """
    _require_admin(request)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        spend = db.get_recent_service_cost_usd(conn, "docai", hours=hours)
    over = spend > threshold_usd

    notified = False
    if over and notify and settings.cost_report_email:
        try:
            from hoaware import email_service

            subject = f"[hoaproxy] DocAI spend ${spend:.2f} over ${threshold_usd:.2f} in {hours}h"
            html = (
                f"<p>Document AI spend for the last {hours}h reached "
                f"<b>${spend:.2f}</b>, above the configured threshold of "
                f"<b>${threshold_usd:.2f}</b>.</p>"
                f"<p>Check <code>/admin/costs</code> to investigate.</p>"
                f"<p><code>DAILY_DOCAI_BUDGET_USD</code> currently caps "
                f"<code>/upload</code> at ${DAILY_DOCAI_BUDGET_USD:.2f}/day.</p>"
            )
            email_service._send_email(
                to=[settings.cost_report_email],
                subject=subject,
                html=html,
            )
            notified = True
        except Exception:
            logger.exception("DocAI alert email failed")

    return {
        "service": "docai",
        "hours": hours,
        "spend_usd": round(spend, 4),
        "threshold_usd": threshold_usd,
        "over_threshold": over,
        "daily_upload_cap_usd": DAILY_DOCAI_BUDGET_USD,
        "notified": notified,
    }


@app.get("/admin/costs/fixed")
def admin_list_fixed_costs(request: Request, all: bool = False):
    _require_admin(request)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        costs = db.list_fixed_costs(conn, active_only=not all)
    return {"fixed_costs": costs}


@app.post("/admin/costs/fixed")
def admin_create_fixed_cost(request: Request, body: FixedCostRequest):
    _require_admin(request)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        cost_id = db.create_fixed_cost(
            conn,
            service=body.service,
            description=body.description,
            amount_usd=body.amount_usd,
            frequency=body.frequency,
        )
    return {"id": cost_id, "status": "created"}


@app.put("/admin/costs/fixed/{cost_id}")
def admin_update_fixed_cost(cost_id: int, request: Request, body: FixedCostUpdateRequest):
    _require_admin(request)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        updated = db.update_fixed_cost(
            conn,
            cost_id,
            service=body.service,
            description=body.description,
            amount_usd=body.amount_usd,
            frequency=body.frequency,
            active=body.active,
        )
    if not updated:
        raise HTTPException(status_code=404, detail="Fixed cost not found")
    return updated


@app.delete("/admin/costs/fixed/{cost_id}")
def admin_delete_fixed_cost(cost_id: int, request: Request):
    _require_admin(request)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.delete_fixed_cost(conn, cost_id)
    return {"status": "deactivated"}


@app.post("/admin/costs/report")
def admin_send_cost_report(request: Request, email: Optional[str] = None):
    """Manually trigger a cost report email."""
    _require_admin(request)
    from hoaware.email_service import send_cost_report

    settings = load_settings()
    to_email = email or settings.cost_report_email
    if not to_email:
        raise HTTPException(status_code=400, detail="No email provided and COST_REPORT_EMAIL not set")
    ok = send_cost_report(to_email=to_email)
    if not ok:
        raise HTTPException(status_code=502, detail="Failed to send report email")
    return {"status": "sent", "to": to_email}


@app.post("/admin/refit-polygon-centers")
def admin_refit_polygon_centers(request: Request, distance_threshold_km: float = 1.5):
    """When an HOA has a polygon AND a lat/lon farther than threshold from the
    polygon centroid, the lat/lon is almost certainly stale city-center
    geocoding from the pre-fix /upload code path. Replace it with the polygon
    centroid.
    """
    _require_admin(request)
    import math

    settings = load_settings()
    fixed = 0
    filled = 0
    examined = 0

    def _polygon_center(s: str):
        center = _center_from_boundary_geojson(s)
        return center  # already (lat, lon)

    def _km(lat1, lon1, lat2, lon2):
        dx = (lon2 - lon1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
        dy = (lat2 - lat1) * 111.32
        return math.sqrt(dx * dx + dy * dy)

    with db.get_connection(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT h.name, l.latitude, l.longitude, l.boundary_geojson
            FROM hoa_locations l JOIN hoas h ON h.id = l.hoa_id
            WHERE l.boundary_geojson IS NOT NULL
            """
        ).fetchall()
        examined = len(rows)
        updates: list[tuple[str, float, float]] = []
        for r in rows:
            center = _polygon_center(r["boundary_geojson"])
            if not center:
                continue
            new_lat, new_lon = center
            if r["latitude"] is None or r["longitude"] is None:
                updates.append((r["name"], new_lat, new_lon))
                filled += 1
                continue
            d = _km(r["latitude"], r["longitude"], new_lat, new_lon)
            if d > distance_threshold_km:
                updates.append((r["name"], new_lat, new_lon))
                fixed += 1
        for name, new_lat, new_lon in updates:
            conn.execute(
                """
                UPDATE hoa_locations
                SET latitude = ?, longitude = ?
                WHERE hoa_id = (SELECT id FROM hoas WHERE name = ?)
                """,
                (new_lat, new_lon, name),
            )
        conn.commit()
    return {
        "examined_with_polygon": examined,
        "fixed_moved_to_centroid": fixed,
        "filled_was_missing": filled,
        "distance_threshold_km": distance_threshold_km,
    }


@app.post("/admin/backfill-categories")
def admin_backfill_categories(request: Request, body: dict, apply_hidden_reason: bool = False):
    """Backfill documents.category from a posted audit-report JSON body.

    Body shape: same as data/doc_audit_report.json — {results: [{name, documents: [...]}]}.
    Operator runs:
      curl -X POST -H "Authorization: Bearer $JWT_SECRET" \
        -H "Content-Type: application/json" \
        --data-binary @data/doc_audit_report.json \
        "$URL/admin/backfill-categories?apply_hidden_reason=true"
    """
    _require_admin(request)
    from collections import Counter
    from hoaware.doc_classifier import REJECT_JUNK, REJECT_PII

    settings = load_settings()
    results = body.get("results") or []
    matched = 0
    not_found_hoa = 0
    not_found_doc = 0
    by_category: Counter = Counter()
    hidden_count = 0

    with db.get_connection(settings.db_path) as conn:
        hoa_rows = conn.execute("SELECT id, name FROM hoas").fetchall()
        hoa_id_by_lower = {row["name"].lower(): int(row["id"]) for row in hoa_rows}

        for entry in results:
            hoa_name = (entry.get("name") or "").lower()
            hoa_id = hoa_id_by_lower.get(hoa_name)
            if not hoa_id:
                not_found_hoa += 1
                continue

            doc_rows = conn.execute(
                "SELECT id, relative_path FROM documents WHERE hoa_id = ?",
                (hoa_id,),
            ).fetchall()
            by_basename = {
                str(row["relative_path"]).rsplit("/", 1)[-1].lower(): int(row["id"])
                for row in doc_rows
            }

            for doc in entry.get("documents", []):
                fname = (doc.get("filename") or "").lower()
                doc_id = by_basename.get(fname)
                if not doc_id:
                    not_found_doc += 1
                    continue
                category = doc.get("category") or "unknown"
                te_val = 1 if doc.get("is_digital") else 0
                hidden = None
                if apply_hidden_reason:
                    if category in REJECT_PII:
                        hidden = f"pii:{category}"
                        hidden_count += 1
                    elif category in REJECT_JUNK:
                        hidden = f"junk:{category}"
                        hidden_count += 1
                if hidden:
                    conn.execute(
                        """UPDATE documents SET category = ?, text_extractable = ?, hidden_reason = ?
                           WHERE id = ?""",
                        (category, te_val, hidden, doc_id),
                    )
                else:
                    conn.execute(
                        "UPDATE documents SET category = ?, text_extractable = ? WHERE id = ?",
                        (category, te_val, doc_id),
                    )
                matched += 1
                by_category[category] += 1
        conn.commit()

    return {
        "matched": matched,
        "not_found_hoa": not_found_hoa,
        "not_found_doc": not_found_doc,
        "marked_hidden": hidden_count,
        "by_category": dict(by_category.most_common()),
    }


@app.post("/admin/backup")
def admin_backup(request: Request):
    """Snapshot the SQLite DB and uploaded PDFs to GCS, with retention."""
    _require_admin(request)
    import tarfile
    import tempfile
    from datetime import datetime, timezone
    from google.cloud import storage as gcs

    settings = load_settings()
    bucket_name = os.environ.get("BACKUP_GCS_BUCKET", "hoaproxy-backups")
    max_backups = int(os.environ.get("BACKUP_MAX_COPIES", "7"))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    client = gcs.Client()
    gcs_bucket = client.bucket(bucket_name)
    uploaded_blobs: list[str] = []
    errors: list[str] = []

    # --- 1. SQLite DB snapshot (incremental backup, low memory) ---
    # Copies 50 pages at a time with 50ms sleep between batches,
    # avoiding the memory spike of VACUUM INTO.
    tmp_dir = tempfile.mkdtemp()
    tmp_db = os.path.join(tmp_dir, "backup.db")
    try:
        import sqlite3 as _sqlite3
        src = _sqlite3.connect(settings.db_path)
        dst = _sqlite3.connect(tmp_db)
        src.backup(dst, pages=50, sleep=0.05)
        dst.close()
        src.close()
        db_blob_name = f"db/hoa_index-{stamp}.db"
        gcs_bucket.blob(db_blob_name).upload_from_filename(tmp_db)
        uploaded_blobs.append(db_blob_name)
    except Exception as exc:
        errors.append(f"db: {exc}")
    finally:
        if os.path.exists(tmp_db):
            os.unlink(tmp_db)
        if os.path.isdir(tmp_dir):
            os.rmdir(tmp_dir)

    # --- 2. PDF docs (upsert: only upload new/changed files) ---
    docs_root = settings.docs_root
    if docs_root.exists() and any(docs_root.iterdir()):
        try:
            # Build index of existing remote docs by relative path → size
            remote_docs: dict[str, int] = {}
            for b in gcs_bucket.list_blobs(prefix="docs/files/"):
                remote_docs[b.name.removeprefix("docs/files/")] = b.size or 0

            upserted = 0
            for doc_path in sorted(docs_root.rglob("*")):
                if not doc_path.is_file():
                    continue
                rel = str(doc_path.relative_to(docs_root))
                local_size = doc_path.stat().st_size
                if rel in remote_docs and remote_docs[rel] == local_size:
                    continue  # unchanged
                gcs_bucket.blob(f"docs/files/{rel}").upload_from_filename(
                    str(doc_path)
                )
                upserted += 1
            if upserted:
                uploaded_blobs.append(f"docs/files/ ({upserted} upserted)")
        except Exception as exc:
            errors.append(f"docs: {exc}")

    # --- 3. Retention: keep only the most recent DB snapshots ---
    # (Docs use upsert into docs/files/ — no rotation needed)
    try:
        db_blobs = list(gcs_bucket.list_blobs(prefix="db/"))
        db_blobs.sort(key=lambda b: b.name, reverse=True)
        for old_blob in db_blobs[max_backups:]:
            old_blob.delete()
    except Exception as exc:
        errors.append(f"retention(db): {exc}")

    if errors and not uploaded_blobs:
        raise HTTPException(status_code=500, detail="; ".join(errors))

    return {
        "status": "ok" if not errors else "partial",
        "uploaded": uploaded_blobs,
        "errors": errors or None,
        "retention": f"keeping last {max_backups}",
    }


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@app.post("/auth/register", response_model=AuthResponse)
def register(request: Request, body: RegisterRequest, background_tasks: BackgroundTasks):
    _check_rate_limit(request, limit=10)
    import secrets as _secrets
    from datetime import timedelta
    from hoaware.email_service import send_verification_email
    settings = load_settings()
    display_name = body.display_name.strip() if body.display_name else None
    with db.get_connection(settings.db_path) as conn:
        existing = db.get_user_by_email(conn, body.email)
        if existing:
            raise HTTPException(status_code=409, detail="Email already registered")
        pw_hash = hash_password(body.password)
        user_id = db.create_user(conn, email=body.email, password_hash=pw_hash, display_name=display_name)
        token, jti, expires = create_access_token(user_id, settings)
        db.create_session(conn, user_id=user_id, token_jti=jti, expires_at=expires.isoformat())
        # Create email verification token (24-hour expiry)
        verify_token = _secrets.token_urlsafe(32)
        verify_expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        db.create_verification_token(conn, user_id=user_id, token=verify_token, expires_at=verify_expires)
    # Send verification email in background (non-blocking)
    background_tasks.add_task(
        send_verification_email,
        email=body.email,
        token=verify_token,
        base_url=settings.app_base_url,
    )
    return AuthResponse(user_id=user_id, token=token)


@app.post("/auth/login", response_model=AuthResponse)
def login(request: Request, body: LoginRequest):
    _check_rate_limit(request, limit=10)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        user = db.get_user_by_email(conn, body.email)
        if not user or not user["password_hash"] or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        token, jti, expires = create_access_token(user["id"], settings)
        db.create_session(conn, user_id=user["id"], token_jti=jti, expires_at=expires.isoformat())
    return AuthResponse(user_id=user["id"], token=token)


@app.post("/auth/logout")
def logout(user: dict = Depends(get_current_user)):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.delete_session_by_jti(conn, user["_jti"])
    return {"ok": True}


@app.get("/auth/verify-email")
def verify_email(token: str):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        record = db.get_verification_token(conn, token)
        if not record:
            raise HTTPException(status_code=400, detail="Invalid or expired verification link")
        expires_at = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
        if expires_at < datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="Verification link has expired. Please request a new one.")
        db.mark_user_verified(conn, record["user_id"])
    return {"ok": True}


@app.post("/auth/resend-verification")
def resend_verification(request: Request, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    import secrets as _secrets
    from datetime import timedelta
    from hoaware.email_service import send_verification_email
    _check_rate_limit(request, limit=5)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        user_row = db.get_user_by_id(conn, user["id"])
        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")
        if user_row.get("verified_at"):
            return {"ok": True, "already_verified": True}
        verify_token = _secrets.token_urlsafe(32)
        verify_expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        db.create_verification_token(conn, user_id=user["id"], token=verify_token, expires_at=verify_expires)
    background_tasks.add_task(
        send_verification_email,
        email=user_row["email"],
        token=verify_token,
        base_url=settings.app_base_url,
    )
    return {"ok": True, "already_verified": False}


@app.post("/auth/forgot-password")
def forgot_password(request: Request, background_tasks: BackgroundTasks, body: dict = Body(...)):
    import secrets as _secrets
    from datetime import timedelta
    from hoaware.email_service import send_password_reset_email
    _check_rate_limit(request, limit=5)
    email = (body.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="Email is required")
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        user = db.get_user_by_email(conn, email)
        if user:
            reset_token = _secrets.token_urlsafe(32)
            reset_expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            db.create_password_reset_token(conn, user_id=user["id"], token=reset_token, expires_at=reset_expires)
            background_tasks.add_task(
                send_password_reset_email,
                email=user["email"],
                token=reset_token,
                base_url=settings.app_base_url,
            )
    # Always return success to avoid user enumeration
    return {"ok": True}


@app.post("/auth/reset-password")
def reset_password(request: Request, body: dict = Body(...)):
    from hoaware.auth import hash_password
    _check_rate_limit(request, limit=10)
    token = (body.get("token") or "").strip()
    new_password = body.get("password") or ""
    if not token:
        raise HTTPException(status_code=422, detail="Token is required")
    if len(new_password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        record = db.get_password_reset_token(conn, token)
        if not record:
            raise HTTPException(status_code=400, detail="Invalid or expired reset link")
        expires_at = datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            raise HTTPException(status_code=400, detail="Reset link has expired. Please request a new one.")
        new_hash = hash_password(new_password)
        db.consume_password_reset_token(conn, token, new_hash)
    return {"ok": True}


@app.get("/auth/me", response_model=UserMeResponse)
def me(user: dict = Depends(get_current_user)):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        claims = db.list_membership_claims_for_user(conn, user["id"])
        user_row = db.get_user_by_id(conn, user["id"])
    return UserMeResponse(
        user_id=user["id"],
        email=user["email"],
        display_name=user.get("display_name"),
        hoas=[{"hoa_id": c["hoa_id"], "hoa_name": c["hoa_name"], "unit_number": c["unit_number"], "status": c["status"]} for c in claims],
        email_verified=bool(user_row and user_row.get("verified_at")),
    )


@app.put("/auth/me", response_model=UserMeResponse)
def update_me(body: UserUpdateRequest, request: Request, user: dict = Depends(get_current_user)):
    _check_rate_limit(request, limit=10)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        # Password change requires current_password (unless Google-only account setting initial password)
        new_hash = None
        if body.new_password:
            user_row = db.get_user_by_id(conn, user["id"])
            if user_row and user_row["password_hash"]:
                # Existing password — require current_password to change
                if not body.current_password:
                    raise HTTPException(status_code=400, detail="Current password is required to set a new password")
                if not verify_password(body.current_password, user_row["password_hash"]):
                    raise HTTPException(status_code=403, detail="Current password is incorrect")
            # Google-only users can set an initial password without current_password
            if len(body.new_password) < 8:
                raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
            new_hash = hash_password(body.new_password)

        # Email change: check uniqueness
        if body.email and body.email.strip().lower() != user["email"]:
            existing = db.get_user_by_email(conn, body.email)
            if existing:
                raise HTTPException(status_code=409, detail="That email is already in use")

        db.update_user(
            conn, user["id"],
            display_name=body.display_name,
            email=body.email,
            password_hash=new_hash,
        )
        updated = db.get_user_by_id(conn, user["id"])
        claims = db.list_membership_claims_for_user(conn, user["id"])
    return UserMeResponse(
        user_id=updated["id"],
        email=updated["email"],
        display_name=updated.get("display_name"),
        hoas=[{"hoa_id": c["hoa_id"], "hoa_name": c["hoa_name"], "unit_number": c["unit_number"], "status": c["status"]} for c in claims],
        email_verified=bool(updated.get("verified_at")),
    )


# ---------------------------------------------------------------------------
# Google OAuth
# ---------------------------------------------------------------------------

@app.get("/auth/google/login")
async def google_login(request: Request):
    """Redirect user to Google's OAuth consent screen."""
    from authlib.integrations.starlette_client import OAuth as StarletteOAuth
    settings = load_settings()
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=501, detail="Google login is not configured")
    oauth = StarletteOAuth()
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    redirect_uri = settings.app_base_url.rstrip("/") + "/auth/google/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback")
async def google_callback(request: Request):
    """Handle the OAuth callback from Google, create/login the user, and redirect to dashboard."""
    from authlib.integrations.starlette_client import OAuth as StarletteOAuth
    settings = load_settings()
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=501, detail="Google login is not configured")
    oauth = StarletteOAuth()
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    try:
        token_data = await oauth.google.authorize_access_token(request)
    except Exception:
        return HTMLResponse(
            '<script>alert("Google sign-in failed. Please try again.");window.location.href="/login";</script>',
            status_code=400,
        )
    userinfo = token_data.get("userinfo")
    if not userinfo or not userinfo.get("email"):
        raise HTTPException(status_code=400, detail="Could not retrieve email from Google")

    google_id = userinfo["sub"]
    email = userinfo["email"].strip().lower()
    display_name = userinfo.get("name")

    with db.get_connection(settings.db_path) as conn:
        # Try to find user by google_id first, then by email
        user = db.get_user_by_google_id(conn, google_id)
        if not user:
            user = db.get_user_by_email(conn, email)
            if user:
                # Link Google ID to existing account
                db.link_google_id(conn, user["id"], google_id)
            else:
                # Create new user (no password, verified via Google)
                now = datetime.now(timezone.utc).isoformat()
                user_id = db.create_user(
                    conn,
                    email=email,
                    display_name=display_name,
                    google_id=google_id,
                    verified_at=now,
                )
                user = db.get_user_by_id(conn, user_id)

        # Mark email as verified if not already (Google verified it)
        if not user.get("verified_at"):
            db.mark_user_verified(conn, user["id"])

        # Create session
        jwt_token, jti, expires = create_access_token(user["id"], settings)
        db.create_session(conn, user_id=user["id"], token_jti=jti, expires_at=expires.isoformat())

    # Determine if this was a new registration or returning login
    is_new_user = user.get("created_at", "") >= (datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=1)).isoformat()
    ga_event = "sign_up" if is_new_user else "login"

    # Return an HTML page that stores the JWT and redirects to dashboard
    return HTMLResponse(f"""<!doctype html>
<html><head><title>Signing in...</title>
<script async src="https://www.googletagmanager.com/gtag/js?id=G-BV7JXG4JDE"></script>
<script>window.dataLayer=window.dataLayer||[];function gtag(){{dataLayer.push(arguments)}}gtag('js',new Date());gtag('config','G-BV7JXG4JDE');</script>
</head>
<body>
<script>
localStorage.setItem("hoaware_token", {json.dumps(jwt_token)});
gtag("event", "{ga_event}", {{method: "google"}});
fetch("/auth/me", {{headers: {{"Authorization": "Bearer " + {json.dumps(jwt_token)}}}}})
  .then(r => r.json())
  .then(u => {{localStorage.setItem("hoaware_user", JSON.stringify(u)); window.location.href = "/dashboard";}})
  .catch(() => {{window.location.href = "/dashboard";}});
</script>
</body></html>""")


# ---------------------------------------------------------------------------
# Membership endpoints
# ---------------------------------------------------------------------------

@app.post("/user/hoas/{hoa_id}/claim", response_model=MembershipClaimResponse)
def claim_membership(hoa_id: int, body: MembershipClaimRequest, request: Request, user: dict = Depends(get_current_user)):
    _check_rate_limit(request, limit=20)
    unit_number = body.unit_number.strip() if body.unit_number else None
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        # Verify HOA exists
        hoa_row = conn.execute("SELECT id, name FROM hoas WHERE id = ?", (hoa_id,)).fetchone()
        if not hoa_row:
            raise HTTPException(status_code=404, detail="HOA not found")
        existing = db.get_membership_claim(conn, user["id"], hoa_id)
        if existing:
            raise HTTPException(status_code=409, detail="You have already claimed membership in this HOA")
        claim_id = db.create_membership_claim(conn, user_id=user["id"], hoa_id=hoa_id, unit_number=unit_number)
        claim = conn.execute("SELECT * FROM membership_claims WHERE id = ?", (claim_id,)).fetchone()
    return MembershipClaimResponse(
        id=claim_id, user_id=user["id"], hoa_id=hoa_id,
        hoa_name=hoa_row["name"], unit_number=dict(claim).get("unit_number"), status="self_declared",
    )


class MembershipClaimByNameRequest(BaseModel):
    hoa_name: str
    unit_number: str | None = None


@app.post("/user/hoas/claim-by-name", response_model=MembershipClaimResponse)
def claim_membership_by_name(body: MembershipClaimByNameRequest, user: dict = Depends(get_current_user)):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_row = conn.execute("SELECT id, name FROM hoas WHERE name = ?", (body.hoa_name,)).fetchone()
        if not hoa_row:
            raise HTTPException(status_code=404, detail="HOA not found")
        hoa_id = hoa_row["id"]
        existing = db.get_membership_claim(conn, user["id"], hoa_id)
        if existing:
            raise HTTPException(status_code=409, detail="You have already claimed membership in this HOA")
        claim_id = db.create_membership_claim(conn, user_id=user["id"], hoa_id=hoa_id, unit_number=body.unit_number)
    return MembershipClaimResponse(
        id=claim_id, user_id=user["id"], hoa_id=hoa_id,
        hoa_name=hoa_row["name"], unit_number=body.unit_number, status="self_declared",
    )


@app.get("/user/hoas")
def list_user_hoas(user: dict = Depends(get_current_user)):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        claims = db.list_membership_claims_for_user(conn, user["id"])
    return claims


# ---------------------------------------------------------------------------
# Delegate endpoints
# ---------------------------------------------------------------------------

@app.post("/delegates/register", response_model=DelegateResponse)
def register_delegate(body: DelegateRegisterRequest, request: Request, user: dict = Depends(get_current_user)):
    _check_rate_limit(request, limit=20)
    settings = load_settings()
    bio = body.bio.strip() if body.bio else None
    contact_email = body.contact_email.strip() if body.contact_email else None
    if not _is_full_name(user.get("display_name")):
        raise HTTPException(
            status_code=400,
            detail="Delegates must have a full first and last name on their account before registering.",
        )
    with db.get_connection(settings.db_path) as conn:
        # Must be a member of the HOA
        claim = db.get_membership_claim(conn, user["id"], body.hoa_id)
        if not claim:
            raise HTTPException(status_code=403, detail="You must claim membership in this HOA first")
        existing = db.get_delegate_by_user_hoa(conn, user["id"], body.hoa_id)
        if existing:
            raise HTTPException(status_code=409, detail="You are already a delegate for this HOA")
        delegate_id = db.create_delegate(
            conn, user_id=user["id"], hoa_id=body.hoa_id,
            bio=bio, contact_email=contact_email,
        )
        delegate = db.get_delegate(conn, delegate_id)
    return DelegateResponse(
        id=delegate["id"], user_id=delegate["user_id"], hoa_id=delegate["hoa_id"],
        hoa_name=delegate["hoa_name"], display_name=delegate.get("display_name"),
        bio=delegate.get("bio"), contact_email=delegate.get("contact_email"),
        created_at=delegate.get("created_at"),
    )


@app.get("/delegates/{delegate_id}", response_model=DelegateResponse)
def get_delegate(delegate_id: int):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        delegate = db.get_delegate(conn, delegate_id)
    if not delegate:
        raise HTTPException(status_code=404, detail="Delegate not found")
    return DelegateResponse(
        id=delegate["id"], user_id=delegate["user_id"], hoa_id=delegate["hoa_id"],
        hoa_name=delegate["hoa_name"], display_name=delegate.get("display_name"),
        bio=delegate.get("bio"), contact_email=delegate.get("contact_email"),
        created_at=delegate.get("created_at"),
    )


@app.get("/hoas/{hoa_id}/delegates")
def list_hoa_delegates(hoa_id: int):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        delegates = db.list_delegates_for_hoa(conn, hoa_id)
    return [
        DelegateResponse(
            id=d["id"], user_id=d["user_id"], hoa_id=hoa_id,
            hoa_name="", display_name=d.get("display_name"),
            bio=d.get("bio"), contact_email=d.get("contact_email"),
            created_at=d.get("created_at"),
        )
        for d in delegates
    ]


@app.patch("/delegates/{delegate_id}")
def update_delegate(delegate_id: int, body: DelegateUpdateRequest, user: dict = Depends(get_current_user)):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        delegate = db.get_delegate(conn, delegate_id)
        if not delegate:
            raise HTTPException(status_code=404, detail="Delegate not found")
        if delegate["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Not your delegate profile")
        db.update_delegate(conn, delegate_id, bio=body.bio, contact_email=body.contact_email)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Proxy Template endpoints
# ---------------------------------------------------------------------------

@app.get("/proxy-templates/preview")
def preview_proxy_template(
    jurisdiction: str = "CA",
    community_type: str = "hoa",
) -> HTMLResponse:
    from hoaware.proxy_templates import render_proxy_form
    html = render_proxy_form(
        jurisdiction=jurisdiction,
        community_type=community_type,
        grantor_name="Jane Doe",
        grantor_unit="Unit 42",
        delegate_name="John Smith",
        hoa_name="Example Homeowners Association",
        direction="undirected",
    )
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Proxy Assignment endpoints
# ---------------------------------------------------------------------------

def _proxy_to_response(p: dict) -> ProxyResponse:
    return ProxyResponse(
        id=p["id"], grantor_user_id=p["grantor_user_id"], delegate_user_id=p["delegate_user_id"],
        hoa_id=p["hoa_id"], hoa_name=p.get("hoa_name"), grantor_name=p.get("grantor_name"),
        delegate_name=p.get("delegate_name"), jurisdiction=p["jurisdiction"],
        community_type=p["community_type"], direction=p.get("direction", "undirected"),
        voting_instructions=p.get("voting_instructions"), for_meeting_date=p.get("for_meeting_date"),
        expires_at=p.get("expires_at"), status=p["status"],
        signed_at=p.get("signed_at"),
        delivered_at=p.get("delivered_at"), revoked_at=p.get("revoked_at"),
        revoke_reason=p.get("revoke_reason"), created_at=p.get("created_at"),
    )


@app.post("/proxies", response_model=ProxyResponse)
def create_proxy(body: CreateProxyRequest, request: Request, user: dict = Depends(get_current_user)):
    _check_rate_limit(request, limit=20)
    from hoaware.proxy_templates import render_proxy_form
    settings = load_settings()
    if not _is_full_name(user.get("display_name")):
        raise HTTPException(
            status_code=400,
            detail="Proxy grantors must have a full first and last name on their account before creating a proxy.",
        )
    with db.get_connection(settings.db_path) as conn:
        # Verify grantor is a member of the HOA
        claim = db.get_membership_claim(conn, user["id"], body.hoa_id)
        if not claim:
            raise HTTPException(status_code=403, detail="You must be a member of this HOA to create a proxy")
        existing_proxy = db.get_active_proxy_for_grantor_hoa(conn, user["id"], body.hoa_id)
        if existing_proxy:
            raise HTTPException(
                status_code=409,
                detail="You already have an active proxy for this HOA. Revoke it before creating another.",
            )
        # Verify delegate exists for this HOA
        delegate_delegate = None
        delegates = db.list_delegates_for_hoa(conn, body.hoa_id)
        for d in delegates:
            if d["user_id"] == body.delegate_user_id:
                delegate_delegate = d
                break
        if not delegate_delegate:
            raise HTTPException(status_code=404, detail="Delegate not found for this HOA")
        # Cannot assign proxy to yourself
        if body.delegate_user_id == user["id"]:
            raise HTTPException(status_code=400, detail="Cannot assign proxy to yourself")

        # Get HOA location to determine jurisdiction
        hoa_row = conn.execute("SELECT id, name FROM hoas WHERE id = ?", (body.hoa_id,)).fetchone()
        hoa_name = hoa_row["name"] if hoa_row else "Unknown HOA"
        loc = db.get_hoa_location(conn, hoa_name)
        jurisdiction = (loc.get("state") if loc else None) or "XX"

        # Current product scope: undirected general proxies only.
        direction = "undirected"

        # Render form
        grantor = db.get_user_by_id(conn, user["id"])
        delegate_user = db.get_user_by_id(conn, body.delegate_user_id)
        if not _is_full_name(delegate_user.get("display_name")):
            raise HTTPException(
                status_code=400,
                detail="The selected delegate must have a full first and last name before a proxy can be created.",
            )
        form_html = render_proxy_form(
            jurisdiction=jurisdiction,
            community_type="hoa",
            grantor_name=grantor.get("display_name") or grantor["email"],
            grantor_unit=claim.get("unit_number"),
            delegate_name=delegate_user.get("display_name") or delegate_user["email"],
            hoa_name=hoa_name,
            direction=direction,
        )

        proxy_id = db.create_proxy_assignment(
            conn,
            grantor_user_id=user["id"],
            delegate_user_id=body.delegate_user_id,
            hoa_id=body.hoa_id,
            jurisdiction=jurisdiction,
            community_type="hoa",
            direction=direction,
            voting_instructions=None,
            for_meeting_date=None,
            form_html=form_html,
        )
        db.create_proxy_audit(
            conn, proxy_id=proxy_id, action="created", actor_user_id=user["id"],
            details={"direction": direction},
        )
        proxy = db.get_proxy_assignment(conn, proxy_id)
    return _proxy_to_response(proxy)


@app.get("/proxies/mine")
def list_my_proxies(user: dict = Depends(get_current_user)):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proxies = db.list_proxies_for_grantor(conn, user["id"])
    return [_proxy_to_response(p) for p in proxies]


@app.get("/proxies/delegated")
def list_delegated_proxies(user: dict = Depends(get_current_user)):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proxies = db.list_proxies_for_delegate(conn, user["id"])
    return [_proxy_to_response(p) for p in proxies]


@app.get("/proxies/{proxy_id}", response_model=ProxyResponse)
def get_proxy(proxy_id: int, user: dict = Depends(get_current_user)):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)
    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")
    if proxy["grantor_user_id"] != user["id"] and proxy["delegate_user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    return _proxy_to_response(proxy)


@app.get("/proxies/{proxy_id}/form")
def get_proxy_form(proxy_id: int, user: dict = Depends(get_current_user)) -> HTMLResponse:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)
    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")
    if proxy["grantor_user_id"] != user["id"] and proxy["delegate_user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Access denied")
    return HTMLResponse(content=proxy.get("form_html") or "<p>No form available.</p>")


@app.post("/proxies/{proxy_id}/sign", response_model=ProxyResponse)
def sign_proxy(proxy_id: int, request: Request, user: dict = Depends(get_current_user)):
    _check_rate_limit(request, limit=20)
    """Record a click-to-sign e-signature for a proxy."""
    from hoaware.esign import record_signature
    settings = load_settings()
    ip = request.client.host if request.client else None
    ua = request.headers.get("User-Agent")

    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)
    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")
    if proxy["grantor_user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only the grantor can sign this proxy")
    if proxy["status"] != "draft":
        raise HTTPException(status_code=400, detail=f"Proxy is already {proxy['status']}")
    if not user.get("verified_at"):
        raise HTTPException(
            status_code=403,
            detail="Email verification required before signing. Please verify your email address.",
        )

    success = record_signature(
        proxy_id,
        user["id"],
        ip_address=ip,
        user_agent=ua,
        base_url=str(request.base_url).rstrip("/"),
    )
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Cannot sign this proxy. It may not be in draft status or you may not be the grantor.",
        )
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)

    return _proxy_to_response(proxy)


@app.post("/proxies/{proxy_id}/deliver", response_model=ProxyResponse)
def deliver_proxy(proxy_id: int, request: Request, user: dict = Depends(get_current_user)):
    _check_rate_limit(request, limit=20)
    from hoaware.email_service import deliver_proxy_to_board, notify_delegate, notify_grantor
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)
    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")
    if proxy["grantor_user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Only the grantor can deliver a proxy")
    success = deliver_proxy_to_board(proxy_id, actor_user_id=user["id"])
    if not success:
        raise HTTPException(status_code=400, detail="Cannot deliver this proxy. It must be signed first.")
    notify_delegate(proxy_id, "new_proxy")
    notify_grantor(proxy_id, "delivered")
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)
    return _proxy_to_response(proxy)


@app.post("/proxies/{proxy_id}/revoke", response_model=ProxyResponse)
def revoke_proxy(proxy_id: int, body: RevokeProxyRequest, request: Request, user: dict = Depends(get_current_user)):
    _check_rate_limit(request, limit=20)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)
        if not proxy:
            raise HTTPException(status_code=404, detail="Proxy not found")
        if proxy["grantor_user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Only the grantor can revoke a proxy")
        if proxy["status"] in ("revoked", "expired"):
            raise HTTPException(status_code=400, detail=f"Proxy is already {proxy['status']}")
        now = datetime.now(timezone.utc).isoformat()
        db.update_proxy_status(conn, proxy_id, "revoked", revoked_at=now, revoke_reason=body.reason)
        db.create_proxy_audit(
            conn, proxy_id=proxy_id, action="revoked", actor_user_id=user["id"],
            details={"reason": body.reason},
        )
        proxy = db.get_proxy_assignment(conn, proxy_id)
    from hoaware.email_service import notify_delegate
    notify_delegate(proxy_id, "revoked")
    return _proxy_to_response(proxy)


@app.get("/hoas/{hoa_id}/proxy-stats", response_model=ProxyStatsResponse)
def get_proxy_stats(hoa_id: int):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        stats = db.count_proxies_for_hoa(conn, hoa_id)
    return ProxyStatsResponse(**stats)


@app.get("/hoas/{hoa_id}/proxy-status")
def get_hoa_proxy_status(hoa_id: int, user: dict = Depends(get_current_user)):
    """Return the proxy_status and proxy_citation for an HOA as determined from its documents."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa = db.get_hoa_by_id(conn, hoa_id)
    if not hoa:
        raise HTTPException(status_code=404, detail="HOA not found")
    return {
        "hoa_id": hoa_id,
        "proxy_status": hoa.get("proxy_status") or "unknown",
        "proxy_citation": hoa.get("proxy_citation"),
    }


@app.get("/verify-proxy", include_in_schema=False)
def verify_proxy_page() -> FileResponse:
    return _serve_static_page("verify-proxy.html")


@app.get("/proxies/verify/{code}")
def verify_proxy_by_code(code: str):
    """Public endpoint — no auth required. Returns verification info for a signed proxy."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_by_verification_code(conn, code)
    if not proxy:
        raise HTTPException(status_code=404, detail="Verification code not found")
    # Return only non-PII fields; show first name + last initial for grantor privacy
    grantor_name: str = proxy.get("grantor_name") or ""
    parts = grantor_name.split()
    display_grantor = (
        f"{parts[0]} {parts[-1][0]}." if len(parts) >= 2 else grantor_name
    )
    return {
        "hoa_name": proxy.get("hoa_name"),
        "grantor_display": display_grantor,
        "delegate_name": proxy.get("delegate_name"),
        "direction": proxy.get("direction") or "undirected",
        "for_meeting_date": proxy.get("for_meeting_date"),
        "signed_at": proxy.get("signed_at"),
        "status": proxy.get("status"),
        "form_hash": proxy.get("form_hash"),
        "verification_code": code,
    }


# ---------------------------------------------------------------------------
# HOA board email (M6)
# ---------------------------------------------------------------------------

class SetBoardEmailRequest(BaseModel):
    board_email: str | None = None


@app.patch("/hoas/{hoa_id}/board-email")
def set_board_email(hoa_id: int, body: SetBoardEmailRequest, user: dict = Depends(get_current_user)):
    """Set the board contact email for an HOA. Requires membership in the HOA."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        # Verify membership
        claims = db.list_membership_claims_for_user(conn, user["id"])
        if not any(c["hoa_id"] == hoa_id for c in claims):
            raise HTTPException(status_code=403, detail="You are not a member of this HOA")
        db.set_hoa_board_email(conn, hoa_id, body.board_email)
    return {"ok": True, "board_email": body.board_email}


@app.get("/hoas")
def list_hoas() -> list[str]:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        return db.list_hoa_names_with_documents(conn)


@app.get("/hoas/summary", response_model=HoaSummaryPage)
def list_hoa_summary(
    q: str | None = None,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HoaSummaryPage:
    limit = min(limit, 500)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        page = db.list_hoa_summaries(conn, q=q, state=state, limit=limit, offset=offset)
    return HoaSummaryPage(
        results=[HoaSummary(**row) for row in page["results"]],
        total=page["total"],
    )


class HoaStateCount(BaseModel):
    state: str
    count: int


@app.get("/hoas/states", response_model=List[HoaStateCount])
def list_hoa_states() -> List[HoaStateCount]:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        return [HoaStateCount(**row) for row in db.list_hoa_states(conn)]


@app.get("/hoas/map-points", response_model=List[HoaMapPoint])
def list_hoa_map_points(
    q: str | None = None,
    state: str | None = None,
) -> List[HoaMapPoint]:
    """Lightweight endpoint returning only lat/lng/state/doc_count for map markers."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        rows = db.list_hoa_map_points(conn, q=q, state=state)
    return [HoaMapPoint(**row) for row in rows]


class HoaResolveResponse(BaseModel):
    hoa_id: int
    hoa_name: str
    city: str | None = None
    state: str | None = None


@app.get("/hoas/resolve/{slug}", response_model=HoaResolveResponse)
def resolve_hoa_slug(slug: str) -> HoaResolveResponse:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        result = db.resolve_hoa_by_slug(conn, slug)
    if result is None:
        raise HTTPException(status_code=404, detail="HOA not found")
    return HoaResolveResponse(**result)


@app.post("/lookup/universal", response_model=UniversalLookupResponse)
def universal_lookup(body: UniversalLookupRequest) -> UniversalLookupResponse:
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_names = db.list_hoa_names_with_documents(conn)
        location_rows = db.list_hoa_locations(conn)

    hoa_matches = _find_hoa_matches(query, hoa_names)
    geocoded = _geocode_from_query(query)
    address_lookup = AddressLookup(resolved=False)
    suggestions: list[dict] = []
    if geocoded:
        address_lookup = AddressLookup(
            resolved=True,
            display_name=str(geocoded["display_name"]),
            latitude=float(geocoded["latitude"]),
            longitude=float(geocoded["longitude"]),
        )
        suggestions = _suggestions_for_point(
            point_lat=float(geocoded["latitude"]),
            point_lon=float(geocoded["longitude"]),
            rows=location_rows,
            max_suggestions=body.max_suggestions,
        )

    # Promote address suggestions to hoa_matches when no name matches were
    # found — so the user's nearby HOAs show in the primary results.
    if not hoa_matches:
        for s in suggestions:
            hoa_matches.append({"hoa": s["hoa"], "match_reason": s.get("match_type", "nearby")})

    return UniversalLookupResponse(
        query=query,
        hoa_matches=[HoaMatch(**item) for item in hoa_matches],
        address_lookup=address_lookup,
        address_suggestions=[AddressSuggestion(**item) for item in suggestions],
    )


@app.get("/hoas/locations", response_model=List[HoaLocation])
def list_hoa_locations() -> List[HoaLocation]:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        rows = db.list_hoa_locations(conn)
    return [HoaLocation(**row) for row in rows]


@app.post("/hoas/locations/infer", response_model=List[HoaLocation])
def infer_hoa_locations() -> List[HoaLocation]:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoas = db.list_hoa_names_with_documents(conn)
    for hoa_name in hoas:
        settings = load_settings()
        with db.get_connection(settings.db_path) as conn:
            location = db.get_hoa_location(conn, hoa_name)
        if location and location.get("latitude") is not None and location.get("longitude") is not None:
            continue
        _infer_and_store_location(hoa_name)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        rows = db.list_hoa_locations(conn)
    return [HoaLocation(**row) for row in rows]


@app.get("/hoas/{hoa_name}/location", response_model=HoaLocation | None)
def get_hoa_location(hoa_name: str) -> HoaLocation | None:
    settings = load_settings()
    resolved_hoa = _resolve_hoa_name(hoa_name)
    with db.get_connection(settings.db_path) as conn:
        row = db.get_hoa_location(conn, resolved_hoa)
    if row is None:
        return None
    return HoaLocation(**row)


@app.post("/hoas/{hoa_name}/location", response_model=HoaLocation)
def upsert_hoa_location(
    hoa_name: str,
    metadata_type: str | None = Form(default=None),
    website_url: str | None = Form(default=None),
    street: str | None = Form(default=None),
    city: str | None = Form(default=None),
    state: str | None = Form(default=None),
    postal_code: str | None = Form(default=None),
    country: str | None = Form(default=None),
    latitude: float | None = Form(default=None),
    longitude: float | None = Form(default=None),
    boundary_geojson: str | None = Form(default=None),
) -> HoaLocation:
    settings = load_settings()
    resolved_hoa = _resolve_hoa_name(hoa_name)
    normalized_metadata_type = _normalize_metadata_type(metadata_type)
    normalized_website = _normalize_website_url(website_url)
    normalized_boundary = _parse_boundary_geojson(boundary_geojson)
    if latitude is not None and not (-90 <= latitude <= 90):
        raise HTTPException(status_code=400, detail="latitude must be between -90 and 90")
    if longitude is not None and not (-180 <= longitude <= 180):
        raise HTTPException(status_code=400, detail="longitude must be between -180 and 180")
    # Polygon centroid wins over address-based geocoding — a polygon describes
    # the actual neighborhood, while "city, state" geocoding lands on the city
    # center.
    if (latitude is None or longitude is None) and normalized_boundary:
        center = _center_from_boundary_geojson(normalized_boundary)
        if center:
            latitude, longitude = center
    if (latitude is None or longitude is None) and any([street, city, state, postal_code]):
        coords = _geocode_from_parts(
            street=(street.strip() if street else None),
            city=(city.strip() if city else None),
            state=(state.strip().upper() if state else None),
            postal_code=(postal_code.strip() if postal_code else None),
        )
        if coords:
            latitude, longitude = coords
    with db.get_connection(settings.db_path) as conn:
        db.upsert_hoa_location(
            conn,
            resolved_hoa,
            metadata_type=normalized_metadata_type,
            website_url=normalized_website,
            street=(street.strip() if street else None),
            city=(city.strip() if city else None),
            state=(state.strip().upper() if state else None),
            postal_code=(postal_code.strip() if postal_code else None),
            country=(country.strip().upper() if country else None),
            latitude=latitude,
            longitude=longitude,
            boundary_geojson=normalized_boundary,
            source="manual",
        )
        row = db.get_hoa_location(conn, resolved_hoa)
    if row is None:
        raise HTTPException(status_code=404, detail="HOA not found")
    return HoaLocation(**row)


@app.get("/hoas/{hoa_name}/documents", response_model=List[DocumentSummary])
def list_documents(hoa_name: str) -> List[DocumentSummary]:
    settings = load_settings()
    resolved_hoa = _resolve_hoa_name(hoa_name)
    with db.get_connection(settings.db_path) as conn:
        rows = db.list_documents_for_hoa(conn, resolved_hoa)
    return [DocumentSummary(**row) for row in rows]


@app.get("/hoas/{hoa_name}/documents/file")
def open_document_file(hoa_name: str, path: str) -> FileResponse:
    settings = load_settings()
    resolved_hoa = _resolve_hoa_name(hoa_name)
    rel_doc = _safe_relative_document_path(path)
    doc_path = (settings.docs_root / rel_doc).resolve()
    try:
        doc_path.relative_to(settings.docs_root.resolve())
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid document path") from exc
    if not doc_path.exists() or not doc_path.is_file():
        raise HTTPException(status_code=404, detail="Document not found")
    if doc_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are supported")
    if not rel_doc.startswith(f"{resolved_hoa}/"):
        raise HTTPException(status_code=400, detail="Document does not belong to requested HOA")
    return FileResponse(doc_path, media_type="application/pdf", filename=doc_path.name)


@app.get("/hoas/{hoa_name}/documents/searchable", response_class=HTMLResponse)
def open_document_searchable(hoa_name: str, path: str) -> HTMLResponse:
    settings = load_settings()
    resolved_hoa = _resolve_hoa_name(hoa_name)
    rel_doc = _safe_relative_document_path(path)
    if not rel_doc.startswith(f"{resolved_hoa}/"):
        raise HTTPException(status_code=400, detail="Document does not belong to requested HOA")
    with db.get_connection(settings.db_path) as conn:
        chunks = db.list_document_chunks_for_hoa(conn, resolved_hoa, rel_doc)
    if not chunks:
        raise HTTPException(
            status_code=404,
            detail="No searchable OCR text is indexed for this document yet.",
        )
    html = _render_searchable_document_html(resolved_hoa, rel_doc, chunks)
    return HTMLResponse(content=html)


@app.post("/upload", response_model=UploadResponse)
async def upload_documents(
    background_tasks: BackgroundTasks,
    hoa: str = Form(...),
    files: List[UploadFile] = File(...),
    metadata_type: str | None = Form(default=None),
    website_url: str | None = Form(default=None),
    street: str | None = Form(default=None),
    city: str | None = Form(default=None),
    state: str | None = Form(default=None),
    postal_code: str | None = Form(default=None),
    country: str | None = Form(default=None),
    latitude: float | None = Form(default=None),
    longitude: float | None = Form(default=None),
    boundary_geojson: str | None = Form(default=None),
    categories: List[str] | None = Form(default=None),
    text_extractable: List[str] | None = Form(default=None),
    source_urls: List[str] | None = Form(default=None),
    user: dict = Depends(get_current_user),
) -> UploadResponse:
    settings = load_settings()
    resolved_hoa = _resolve_hoa_name(hoa)
    if not settings.openai_api_key:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is required for ingestion")
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required")
    MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB per file
    for upload in files:
        upload.file.seek(0, 2)
        size = upload.file.tell()
        upload.file.seek(0)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"File '{upload.filename}' exceeds 25 MB limit ({size // 1024 // 1024} MB)")

    per_file_meta = _parse_per_file_metadata(
        len(files),
        categories=categories,
        text_extractable=text_extractable,
        source_urls=source_urls,
    )

    settings.docs_root.mkdir(parents=True, exist_ok=True)
    hoa_dir = settings.docs_root / resolved_hoa
    hoa_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[Path] = []
    saved_files: list[str] = []
    metadata_by_path: dict[Path, dict] = {}
    for upload, meta in zip(files, per_file_meta):
        filename = _safe_pdf_filename(upload.filename)
        target = hoa_dir / filename
        with target.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        saved_paths.append(target)
        saved_files.append(filename)
        metadata_by_path[target] = meta
        await upload.close()

    normalized_website = _normalize_website_url(website_url)
    normalized_metadata_type = _normalize_metadata_type(metadata_type)
    normalized_boundary = _parse_boundary_geojson(boundary_geojson)
    location_saved = False
    if any(value is not None and str(value).strip() for value in [normalized_metadata_type, normalized_website, street, city, state, postal_code, country, normalized_boundary]) or (
        latitude is not None and longitude is not None
    ):
        if latitude is not None and not (-90 <= latitude <= 90):
            raise HTTPException(status_code=400, detail="latitude must be between -90 and 90")
        if longitude is not None and not (-180 <= longitude <= 180):
            raise HTTPException(status_code=400, detail="longitude must be between -180 and 180")
        # Polygon centroid wins over address-based geocoding (the polygon
        # describes the actual neighborhood; "city, state" geocoding lands on
        # the city center).
        if (latitude is None or longitude is None) and normalized_boundary:
            center = _center_from_boundary_geojson(normalized_boundary)
            if center:
                latitude, longitude = center
        if (latitude is None or longitude is None) and any([street, city, state, postal_code]):
            coords = _geocode_from_parts(
                street=(street.strip() if street else None),
                city=(city.strip() if city else None),
                state=(state.strip().upper() if state else None),
                postal_code=(postal_code.strip() if postal_code else None),
            )
            if coords:
                latitude, longitude = coords
        with db.get_connection(settings.db_path) as conn:
            db.upsert_hoa_location(
                conn,
                resolved_hoa,
                metadata_type=normalized_metadata_type,
                website_url=normalized_website,
                street=(street.strip() if street else None),
                city=(city.strip() if city else None),
                state=(state.strip().upper() if state else None),
                postal_code=(postal_code.strip() if postal_code else None),
                country=(country.strip().upper() if country else None),
                latitude=latitude,
                longitude=longitude,
                boundary_geojson=normalized_boundary,
                source="manual",
            )
        location_saved = True

    # Daily DocAI budget guard: count pages from files the agent flagged as
    # text_extractable=False — those will hit DocAI in full.
    projected_ocr_pages = _projected_docai_pages(saved_paths, metadata_by_path)
    _check_daily_docai_budget(projected_ocr_pages)

    background_tasks.add_task(
        _ingest_uploaded_files, resolved_hoa, saved_paths, metadata_by_path
    )
    return UploadResponse(
        hoa=resolved_hoa,
        saved_files=saved_files,
        indexed=0,
        skipped=0,
        failed=0,
        queued=True,
        location_saved=location_saved,
    )


@app.post("/upload/anonymous", response_model=UploadResponse)
async def upload_documents_anonymous(
    request: Request,
    background_tasks: BackgroundTasks,
    hoa: str = Form(...),
    email: str = Form(...),
    files: List[UploadFile] = File(...),
    website_url: str | None = Form(default=None),
    street: str | None = Form(default=None),
    city: str | None = Form(default=None),
    state: str | None = Form(default=None),
    postal_code: str | None = Form(default=None),
    latitude: float | None = Form(default=None),
    longitude: float | None = Form(default=None),
    boundary_geojson: str | None = Form(default=None),
    categories: List[str] | None = Form(default=None),
    text_extractable: List[str] | None = Form(default=None),
    source_urls: List[str] | None = Form(default=None),
) -> UploadResponse:
    """Accept HOA uploads without authentication. Rate-limited to 3/hour per IP."""
    _check_rate_limit(request, limit=3)
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email address is required")
    settings = load_settings()
    resolved_hoa = _resolve_hoa_name(hoa)
    if not settings.openai_api_key:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is required for ingestion")
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required")
    MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB per file
    for upload in files:
        upload.file.seek(0, 2)
        size = upload.file.tell()
        upload.file.seek(0)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"File '{upload.filename}' exceeds 25 MB limit ({size // 1024 // 1024} MB)")

    per_file_meta = _parse_per_file_metadata(
        len(files),
        categories=categories,
        text_extractable=text_extractable,
        source_urls=source_urls,
    )

    settings.docs_root.mkdir(parents=True, exist_ok=True)
    hoa_dir = settings.docs_root / resolved_hoa
    hoa_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[Path] = []
    saved_files: list[str] = []
    metadata_by_path: dict[Path, dict] = {}
    for upload, meta in zip(files, per_file_meta):
        filename = _safe_pdf_filename(upload.filename)
        target = hoa_dir / filename
        with target.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        saved_paths.append(target)
        saved_files.append(filename)
        metadata_by_path[target] = meta
        await upload.close()

    normalized_website = _normalize_website_url(website_url)
    normalized_boundary = _parse_boundary_geojson(boundary_geojson)
    location_saved = False
    if any(value is not None and str(value).strip() for value in [normalized_website, street, city, state, postal_code, normalized_boundary]) or (
        latitude is not None and longitude is not None
    ):
        if latitude is not None and not (-90 <= latitude <= 90):
            raise HTTPException(status_code=400, detail="latitude must be between -90 and 90")
        if longitude is not None and not (-180 <= longitude <= 180):
            raise HTTPException(status_code=400, detail="longitude must be between -180 and 180")
        # Polygon centroid wins over address-based geocoding (the polygon
        # describes the actual neighborhood; "city, state" geocoding lands on
        # the city center).
        if (latitude is None or longitude is None) and normalized_boundary:
            center = _center_from_boundary_geojson(normalized_boundary)
            if center:
                latitude, longitude = center
        if (latitude is None or longitude is None) and any([street, city, state, postal_code]):
            coords = _geocode_from_parts(
                street=(street.strip() if street else None),
                city=(city.strip() if city else None),
                state=(state.strip().upper() if state else None),
                postal_code=(postal_code.strip() if postal_code else None),
            )
            if coords:
                latitude, longitude = coords
        with db.get_connection(settings.db_path) as conn:
            db.upsert_hoa_location(
                conn,
                resolved_hoa,
                website_url=normalized_website,
                street=(street.strip() if street else None),
                city=(city.strip() if city else None),
                state=(state.strip().upper() if state else None),
                postal_code=(postal_code.strip() if postal_code else None),
                latitude=latitude,
                longitude=longitude,
                boundary_geojson=normalized_boundary,
                source="anonymous_upload",
            )
        location_saved = True

    logger.info("anonymous_upload hoa=%s email=%s files=%d ip=%s",
                resolved_hoa, email, len(saved_files),
                request.client.host if request.client else "unknown")

    projected_ocr_pages = _projected_docai_pages(saved_paths, metadata_by_path)
    _check_daily_docai_budget(projected_ocr_pages)

    background_tasks.add_task(
        _ingest_uploaded_files, resolved_hoa, saved_paths, metadata_by_path
    )
    return UploadResponse(
        hoa=resolved_hoa,
        saved_files=saved_files,
        indexed=0,
        skipped=0,
        failed=0,
        queued=True,
        location_saved=location_saved,
    )


@app.post("/agent/precheck", response_model=AgentPrecheckResponse)
def agent_precheck(body: AgentPrecheckRequest) -> AgentPrecheckResponse:
    """Inspect a candidate PDF before uploading.

    Agent provides one of: a public URL to download, or a sha256 + filename to
    check duplicates against the existing corpus. Returns category suggestion,
    page count, text-extractability, duplicate detection, and est DocAI cost.
    """
    notes: list[str] = []
    pdf_bytes: bytes | None = None
    page_count: int | None = None
    file_size_bytes: int | None = None
    sha256: str | None = body.sha256
    text_extractable: bool | None = None
    suggested_category: str | None = None

    if body.url:
        # Bounded download — refuse anything larger than 25 MB
        try:
            with requests.get(body.url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                limit = 25 * 1024 * 1024
                buf = io.BytesIO()
                total = 0
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        raise HTTPException(status_code=413, detail="PDF exceeds 25 MB limit")
                    buf.write(chunk)
                pdf_bytes = buf.getvalue()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {exc}")

        file_size_bytes = len(pdf_bytes)
        sha256 = hashlib.sha256(pdf_bytes).hexdigest()

        # Inspect with PyPDF
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            page_count = len(reader.pages)
            first_text = (reader.pages[0].extract_text() or "") if page_count else ""
            text_extractable = len(first_text.strip()) >= 50
            if text_extractable:
                # Try classifier on first 5 pages of text
                full_text_parts: list[str] = []
                for i in range(min(5, page_count)):
                    try:
                        full_text_parts.append(reader.pages[i].extract_text() or "")
                    except Exception:
                        pass
                full_text = "\n".join(full_text_parts)
                clf = classify_from_text(full_text, body.hoa or "")
                if clf:
                    suggested_category = clf["category"]
                    notes.append(f"category via {clf['method']} (conf={clf['confidence']:.2f})")
        except Exception as exc:
            notes.append(f"PyPDF inspection failed: {exc}")

    # Filename fallback for category
    if not suggested_category and body.filename:
        clf = classify_from_filename(body.filename)
        if clf:
            suggested_category = clf["category"]
            notes.append(f"category via filename (conf={clf['confidence']:.2f})")

    # Duplicate check
    duplicate_of: str | None = None
    if sha256:
        settings = load_settings()
        try:
            with db.get_connection(settings.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT h.name, d.relative_path
                    FROM documents d JOIN hoas h ON h.id = d.hoa_id
                    WHERE d.checksum = ?
                    LIMIT 1
                    """,
                    (sha256,),
                ).fetchone()
                if row:
                    duplicate_of = f"{row['name']}/{row['relative_path']}"
                    notes.append(f"duplicate of existing doc")
        except Exception as exc:
            notes.append(f"dup check failed: {exc}")

    is_valid = (suggested_category in VALID_CATEGORIES) if suggested_category else False
    is_pii = (suggested_category in REJECT_PII) if suggested_category else False
    est_pages = (page_count or 0) if (text_extractable is False) else 0
    est_cost = round(est_pages * COST_DOCAI_PER_PAGE, 6)

    return AgentPrecheckResponse(
        page_count=page_count,
        file_size_bytes=file_size_bytes,
        sha256=sha256,
        text_extractable=text_extractable,
        suggested_category=suggested_category,
        is_valid_governing_doc=is_valid,
        is_pii_risk=is_pii,
        duplicate_of=duplicate_of,
        est_docai_pages=est_pages,
        est_docai_cost_usd=est_cost,
        notes=notes,
    )


@app.post("/search", response_model=SearchResponse)
def search(body: SearchRequest) -> SearchResponse:
    settings = load_settings()
    hoa_name = _resolve_hoa_name(body.hoa)
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    if not settings.openai_api_key:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is required for search")

    try:
        matches = retrieve_context(query, hoa_name, body.k, settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    results: list[SearchResult] = []
    for item in matches:
        payload = item["payload"]
        raw_text = payload.get("text", "")
        excerpt = raw_text[:280].replace("\n", " ")
        pages = f"{payload.get('start_page')}–{payload.get('end_page')}"
        results.append(
            SearchResult(
                score=float(item["score"]),
                document=str(payload.get("document", "unknown")),
                pages=pages,
                excerpt=excerpt + ("..." if len(raw_text) > 280 else ""),
            )
        )
    return SearchResponse(results=results)


@app.post("/search/multi", response_model=MultiSearchResponse)
def search_multi(body: MultiSearchRequest) -> MultiSearchResponse:
    settings = load_settings()
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    if not body.hoas:
        raise HTTPException(status_code=400, detail="hoas is required")
    resolved_hoas = [_resolve_hoa_name(hoa_name) for hoa_name in body.hoas if str(hoa_name).strip()]
    if not resolved_hoas:
        raise HTTPException(status_code=400, detail="hoas is required")
    if not settings.openai_api_key:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is required for search")

    try:
        matches = retrieve_context_multi(query, resolved_hoas, body.k, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    results: list[MultiSearchResult] = []
    for item in matches:
        payload = item["payload"]
        raw_text = str(payload.get("text", ""))
        excerpt = raw_text[:280].replace("\n", " ")
        pages = f"{payload.get('start_page')}–{payload.get('end_page')}"
        results.append(
            MultiSearchResult(
                score=float(item["score"]),
                hoa=str(payload.get("hoa", "unknown")),
                document=str(payload.get("document", "unknown")),
                pages=pages,
                excerpt=excerpt + ("..." if len(raw_text) > 280 else ""),
            )
        )
    return MultiSearchResponse(results=results)


@app.post("/qa", response_model=QAResponse)
def qa(body: QARequest) -> QAResponse:
    settings = load_settings()
    hoa_name = _resolve_hoa_name(body.hoa)
    if not settings.openai_api_key:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is required for QA")
    try:
        answer, citations, results = get_answer(
            body.question,
            hoa_name,
            k=body.k,
            model=body.model,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not results:
        raise HTTPException(status_code=404, detail="No context found for query.")
    return QAResponse(answer=answer, sources=citations)


@app.post("/qa/multi", response_model=QAResponse)
def qa_multi(body: MultiQARequest) -> QAResponse:
    settings = load_settings()
    if not body.hoas:
        raise HTTPException(status_code=400, detail="hoas is required")
    resolved_hoas = [_resolve_hoa_name(hoa_name) for hoa_name in body.hoas if str(hoa_name).strip()]
    if not resolved_hoas:
        raise HTTPException(status_code=400, detail="hoas is required")
    if not settings.openai_api_key:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is required for QA")
    try:
        answer, citations, results = get_answer_multi(
            body.question,
            resolved_hoas,
            k=body.k,
            model=body.model,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not results:
        raise HTTPException(status_code=404, detail="No context found for query.")
    return QAResponse(answer=answer, sources=citations)


@app.get("/law/jurisdictions", response_model=List[LawJurisdictionSummary])
def list_law_jurisdictions() -> List[LawJurisdictionSummary]:
    _ensure_law_module_available()
    try:
        rows = list_jurisdictions()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return [LawJurisdictionSummary(**row) for row in rows]


@app.get("/law/{jurisdiction}/profiles", response_model=List[LawProfile])
def list_law_profiles(
    jurisdiction: str,
    community_type: str | None = None,
    entity_form: str | None = None,
) -> List[LawProfile]:
    _ensure_law_module_available()
    try:
        rows = list_profiles(
            jurisdiction=jurisdiction,
            community_type=community_type,
            entity_form=entity_form,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return [LawProfile(**row) for row in rows]


@app.post("/law/qa", response_model=LawQAResponse)
def law_qa(body: LawQARequest) -> LawQAResponse:
    _ensure_law_module_available()
    try:
        answer = answer_law_question(
            jurisdiction=body.jurisdiction,
            community_type=body.community_type,
            entity_form=body.entity_form,
            question_family=body.question_family,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return LawQAResponse(
        answer=answer.answer,
        checklist=answer.checklist,
        citations=answer.citations,
        known_unknowns=answer.known_unknowns,
        confidence=answer.confidence,
        last_verified_date=answer.last_verified_date,
        disclaimer=answer.disclaimer,
    )


@app.get("/law/{jurisdiction}/proxy-electronic", response_model=ElectronicProxyQuestionResponse)
def law_proxy_electronic(
    jurisdiction: str,
    community_type: str = "hoa",
    entity_form: str = "unknown",
) -> ElectronicProxyQuestionResponse:
    _ensure_law_module_available()
    try:
        answer = answer_electronic_proxy_questions(
            jurisdiction=jurisdiction,
            community_type=community_type,
            entity_form=entity_form,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return ElectronicProxyQuestionResponse(
        jurisdiction=answer.jurisdiction,
        community_type=answer.community_type,
        entity_form=answer.entity_form,
        electronic_assignment={
            "status": answer.electronic_assignment.status,
            "evidence_rules": answer.electronic_assignment.evidence_rules,
            "citations": answer.electronic_assignment.citations,
        },
        electronic_signature={
            "status": answer.electronic_signature.status,
            "evidence_rules": answer.electronic_signature.evidence_rules,
            "citations": answer.electronic_signature.citations,
        },
        known_unknowns=answer.known_unknowns,
        confidence=answer.confidence,
        last_verified_date=answer.last_verified_date,
        disclaimer=answer.disclaimer,
    )


@app.get("/law/proxy-electronic/summary", response_model=List[ElectronicProxySummaryItem])
def law_proxy_electronic_summary(
    community_type: str = "hoa",
    entity_form: str = "unknown",
) -> List[ElectronicProxySummaryItem]:
    _ensure_law_module_available()
    try:
        rows = electronic_proxy_summary(community_type=community_type, entity_form=entity_form)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return [ElectronicProxySummaryItem(**row) for row in rows]


# ---------------------------------------------------------------------------
# Proposals — page routes
# ---------------------------------------------------------------------------

@app.get("/proposals", include_in_schema=False)
def proposals_page() -> FileResponse:
    return _serve_static_page("proposals.html")


# ---------------------------------------------------------------------------
# Proposals — API
# ---------------------------------------------------------------------------

def _proposal_to_response(
    p: dict,
    *,
    hoa_name: str | None = None,
    share_code: str | None = None,
    cosigners: list[str] | None = None,
    user_cosigned: bool = False,
    user_upvoted: bool = False,
) -> ProposalResponse:
    return ProposalResponse(
        id=p["id"],
        hoa_id=p["hoa_id"],
        hoa_name=hoa_name,
        creator_user_id=p["creator_user_id"],
        title=p["title"],
        description=p["description"],
        category=p["category"],
        status=p["status"],
        cosigner_count=p["cosigner_count"],
        upvote_count=p["upvote_count"],
        share_code=share_code,
        cosigners=cosigners or [],
        user_cosigned=user_cosigned,
        user_upvoted=user_upvoted,
        created_at=p.get("created_at"),
        published_at=p.get("published_at"),
        lat=p.get("lat"),
        lng=p.get("lng"),
        location_description=p.get("location_description"),
    )


@app.post("/proposals", response_model=ProposalResponse)
def create_proposal(body: CreateProposalRequest, request: Request, user: dict = Depends(get_current_user)):
    _check_rate_limit(request, limit=10)
    if body.category not in PROPOSAL_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category must be one of {sorted(PROPOSAL_CATEGORIES)}")
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        claim = db.get_membership_claim(conn, user["id"], body.hoa_id)
        if not claim:
            raise HTTPException(status_code=403, detail="You must be a member of this HOA to create a proposal")
        existing = db.get_active_proposal_for_user(conn, user["id"])
        if existing:
            raise HTTPException(status_code=409, detail="You already have an active proposal; withdraw it before creating another")
        # Validate location: both lat+lng must be provided together
        lat, lng = body.lat, body.lng
        if (lat is None) != (lng is None):
            raise HTTPException(status_code=422, detail="lat and lng must both be provided or both omitted")
        if lat is not None and not (-90 <= lat <= 90):
            raise HTTPException(status_code=422, detail="lat must be between -90 and 90")
        if lng is not None and not (-180 <= lng <= 180):
            raise HTTPException(status_code=422, detail="lng must be between -180 and 180")
        proposal_id = db.create_proposal(
            conn,
            hoa_id=body.hoa_id,
            creator_user_id=user["id"],
            title=body.title.strip(),
            description=body.description.strip(),
            category=body.category,
            lat=lat,
            lng=lng,
            location_description=body.location_description.strip() if body.location_description else None,
        )
        p = db.get_proposal(conn, proposal_id)
        hoa_row = conn.execute("SELECT name FROM hoas WHERE id = ?", (body.hoa_id,)).fetchone()
        hoa_name = str(hoa_row["name"]) if hoa_row else None
    return _proposal_to_response(p, hoa_name=hoa_name, share_code=p["share_code"])


@app.get("/proposals/mine")
def list_my_proposals(user: dict = Depends(get_current_user)):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proposals = db.list_proposals_for_user(conn, user["id"])
        result = []
        for p in proposals:
            hoa_row = conn.execute("SELECT name FROM hoas WHERE id = ?", (p["hoa_id"],)).fetchone()
            hoa_name = str(hoa_row["name"]) if hoa_row else None
            share_code = p["share_code"] if p["status"] == "private" else None
            result.append(_proposal_to_response(p, hoa_name=hoa_name, share_code=share_code))
    return result


@app.post("/proposals/cosign/{share_code}", response_model=ProposalResponse)
def cosign_proposal(share_code: str, request: Request, user: dict = Depends(get_current_user)):
    _check_rate_limit(request, limit=30)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        p = db.get_proposal_by_share_code(conn, share_code)
        if not p:
            raise HTTPException(status_code=404, detail="Proposal not found")
        # Must be in same HOA
        claim = db.get_membership_claim(conn, user["id"], p["hoa_id"])
        if not claim:
            raise HTTPException(status_code=404, detail="Proposal not found")
        # Cannot co-sign own proposal
        if p["creator_user_id"] == user["id"]:
            raise HTTPException(status_code=403, detail="Cannot co-sign your own proposal")
        # Must be private or public (not archived) to accept co-signers
        if p["status"] not in ("private", "public"):
            raise HTTPException(status_code=400, detail="Proposal is no longer accepting co-signers")
        import sqlite3 as _sqlite3
        try:
            db.create_cosigner(conn, proposal_id=p["id"], user_id=user["id"])
        except _sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="You have already co-signed this proposal")
        p = db.get_proposal(conn, p["id"])
        hoa_row = conn.execute("SELECT name FROM hoas WHERE id = ?", (p["hoa_id"],)).fetchone()
        hoa_name = str(hoa_row["name"]) if hoa_row else None
        user_cosigned = db.get_cosigner(conn, p["id"], user["id"]) is not None
        cosigners = db.list_cosigner_names(conn, p["id"]) if p["status"] == "public" else []
    return _proposal_to_response(p, hoa_name=hoa_name, cosigners=cosigners, user_cosigned=user_cosigned)


@app.post("/proposals/{proposal_id}/cosign", response_model=ProposalResponse)
def cosign_public_proposal(proposal_id: int, request: Request, user: dict = Depends(get_current_user)):
    """Co-sign a public proposal by ID (puts your name on it publicly)."""
    _check_rate_limit(request, limit=30)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        p = db.get_proposal(conn, proposal_id)
        if not p:
            raise HTTPException(status_code=404, detail="Proposal not found")
        claim = db.get_membership_claim(conn, user["id"], p["hoa_id"])
        if not claim:
            raise HTTPException(status_code=403, detail="You must be a member of this HOA")
        if p["creator_user_id"] == user["id"]:
            raise HTTPException(status_code=403, detail="Cannot co-sign your own proposal")
        if p["status"] != "public":
            raise HTTPException(status_code=400, detail="Can only co-sign public proposals by ID")
        import sqlite3 as _sqlite3
        try:
            db.create_cosigner(conn, proposal_id=proposal_id, user_id=user["id"])
        except _sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="You have already co-signed this proposal")
        p = db.get_proposal(conn, proposal_id)
        hoa_row = conn.execute("SELECT name FROM hoas WHERE id = ?", (p["hoa_id"],)).fetchone()
        hoa_name = str(hoa_row["name"]) if hoa_row else None
        cosigners = db.list_cosigner_names(conn, proposal_id)
    return _proposal_to_response(p, hoa_name=hoa_name, cosigners=cosigners, user_cosigned=True)


@app.delete("/proposals/{proposal_id}/cosign")
def withdraw_cosign(proposal_id: int, user: dict = Depends(get_current_user)):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        deleted = db.delete_cosigner(conn, proposal_id=proposal_id, user_id=user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="No co-signature found to withdraw")
    return {"ok": True}


@app.post("/proposals/{proposal_id}/upvote", response_model=ProposalResponse)
def upvote_proposal(proposal_id: int, request: Request, user: dict = Depends(get_current_user)):
    _check_rate_limit(request, limit=30)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        p = db.get_proposal(conn, proposal_id)
        if not p:
            raise HTTPException(status_code=404, detail="Proposal not found")
        claim = db.get_membership_claim(conn, user["id"], p["hoa_id"])
        if not claim:
            raise HTTPException(status_code=403, detail="You must be a member of this HOA")
        if p["status"] != "public":
            raise HTTPException(status_code=400, detail="Can only upvote public proposals")
        import sqlite3 as _sqlite3
        try:
            db.create_upvote(conn, proposal_id=proposal_id, user_id=user["id"])
        except _sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="You have already upvoted this proposal")
        p = db.get_proposal(conn, proposal_id)
        hoa_row = conn.execute("SELECT name FROM hoas WHERE id = ?", (p["hoa_id"],)).fetchone()
        hoa_name = str(hoa_row["name"]) if hoa_row else None
    return _proposal_to_response(p, hoa_name=hoa_name, user_upvoted=True)


@app.delete("/proposals/{proposal_id}/upvote")
def withdraw_upvote(proposal_id: int, user: dict = Depends(get_current_user)):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        deleted = db.delete_upvote(conn, proposal_id=proposal_id, user_id=user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="No upvote found to withdraw")
    return {"ok": True}


@app.delete("/proposals/{proposal_id}")
def withdraw_proposal(proposal_id: int, request: Request, user: dict = Depends(get_current_user)):
    _check_rate_limit(request, limit=20)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        p = db.get_proposal(conn, proposal_id)
        if not p:
            raise HTTPException(status_code=404, detail="Proposal not found")
        if p["creator_user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Only the creator can withdraw this proposal")
        if p["status"] == "archived":
            raise HTTPException(status_code=400, detail="Proposal is already archived")
        db.archive_proposal(conn, proposal_id)
    return {"ok": True}


@app.get("/proposals/{proposal_id}", response_model=ProposalResponse)
def get_proposal_route(proposal_id: int, user: dict = Depends(get_current_user)):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        p = db.get_proposal(conn, proposal_id)
        if not p:
            raise HTTPException(status_code=404, detail="Proposal not found")
        # Membership check
        claim = db.get_membership_claim(conn, user["id"], p["hoa_id"])
        if not claim:
            raise HTTPException(status_code=403, detail="You must be a member of this HOA")
        # Private proposals: only visible to creator and co-signers
        if p["status"] == "private":
            if p["creator_user_id"] != user["id"] and not db.get_cosigner(conn, proposal_id, user["id"]):
                raise HTTPException(status_code=404, detail="Proposal not found")
        hoa_row = conn.execute("SELECT name FROM hoas WHERE id = ?", (p["hoa_id"],)).fetchone()
        hoa_name = str(hoa_row["name"]) if hoa_row else None
        user_cosigned = db.get_cosigner(conn, proposal_id, user["id"]) is not None
        user_upvoted = db.get_upvote(conn, proposal_id, user["id"]) is not None
        cosigners = db.list_cosigner_names(conn, proposal_id) if p["status"] == "public" else []
        share_code = p["share_code"] if p["creator_user_id"] == user["id"] and p["status"] == "private" else None
    return _proposal_to_response(p, hoa_name=hoa_name, share_code=share_code,
                                  cosigners=cosigners, user_cosigned=user_cosigned, user_upvoted=user_upvoted)


@app.get("/hoas/{hoa_id}/proposals")
def list_hoa_proposals(
    hoa_id: int,
    include_archived: bool = False,
    user: dict = Depends(get_current_user),
):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        claim = db.get_membership_claim(conn, user["id"], hoa_id)
        if not claim:
            raise HTTPException(status_code=403, detail="You must be a member of this HOA")
        proposals = db.list_proposals_for_hoa(conn, hoa_id, include_archived=include_archived)
        hoa_row = conn.execute("SELECT name FROM hoas WHERE id = ?", (hoa_id,)).fetchone()
        hoa_name = str(hoa_row["name"]) if hoa_row else None
        result = []
        for p in proposals:
            user_upvoted = db.get_upvote(conn, p["id"], user["id"]) is not None
            user_cosigned = db.get_cosigner(conn, p["id"], user["id"]) is not None
            cosigners = db.list_cosigner_names(conn, p["id"]) if p["status"] == "public" else []
            result.append(_proposal_to_response(p, hoa_name=hoa_name, cosigners=cosigners, user_upvoted=user_upvoted, user_cosigned=user_cosigned))
    return result
