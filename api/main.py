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
from hoaware.chunker import PageContent
from hoaware.config import load_settings
from hoaware.cost_tracker import COST_DOCAI_PER_PAGE, log_docai_usage
from hoaware import prepared_ingest
from hoaware.doc_classifier import (
    VALID_CATEGORIES,
    REJECT_PII,
    classify_from_filename,
    classify_from_text,
    classify_with_llm,
)
from hoaware.ingest import ingest_pdf_paths
from hoaware.pdf_utils import detect_text_extractable
from hoaware import participation as participation_mod
from hoaware.qa import (
    QAProviderError,
    QATemporaryError,
    get_answer,
    get_answer_multi,
    retrieve_context,
    retrieve_context_multi,
)

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
# Terms of Service version
# ---------------------------------------------------------------------------
# Bump when terms.html materially changes. Clickwrap on /auth/register
# requires the body to include this exact string in `accepted_terms_version`,
# and the value is stored on the users row as evidence of consent.
TOS_VERSION = "2026-05-10"


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


def _new_job_id() -> str:
    """Opaque queue identifier for pending_ingest rows.

    A UUIDv4 is plenty — the queue is small (<10k rows steady-state) and
    job_id only needs to be unique, not sortable. ULID/KSUID would add a
    dependency for no real benefit.
    """
    import uuid
    return uuid.uuid4().hex


def _check_disk_free(docs_root: Path) -> None:
    """Abort ingest if the persistent disk is too close to full.

    Render disks can't shrink and can't be auto-grown mid-batch, so a runaway
    drain that fills /var/data takes the API down. Threshold is configurable
    via MIN_FREE_DISK_GB (default 10).
    """
    try:
        min_gb = float(os.environ.get("MIN_FREE_DISK_GB", "10"))
    except ValueError:
        min_gb = 10.0
    try:
        # Walk up to a parent that exists — docs_root may not be created yet.
        check_path = docs_root
        while not check_path.exists():
            parent = check_path.parent
            if parent == check_path:
                return
            check_path = parent
        free_gb = shutil.disk_usage(str(check_path)).free / (1024 ** 3)
    except OSError:
        return  # best-effort; don't block on a stat failure
    if free_gb < min_gb:
        raise HTTPException(
            status_code=503,
            detail=f"disk free {free_gb:.1f} GB below MIN_FREE_DISK_GB={min_gb:.0f} GB threshold; ingest paused",
        )


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


def _refit_polygon_centers_on_boot() -> None:
    """Idempotent boot-time fix: when an HOA has a polygon AND a lat/lon far
    from the polygon centroid, the lat/lon is almost certainly a stale
    city-center geocode from the pre-fix /upload code path. Replace it with
    the polygon centroid. Runs every boot; a no-op once data is consistent.
    """
    try:
        import math
        settings = load_settings()
        threshold_km = float(os.environ.get("REFIT_POLYGON_THRESHOLD_KM", "1.5"))

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
            updates: list[tuple[str, float, float]] = []
            for r in rows:
                center = _center_from_boundary_geojson(r["boundary_geojson"])
                if not center:
                    continue
                new_lat, new_lon = center
                if r["latitude"] is None or r["longitude"] is None:
                    updates.append((r["name"], new_lat, new_lon))
                    continue
                if _km(r["latitude"], r["longitude"], new_lat, new_lon) > threshold_km:
                    updates.append((r["name"], new_lat, new_lon))
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
            if updates:
                logger.info("refit_polygon_centers: corrected %d HOA pin(s)", len(updates))
    except Exception:
        logger.exception("refit_polygon_centers boot migration failed")


def _seed_location_quality_on_boot() -> None:
    """Idempotent boot-time sweep that closes two leaks:

    1. Any HOA with a polygon but a NULL location_quality gets tagged 'polygon'
       so the map filter includes legacy rows created before the column existed.
    2. Any cluster of 5+ HOAs in the same city sharing the same lat/lon
       (rounded to 3 decimals, ~110m) where every row in the cluster has a
       NULL location_quality is tagged 'city_only'. Bulk imports that don't
       go through /upload's quality derivation can sneak in city-center
       geocodes; this catches them before they pollute the map.
    """
    try:
        settings = load_settings()
        with db.get_connection(settings.db_path) as conn:
            cur = conn.execute(
                """
                UPDATE hoa_locations
                SET location_quality = 'polygon'
                WHERE boundary_geojson IS NOT NULL
                  AND (location_quality IS NULL OR location_quality = '')
                """
            )
            conn.commit()
            if cur.rowcount:
                logger.info("seed_location_quality: tagged %d polygon row(s)", cur.rowcount)

            stack_rows = conn.execute(
                """
                SELECT id, LOWER(city) AS city_lc,
                       ROUND(latitude, 3) AS lat3,
                       ROUND(longitude, 3) AS lon3
                FROM hoa_locations
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                  AND boundary_geojson IS NULL
                  AND (location_quality IS NULL OR location_quality = '')
                  AND city IS NOT NULL
                """
            ).fetchall()
            from collections import defaultdict
            buckets: dict[tuple, list[int]] = defaultdict(list)
            for r in stack_rows:
                buckets[(r["city_lc"], r["lat3"], r["lon3"])].append(int(r["id"]))
            stack_ids = [i for ids in buckets.values() if len(ids) >= 5 for i in ids]
            if stack_ids:
                placeholders = ",".join("?" * len(stack_ids))
                conn.execute(
                    f"UPDATE hoa_locations SET location_quality = 'city_only' WHERE id IN ({placeholders})",
                    stack_ids,
                )
                conn.commit()
                logger.info("seed_location_quality: demoted %d city-stack row(s) to city_only", len(stack_ids))
    except Exception:
        logger.exception("seed_location_quality boot migration failed")


def _request_log_pruner() -> None:
    """Daemon: periodically prune old request_log rows.

    Runs once on boot, then every 6h. Retention is REQUEST_LOG_RETENTION_DAYS
    (default 30). Logs at INFO when rows are deleted, never raises.
    """
    import time as _t
    while True:
        try:
            retention = int(os.environ.get("REQUEST_LOG_RETENTION_DAYS", "30"))
            settings = load_settings()
            with db.get_connection(settings.db_path) as conn:
                deleted = db.prune_request_log(conn, retention)
            if deleted:
                logger.info("request_log: pruned %d rows older than %d days", deleted, retention)
        except Exception:
            logger.exception("request_log pruner failed")
        _t.sleep(6 * 3600)


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
        threading.Thread(target=_refit_polygon_centers_on_boot, daemon=True).start()
        threading.Thread(target=_seed_location_quality_on_boot, daemon=True).start()
        threading.Thread(target=_request_log_pruner, daemon=True).start()
    except Exception as exc:
        logger.error("Startup migration error (non-fatal): %s", exc)
    yield


app = FastAPI(title="HOA QA API", version="0.2.0", lifespan=lifespan)

# Session middleware required by authlib for OAuth state parameter
from starlette.middleware.sessions import SessionMiddleware
_oauth_session_secret = os.environ.get("JWT_SECRET", "dev-secret-change-in-production")
app.add_middleware(SessionMiddleware, secret_key=_oauth_session_secret)


# ---------------------------------------------------------------------------
# Request access logging (writes to request_log table)
# ---------------------------------------------------------------------------
# Skip noisy paths: health checks fire every few seconds, static assets are
# cached, and favicon/robots/sitemap don't help with anomaly detection.
_REQUEST_LOG_SKIP_PREFIXES = (
    "/healthz",
    "/static/",
    "/favicon.ico",
    "/robots.txt",
    "/sitemap.xml",
)


def _request_logging_enabled() -> bool:
    return os.environ.get("REQUEST_LOG_ENABLED", "1") not in ("0", "false", "False")


@app.middleware("http")
async def _log_request_middleware(request: Request, call_next):
    """Persist a row per request to request_log for traffic auditing.

    Best-effort: logging failures never affect the response. Skipped for
    static and health-check paths to keep log volume bounded. Disable with
    REQUEST_LOG_ENABLED=0.
    """
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    try:
        if not _request_logging_enabled():
            return response
        path = request.url.path
        if any(path.startswith(p) for p in _REQUEST_LOG_SKIP_PREFIXES):
            return response
        ip = request.client.host if request.client else None
        ua = request.headers.get("user-agent")
        if ua and len(ua) > 500:
            ua = ua[:500]
        # user_id resolution intentionally omitted: decoding the JWT on every
        # request adds latency, and the IP/UA/path data is enough for the
        # scrape-detection use case. If we ever need per-user audit logs,
        # add `request.state.user_id` from a separate auth-tagging middleware.
        user_id: int | None = None
        settings = load_settings()
        with db.get_connection(settings.db_path) as conn:
            db.log_request(
                conn,
                ip=ip,
                method=request.method,
                path=path,
                query_string=request.url.query or None,
                user_agent=ua,
                user_id=user_id,
                status_code=response.status_code,
                response_ms=elapsed_ms,
            )
    except Exception:
        # Logging is best-effort — don't break the response.
        logger.debug("request_log write failed", exc_info=True)
    return response
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
    # Phase 2 async-mode fields — only populated when ASYNC_INGEST_ENABLED=1.
    job_id: str | None = None
    status_url: str | None = None


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
    location_quality: str | None = None
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
    location_quality: str | None = None


# ---------------------------------------------------------------------------
# Auth models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: str
    password: str = Field(..., min_length=8)
    display_name: str | None = None
    accepted_terms_version: str | None = None


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
    """Sum page counts the SERVER will OCR on this upload.

    Counts files the agent flagged text_extractable=False, EXCLUDING any file
    that came with a sidecar of pre-extracted text — those have already been
    OCR'd locally and the server will skip extract_pages entirely.
    """
    import pypdf as _pypdf
    total = 0
    for p in paths:
        meta = metadata_by_path.get(p) or {}
        if meta.get("pre_extracted_pages") is not None:
            continue
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


_MAX_SIDECAR_BYTES = 10 * 1024 * 1024  # 10 MB JSON sidecar cap


def _parse_extracted_text_sidecars(
    file_count: int,
    extracted_texts: list[str] | None,
) -> list[dict | None]:
    """Parse parallel array of JSON sidecars carrying agent-extracted page text.

    Each entry is either an empty string (server extracts the file as usual)
    or a JSON object: {"pages":[{"number":N,"text":"..."}], "docai_pages":N}.
    Returns one entry per file: dict with keys {pages, docai_pages} or None.
    Raises HTTPException on shape errors.
    """
    if not extracted_texts:
        return [None] * file_count
    if len(extracted_texts) != file_count:
        raise HTTPException(
            status_code=400,
            detail=(
                f"extracted_texts length ({len(extracted_texts)}) must equal "
                f"number of files ({file_count})"
            ),
        )

    out: list[dict | None] = []
    for i, raw in enumerate(extracted_texts):
        if raw is None or raw == "":
            out.append(None)
            continue
        if len(raw.encode("utf-8")) > _MAX_SIDECAR_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file {i}: extracted_texts sidecar exceeds 10 MB",
            )
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"file {i}: extracted_texts is not valid JSON ({exc})",
            )
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400,
                detail=f"file {i}: extracted_texts must be a JSON object",
            )
        raw_pages = payload.get("pages")
        if not isinstance(raw_pages, list) or not raw_pages:
            raise HTTPException(
                status_code=400,
                detail=f"file {i}: extracted_texts.pages must be a non-empty list",
            )
        pages: list[PageContent] = []
        for j, page in enumerate(raw_pages):
            if not isinstance(page, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"file {i}: pages[{j}] must be an object",
                )
            number = page.get("number")
            text = page.get("text", "")
            if not isinstance(number, int) or number < 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"file {i}: pages[{j}].number must be a positive integer",
                )
            if not isinstance(text, str):
                raise HTTPException(
                    status_code=400,
                    detail=f"file {i}: pages[{j}].text must be a string",
                )
            pages.append(PageContent(number=number, text=text))

        docai_pages_raw = payload.get("docai_pages", 0)
        if not isinstance(docai_pages_raw, int) or docai_pages_raw < 0:
            raise HTTPException(
                status_code=400,
                detail=f"file {i}: docai_pages must be a non-negative integer",
            )

        out.append({"pages": pages, "docai_pages": docai_pages_raw})
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


# Phase 2 — when ASYNC_INGEST_ENABLED=1, /upload writes the agent-supplied
# metadata + sidecars to a sidecar JSON in the HOA's docs dir so the worker
# process (separate Python heap, same disk) can pick it up.
_PENDING_UPLOAD_SIDECAR_DIR = ".pending_ingest"


def _persist_upload_sidecar(
    hoa_name: str,
    saved_paths: list[Path],
    metadata_by_path: dict[Path, dict],
    *,
    docs_root: Path,
) -> tuple[str, str]:
    """Write a {job_id}.json sidecar describing this upload.

    Returns (job_id, sidecar_uri) where sidecar_uri is the `local://...`
    URI stored in pending_ingest.bundle_uri for the worker to resolve.

    Keeps the upload payload (PDFs already on disk, per-file metadata)
    durable across the request/worker handoff. The worker reads this
    JSON in lieu of re-receiving the multipart form.
    """
    job_id = _new_job_id()
    sidecar_dir = docs_root / _PENDING_UPLOAD_SIDECAR_DIR
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": job_id,
        "hoa_name": hoa_name,
        "files": [
            {
                "path": str(path.resolve()),
                "metadata": {
                    "category": meta.get("category"),
                    "text_extractable": meta.get("text_extractable"),
                    "source_url": meta.get("source_url"),
                    # pre_extracted_pages is a list of PageContent dataclasses
                    # at this point; persist as plain dicts.
                    "pre_extracted_pages": [
                        {"number": p.number, "text": p.text}
                        for p in (meta.get("pre_extracted_pages") or [])
                    ] if meta.get("pre_extracted_pages") is not None else None,
                    "docai_pages": int(meta.get("docai_pages") or 0),
                },
            }
            for path, meta in metadata_by_path.items()
        ],
    }
    sidecar_path = sidecar_dir / f"{job_id}.json"
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")
    return job_id, f"local://{sidecar_path.resolve()}"


def _process_local_upload_sidecar(sidecar_path: Path, *, settings) -> dict:
    """Worker side of the local-upload async path.

    Reads the {job_id}.json sidecar written by /upload's async path and
    runs `ingest_pdf_paths` on the saved PDFs. Returns the same shape as
    `_process_prepared_bundle` so the worker's mark-done/mark-failed code
    handles both URI schemes identically.
    """
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    hoa_name = payload["hoa_name"]
    saved_paths: list[Path] = []
    metadata_by_path: dict[Path, dict] = {}
    for entry in payload["files"]:
        p = Path(entry["path"])
        if not p.exists():
            return {"status": "failed", "error": f"upload sidecar references missing file: {p}"}
        saved_paths.append(p)
        meta = entry["metadata"] or {}
        if meta.get("pre_extracted_pages") is not None:
            from hoaware.chunker import PageContent
            meta = {
                **meta,
                "pre_extracted_pages": [
                    PageContent(number=int(pg["number"]), text=pg["text"])
                    for pg in meta["pre_extracted_pages"]
                ],
            }
        metadata_by_path[p] = meta

    try:
        stats = ingest_pdf_paths(
            hoa_name,
            saved_paths,
            settings=settings,
            show_progress=False,
            metadata_by_path=metadata_by_path,
        )
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    for path, meta in metadata_by_path.items():
        pages_used = int(meta.get("docai_pages") or 0)
        if pages_used:
            try:
                rel = path.relative_to(settings.docs_root).as_posix()
            except ValueError:
                rel = path.name
            log_docai_usage(pages_used, document=rel)

    # Clean up the sidecar so the .pending_ingest/ dir doesn't grow.
    try:
        sidecar_path.unlink()
    except Exception:
        logger.exception("failed to remove processed upload sidecar %s", sidecar_path)

    return {
        "status": "imported" if stats.failed == 0 else "failed",
        "hoa": hoa_name,
        "processed": stats.processed,
        "indexed": stats.indexed,
        "skipped": stats.skipped,
        "failed": stats.failed,
    }


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


def _derive_location_quality(
    *,
    has_boundary: bool,
    street: str | None,
    postal_code: str | None,
) -> str:
    """Tag a location row based on the strongest field provided. Used by every
    write path that sets lat/lon so map-points filter (which gates on quality)
    excludes rows with only city-level signal — those land on a city center
    and stack hundreds of pins. Higher-quality fields win over lower ones.
    """
    if has_boundary:
        return "polygon"
    if street and street.strip():
        return "address"
    if postal_code and str(postal_code).strip():
        return "zip_centroid"
    return "city_only"


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
        quality = _derive_location_quality(
            has_boundary=False,
            street=parts["street"],
            postal_code=parts["postal_code"],
        ) if (latitude is not None and longitude is not None) else None
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
            location_quality=quality,
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
    today = datetime.now(timezone.utc).date().isoformat()

    def _to_iso_date(value: object) -> str | None:
        """Sitemap lastmod accepts YYYY-MM-DD; trim time component if present."""
        if not value:
            return None
        s = str(value).strip()
        return s.split(" ")[0].split("T")[0] if s else None

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
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>{freq}</changefreq><priority>{priority}</priority></url>"
        )

    # Aggregate per-state and per-city stats including newest lastmod
    states: dict[str, dict] = {}
    cities: dict[tuple[str, str, str], dict] = {}
    for h in hoas:
        s = (h["state"] or "").strip().lower()
        c = (h["city"] or "").strip()
        if not s or not c:
            continue
        last = _to_iso_date(h.get("last_ingested"))
        st = states.setdefault(s, {"count": 0, "last": None})
        st["count"] += 1
        if last and (st["last"] is None or last > st["last"]):
            st["last"] = last
        key = (s, db.slugify_city(c), c)
        ct = cities.setdefault(key, {"count": 0, "last": None})
        ct["count"] += 1
        if last and (ct["last"] is None or last > ct["last"]):
            ct["last"] = last

    # State index pages
    for s in sorted(states):
        last = states[s]["last"] or today
        urls.append(
            f"  <url><loc>https://hoaproxy.org/hoa/{s}/</loc>"
            f"<lastmod>{last}</lastmod>"
            f"<changefreq>weekly</changefreq><priority>0.7</priority></url>"
        )

    # City index pages
    for key in sorted(cities):
        s, cs, _cd = key
        last = cities[key]["last"] or today
        urls.append(
            f"  <url><loc>https://hoaproxy.org/hoa/{s}/{cs}/</loc>"
            f"<lastmod>{last}</lastmod>"
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
        last = _to_iso_date(h.get("last_ingested")) or today
        urls.append(
            f"  <url><loc>https://hoaproxy.org/hoa/{s}/{cs}/{ns}</loc>"
            f"<lastmod>{last}</lastmod>"
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


# Public-facing regional grouping for the homepage state-pill grid, using the
# four top-level US Census regions (Northeast, Midwest, South, West).
_STATE_REGIONS: list[tuple[str, list[str]]] = [
    ("East Coast", ["me", "nh", "vt", "ma", "ri", "ct", "ny", "nj", "pa", "de", "dc", "md", "va"]),
    ("South", ["al", "ar", "fl", "ga", "ky", "la", "ms", "nc", "ok", "sc", "tn", "tx", "wv"]),
    ("Midwest", ["ia", "il", "in", "ks", "mi", "mn", "mo", "nd", "ne", "oh", "sd", "wi"]),
    ("West", ["ak", "az", "ca", "co", "hi", "id", "mt", "nm", "nv", "or", "ut", "wa", "wy"]),
]


@functools.lru_cache(maxsize=1)
def _load_index_template() -> str:
    return (STATIC_DIR / "index.html").read_text()


# Module-level TTL cache for state counts on the homepage. Re-queried at
# most every 5 minutes; the underlying list barely changes between deploys.
_INDEX_STATES_CACHE: dict[str, object] = {"ts": 0.0, "states": []}
_INDEX_STATES_TTL = 300


def _get_index_state_counts() -> list[dict]:
    now = time.time()
    cached_ts = _INDEX_STATES_CACHE["ts"]
    if isinstance(cached_ts, (int, float)) and now - cached_ts < _INDEX_STATES_TTL:
        cached = _INDEX_STATES_CACHE["states"]
        if isinstance(cached, list):
            return cached
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        rows = db.list_hoa_states(conn)
    _INDEX_STATES_CACHE["ts"] = now
    _INDEX_STATES_CACHE["states"] = rows
    return rows


def _render_state_pill_grid(state_counts: list[dict]) -> str:
    """Render homepage state pills as anchor links grouped by Census region.

    Each pill is `<a href="/hoa/{state}/">{NAME} <span>{count}</span></a>` so
    Googlebot follows the link into `/hoa/{state}/` and PageRank flows from `/`
    into the directory. JS attaches click handlers separately to provide
    in-place filter UX for users without overwriting these anchors.
    """
    by_state = {str(r["state"]).lower(): int(r["count"]) for r in state_counts}
    region_html: list[str] = []
    for region, states in _STATE_REGIONS:
        pills: list[str] = []
        for code in states:
            if code not in by_state:
                continue
            count = by_state[code]
            full = _STATE_NAMES.get(code, code.upper())
            pills.append(
                f'<a class="state-pill" href="/hoa/{code}/" data-state="{html_escape(code.upper())}" '
                f'aria-label="{html_escape(full)} — {count} homeowners associations">'
                f'{html_escape(code.upper())} <span class="state-pill-count">{count}</span></a>'
            )
        if not pills:
            continue
        region_html.append(
            f'<div class="state-region">'
            f'<div class="state-region-label">{html_escape(region)}</div>'
            f'<div class="state-region-pills">{"".join(pills)}</div>'
            f'</div>'
        )
    if not region_html:
        return ""
    all_pill = '<button class="state-pill state-pill-all active" type="button" data-state="">All states</button>'
    return (
        '<div class="state-region-grid">'
        + f'<div class="state-region state-region-all">{all_pill}</div>'
        + "".join(region_html)
        + '</div>'
    )


def _render_index() -> HTMLResponse:
    """Render the homepage with SSR'd state-pill grid for SEO + scalability."""
    template = _load_index_template()
    grid_html = _render_state_pill_grid(_get_index_state_counts())
    html = template.replace(
        '<div id="stateFilter" class="state-filter-row"></div>',
        f'<div id="stateFilter" class="state-region-grid-wrap">{grid_html}</div>',
    )
    return HTMLResponse(content=html)


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    if not STATIC_DIR.exists():
        raise HTTPException(status_code=404, detail="UI not available")
    return _render_index()


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


# Human-readable category labels for the SSR document inventory line.
# Keys match VALID_CATEGORIES in hoaware/doc_classifier.py.
_CATEGORY_LABELS_SINGULAR: dict[str, str] = {
    "ccr": "CC&R",
    "bylaws": "set of bylaws",
    "articles": "articles of incorporation",
    "rules": "rules document",
    "amendment": "amendment",
    "resolution": "resolution",
    "plat": "recorded plat",
    "minutes": "meeting minutes document",
    "financial": "financial filing",
    "insurance": "insurance document",
    "unknown": "uncategorized document",
}
_CATEGORY_LABELS_PLURAL: dict[str, str] = {
    "ccr": "CC&Rs",
    "bylaws": "sets of bylaws",
    "articles": "articles of incorporation",
    "rules": "rules documents",
    "amendment": "amendments",
    "resolution": "resolutions",
    "plat": "recorded plats",
    "minutes": "meeting minutes",
    "financial": "financial filings",
    "insurance": "insurance documents",
    "unknown": "uncategorized documents",
}

# Census region grouping, used by the homepage state grid (item #3) and
# also when we need an ordered presentation of categories. Item #2 only
# uses the category labels above; defining region map here keeps SEO
# helpers in one place.


def _format_category_phrase(categories: dict[str, int]) -> str:
    """Render `{2: 'CC&Rs', 1: 'set of bylaws'}` style phrase."""
    if not categories:
        return ""
    # Sort by count desc, then by category name for stability
    items = sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))
    parts = []
    for cat, n in items:
        if n <= 0:
            continue
        label = (_CATEGORY_LABELS_PLURAL if n != 1 else _CATEGORY_LABELS_SINGULAR).get(cat, cat)
        parts.append(f"{n} {label}")
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _format_address(street: str | None, city: str | None, state: str | None, postal: str | None) -> str:
    """Format a US-style mailing address for the SSR overview."""
    line2 = ", ".join([p for p in [city, f"{state.upper()} {postal}".strip() if state or postal else None] if p])
    return ", ".join([p for p in [street, line2] if p])


def _render_hoa_page(
    hoa_name: str,
    hoa_id: int,
    city: str | None,
    state: str | None,
    doc_count: int,
    overview: dict | None = None,
) -> HTMLResponse:
    """Return hoa.html with server-injected SEO metadata and SSR content.

    The `overview` dict (from `db.get_hoa_overview()`) drives the SSR
    overview block — visible body text + FAQ items that give Googlebot
    something unique to index per HOA. Without it, every HOA page is the
    same JS shell.
    """
    template = _load_hoa_template()
    state_upper = state.upper() if state else None
    canonical_path = db.build_hoa_path(hoa_name, city, state)
    canonical = f"https://hoaproxy.org{canonical_path}"

    # --- <title> ---
    title = html_escape(hoa_name)
    if city and state_upper:
        title += f" | {html_escape(city)}, {html_escape(state_upper)}"
    title += " | HOAproxy"

    # --- meta description ---
    desc = f"View governing documents, CC&Rs, bylaws and rules for {html_escape(hoa_name)}"
    if city and state_upper:
        desc += f" in {html_escape(city)}, {html_escape(state_upper)}"
    desc += f". {doc_count} document{'s' if doc_count != 1 else ''} available."

    # --- SSR data for client JS ---
    ssr_json = json.dumps(
        {"hoaName": hoa_name, "hoaId": hoa_id, "city": city, "state": state, "docCount": doc_count},
        ensure_ascii=False,
    )

    # --- Organization JSON-LD (existing) ---
    org_ld: dict = {"@context": "https://schema.org", "@type": "Organization", "name": hoa_name}
    if city and state_upper:
        org_ld["address"] = {"@type": "PostalAddress", "addressLocality": city, "addressRegion": state_upper}
        if overview and overview.get("street"):
            org_ld["address"]["streetAddress"] = overview["street"]
        if overview and overview.get("postal_code"):
            org_ld["address"]["postalCode"] = overview["postal_code"]
    org_ld_json = json.dumps(org_ld, ensure_ascii=False)

    # --- BreadcrumbList JSON-LD ---
    crumbs: list[dict] = [{"@type": "ListItem", "position": 1, "name": "HOAproxy", "item": "https://hoaproxy.org/"}]
    if state and state_upper:
        state_full = _STATE_NAMES.get(state.lower(), state_upper)
        crumbs.append({
            "@type": "ListItem",
            "position": len(crumbs) + 1,
            "name": state_full,
            "item": f"https://hoaproxy.org/hoa/{state.lower()}/",
        })
        if city:
            crumbs.append({
                "@type": "ListItem",
                "position": len(crumbs) + 1,
                "name": city,
                "item": f"https://hoaproxy.org/hoa/{state.lower()}/{db.slugify_city(city)}/",
            })
    crumbs.append({
        "@type": "ListItem",
        "position": len(crumbs) + 1,
        "name": hoa_name,
        "item": canonical,
    })
    breadcrumb_ld_json = json.dumps(
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": crumbs},
        ensure_ascii=False,
    )

    # --- SSR overview block + FAQPage JSON-LD ---
    ov = overview or {}
    inventory_phrase = _format_category_phrase(ov.get("doc_categories") or {})
    address_str = _format_address(ov.get("street"), city, state_upper, ov.get("postal_code"))
    last_ingested = ov.get("last_ingested")
    last_date = last_ingested.split(" ")[0] if isinstance(last_ingested, str) else None

    # Sentence 1: location summary
    if city and state_upper:
        sent1 = f"{html_escape(hoa_name)} is a homeowners association in {html_escape(city)}, {html_escape(state_upper)}."
    else:
        sent1 = f"{html_escape(hoa_name)} is a homeowners association tracked on HOAproxy."

    # Sentence 2: mailing address (if present)
    sent_addr = (
        f' Mailing address: {html_escape(address_str)}.'
        if ov.get("street") and address_str
        else ""
    )

    # Sentence 3: document inventory
    if doc_count > 0 and inventory_phrase:
        sent_docs = f"HOAproxy has {doc_count} document{'s' if doc_count != 1 else ''} on file for {html_escape(hoa_name)}: {html_escape(inventory_phrase)}."
    elif doc_count > 0:
        sent_docs = f"HOAproxy has {doc_count} document{'s' if doc_count != 1 else ''} on file for {html_escape(hoa_name)}."
    else:
        sent_docs = (
            f"HOAproxy doesn't have any governing documents on file for {html_escape(hoa_name)} yet. "
            f"Members can upload CC&Rs, bylaws, rules, and amendments below."
        )

    # Sentence 4: last updated
    sent_last = f" Last updated {html_escape(last_date)}." if last_date and doc_count > 0 else ""

    # FAQ items — visible in HTML and emitted as FAQPage schema
    faq_items: list[tuple[str, str]] = []
    if doc_count > 0:
        ans1 = f"HOAproxy has {doc_count} document{'s' if doc_count != 1 else ''} on file"
        if inventory_phrase:
            ans1 += f" for {hoa_name} ({inventory_phrase})"
        ans1 += ". You can browse and search the full text of each document on this page."
    else:
        ans1 = (
            f"HOAproxy doesn't have any governing documents on file for {hoa_name} yet. "
            "Homeowners and board members can upload CC&Rs, bylaws, rules, and amendments using the upload form on this page."
        )
    faq_items.append((f"What governing documents does {hoa_name} have on file?", ans1))

    if city and state_upper:
        loc_ans = f"{hoa_name} is located in {city}, {state_upper}"
        if address_str and ov.get("street"):
            loc_ans += f". Mailing address: {address_str}"
        loc_ans += "."
        faq_items.append((f"Where is {hoa_name} located?", loc_ans))

    faq_items.append((
        f"How do I file a proxy vote for {hoa_name}?",
        f"HOAproxy lets verified members of {hoa_name} assign a proxy to a delegate, sign electronically, and have the signed proxy delivered to the association before the meeting. Sign in or register an HOAproxy account to get started.",
    ))

    # Render the FAQ as collapsed <details> blocks. Content is in static
    # HTML so Googlebot indexes it; the collapsed UI keeps the visible
    # page tight. FAQPage rich-results work with this pattern.
    faq_blocks = []
    for q, a in faq_items:
        faq_blocks.append(
            f'<details><summary>{html_escape(q)}</summary>'
            f'<p>{html_escape(a)}</p></details>'
        )

    # The "About this HOA" card lives at the bottom of the page, after the
    # interactive grid. It carries the SEO-relevant prose (location sentence,
    # mailing address, document inventory, last-updated, FAQ) without
    # crowding the top of the page where users want to see documents and
    # the assistant.
    overview_paras: list[str] = [f'<p>{sent1}{sent_addr}</p>']
    if doc_count > 0 or sent_docs:
        overview_paras.append(f'<p>{sent_docs}{sent_last}</p>')

    overview_html = (
        '<section class="hoa-about" aria-labelledby="hoaAboutHeading">'
        f'<h2 id="hoaAboutHeading">About this HOA</h2>'
        + "".join(overview_paras)
        + '<h2 class="hoa-about-faq-heading">Frequently asked questions</h2>'
        + "".join(faq_blocks)
        + '</section>'
    )

    faq_ld = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": q,
                "acceptedAnswer": {"@type": "Answer", "text": a},
            }
            for q, a in faq_items
        ],
    }
    faq_ld_json = json.dumps(faq_ld, ensure_ascii=False)

    html = template

    # Inject title
    html = html.replace(
        "<title>HOAproxy | HOA Profile</title>",
        f"<title>{title}</title>",
    )

    # Short social title — SERP <title> stays long for keyword reach, but
    # OG/Twitter cards truncate around 60 chars so we shorten here.
    social_title = html_escape(_shorten_for_social(hoa_name))
    og_block = _og_meta_block(
        title=title, desc=desc, canonical=canonical, social_title=social_title,
    ).replace("\n", "\n    ")

    # Inject meta description + canonical + OG + JSON-LD blocks before ga-measurement-id meta
    injected_head = (
        f'<meta name="description" content="{desc}">\n'
        f'    <link rel="canonical" href="{html_escape(canonical)}">\n'
        f'    {og_block}\n'
        f'    <script type="application/ld+json">{org_ld_json}</script>\n'
        f'    <script type="application/ld+json">{breadcrumb_ld_json}</script>\n'
        f'    <script type="application/ld+json">{faq_ld_json}</script>\n'
        f'    <meta name="ga-measurement-id"'
    )
    html = html.replace('<meta name="ga-measurement-id"', injected_head)

    # Inject SSR data script before closing </head>
    html = html.replace("</head>", f'<script>window.__SSR_DATA__={ssr_json};</script>\n  </head>')

    # Pre-populate visible page heading (now an <h1>)
    html = html.replace(
        'id="hoaTitle">Loading HOA...</h1>',
        f'id="hoaTitle">{html_escape(hoa_name)}</h1>',
    )

    # SSR the meta line directly under the H1 so Googlebot sees city/state
    # without running JS (the JS-driven hydration also runs and overwrites
    # this with richer content for users).
    if city and state_upper:
        meta_line = f'{html_escape(city)}, {html_escape(state_upper)}'
        html = html.replace(
            '<div class="hoa-meta" id="hoaMeta"></div>',
            f'<div class="hoa-meta" id="hoaMeta">{meta_line}</div>',
        )

    # Inject SSR overview block at marker
    html = html.replace("<!--SSR_OVERVIEW-->", overview_html)

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
        overview = db.get_hoa_overview(conn, result["hoa_id"])
    return _render_hoa_page(
        hoa_name=result["hoa_name"],
        hoa_id=result["hoa_id"],
        city=result["city"],
        state=result["state"],
        doc_count=result["doc_count"],
        overview=overview,
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

# State-specific intro paragraph for `/hoa/{state}/` index pages (item #4
# of docs/seo-roadmap.md). Each entry should be ~80–120 words of unique
# prose mentioning the state name and the primary HOA-governing statute,
# so the page has rankable content beyond the bare city list. Add new
# entries as additional states come online; states without an entry fall
# back to a generic template that still includes the state name.
_STATE_INTROS: dict[str, str] = {
    "tx": (
        "Homeowners associations in Texas are governed by the Texas Property Code, primarily Chapter 209 "
        "(the Texas Residential Property Owners Protection Act) for HOAs and Chapter 82 (the Texas Uniform "
        "Condominium Act) for condominiums. State law gives Texas homeowners specific rights around access to "
        "governing documents, open board meetings, dedicatory instruments, and proxy voting at member meetings. "
        "Each association also adopts its own CC&Rs, bylaws, rules, and amendments — these are the documents "
        "HOAproxy makes searchable so members can find what their community actually requires without paging "
        "through hundreds of recorded pages."
    ),
    "ca": (
        "California homeowners associations operate under the Davis-Stirling Common Interest Development Act "
        "(California Civil Code §§4000–6150). Davis-Stirling sets out detailed requirements for how associations "
        "publish governing documents, conduct elections, hold open meetings, and respond to records requests. "
        "Beyond state law, every California HOA has its own CC&Rs, bylaws, operating rules, and any amendments "
        "homeowners have adopted over the years — and those documents are usually scattered across the management "
        "company's portal, the county recorder, and old emails. HOAproxy collects them in one searchable place."
    ),
    "co": (
        "Colorado homeowners associations are governed by the Colorado Common Interest Ownership Act (CCIOA, "
        "C.R.S. §38-33.3-101 et seq.). CCIOA sets minimum standards for member meetings, document access, "
        "board elections, and proxy voting that apply on top of each association's own CC&Rs, bylaws, rules, "
        "and amendments. The Colorado Division of Real Estate also requires associations to register annually. "
        "HOAproxy makes the documents that actually govern day-to-day life — what you can build, when dues are "
        "due, how rules get changed — searchable across every Colorado HOA on the platform."
    ),
    "nc": (
        "North Carolina homeowners associations are governed primarily by the North Carolina Planned Community Act "
        "(NCGS Chapter 47F) and, for condominiums, the NC Condominium Act (Chapter 47C). State law sets a baseline "
        "for member rights around document access, meeting notice, board elections, and proxy voting. On top of that, "
        "every NC association has its own recorded CC&Rs, bylaws, rules, and amendments — and those are often the "
        "documents members actually need to read before disputes, ARC submissions, or board votes. HOAproxy collects "
        "them in one place and makes the full text searchable."
    ),
    "az": (
        "Arizona homeowners associations are governed by the Arizona Planned Community Act "
        "(A.R.S. Title 33, Chapter 16) and, for condominiums, the Condominium Act (Title 33, Chapter 9). "
        "Arizona statutes give homeowners specific rights around access to governing documents, member meetings, "
        "open records, and the procedures associations must follow when changing rules or assessing fines. Each "
        "Arizona HOA also has its own CC&Rs, bylaws, rules, and amendments — HOAproxy makes those documents "
        "searchable so members and prospective buyers can find the rules that actually apply to a property."
    ),
    "sc": (
        "South Carolina homeowners associations operate under the South Carolina Homeowners Association Act "
        "(S.C. Code Title 27, Chapter 30), enacted in 2018, which requires associations to record their "
        "governing documents with the county and gives homeowners a baseline of rights around access. Each "
        "association also has its own recorded CC&Rs, bylaws, rules, and any amendments. HOAproxy collects "
        "those documents in one place and makes their full text searchable, so South Carolina homeowners can "
        "actually find the provisions that apply to their property without filing a records request."
    ),
    "va": (
        "Virginia homeowners associations are governed primarily by the Virginia Property Owners' Association "
        "Act (Va. Code §§55.1-1800 et seq.) and, for condominiums, the Virginia Condominium Act (§§55.1-1900 et seq.). "
        "Virginia law sets a baseline for member access to governing documents, open meetings, board elections, "
        "and proxy voting. Each Virginia association also has its own CC&Rs, bylaws, rules, and amendments — "
        "HOAproxy aggregates those documents and makes the full text searchable across every Virginia HOA on "
        "the platform."
    ),
}

# Static metro groupings for state index pages with enough cities that a
# flat alphabetical list is unwieldy. States without an entry fall back
# to the flat list. Cities are matched by name (case-insensitive) against
# the rows from `db.list_cities_in_state()`.
_STATE_METROS: dict[str, list[tuple[str, list[str]]]] = {
    "tx": [
        ("Houston Metro", [
            "Houston", "Katy", "Sugar Land", "Pearland", "The Woodlands", "Spring", "Cypress",
            "Humble", "Kingwood", "Tomball", "Conroe", "Friendswood", "League City", "Missouri City",
            "Richmond", "Rosenberg", "Magnolia", "Fulshear", "Manvel", "Pasadena", "Baytown",
        ]),
        ("Dallas–Fort Worth", [
            "Dallas", "Fort Worth", "Plano", "Frisco", "McKinney", "Allen", "Arlington", "Irving",
            "Garland", "Mesquite", "Carrollton", "Lewisville", "Flower Mound", "Grapevine",
            "Coppell", "Richardson", "Mansfield", "Euless", "Hurst", "Bedford", "Grand Prairie",
            "Rowlett", "Wylie", "Sachse", "Murphy", "Little Elm", "Prosper", "Celina", "Anna",
            "Forney", "Rockwall", "Heath", "Waxahachie", "Midlothian", "Cedar Hill", "DeSoto",
            "Lancaster", "Duncanville", "Burleson", "Crowley", "Keller", "Southlake", "Roanoke",
            "Trophy Club", "Westlake", "Argyle", "Justin", "Aubrey",
        ]),
        ("Austin Metro", [
            "Austin", "Round Rock", "Cedar Park", "Pflugerville", "Leander", "Georgetown",
            "Kyle", "Buda", "Hutto", "Lago Vista", "Manor", "Bastrop", "Liberty Hill", "Dripping Springs",
            "Lakeway", "Bee Cave",
        ]),
        ("San Antonio Metro", [
            "San Antonio", "New Braunfels", "Schertz", "Cibolo", "Universal City", "Converse",
            "Live Oak", "Helotes", "Boerne", "Bulverde", "Selma",
        ]),
    ],
    "nc": [
        ("Charlotte Metro", [
            "Charlotte", "Concord", "Huntersville", "Cornelius", "Davidson", "Mooresville",
            "Matthews", "Mint Hill", "Pineville", "Indian Trail", "Waxhaw", "Weddington",
            "Monroe", "Stallings", "Harrisburg", "Kannapolis",
        ]),
        ("Triangle (Raleigh–Durham)", [
            "Raleigh", "Durham", "Cary", "Apex", "Wake Forest", "Holly Springs", "Fuquay-Varina",
            "Morrisville", "Chapel Hill", "Garner", "Knightdale", "Wendell", "Zebulon",
            "Clayton", "Smithfield", "Hillsborough", "Pittsboro", "Carrboro",
        ]),
        ("Triad (Greensboro–Winston-Salem)", [
            "Greensboro", "Winston-Salem", "High Point", "Burlington", "Kernersville",
            "Clemmons", "Lewisville", "Jamestown", "Oak Ridge", "Summerfield",
        ]),
        ("Coastal", [
            "Wilmington", "Leland", "Hampstead", "Surf City", "Sneads Ferry", "Carolina Beach",
            "Wrightsville Beach", "Topsail Beach", "New Bern", "Morehead City", "Beaufort",
            "Emerald Isle", "Atlantic Beach", "Jacksonville", "Hubert", "Cape Carteret",
            "Kitty Hawk", "Kill Devil Hills", "Nags Head", "Manteo", "Corolla", "Duck",
        ]),
    ],
}


def _og_meta_block(
    *, title: str, desc: str, canonical: str, social_title: str | None = None
) -> str:
    """Render the Open Graph + Twitter Card meta tags shared by all pages.

    `social_title` overrides the SERP `<title>` for OG/Twitter, where short
    titles render better. SERP titles benefit from being long-tail and
    keyword-rich; social previews don't.
    """
    image = "https://hoaproxy.org/static/og-card.png"
    og_title = social_title or title
    return (
        f'<meta property="og:type" content="website">\n'
        f'<meta property="og:url" content="{html_escape(canonical)}">\n'
        f'<meta property="og:title" content="{og_title}">\n'
        f'<meta property="og:description" content="{html_escape(desc)}">\n'
        f'<meta property="og:image" content="{image}">\n'
        f'<meta property="og:image:width" content="1200">\n'
        f'<meta property="og:image:height" content="630">\n'
        f'<meta property="og:site_name" content="HOAproxy">\n'
        f'<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:title" content="{og_title}">\n'
        f'<meta name="twitter:description" content="{html_escape(desc)}">\n'
        f'<meta name="twitter:image" content="{image}">'
    )


def _shorten_for_social(text: str, limit: int = 60) -> str:
    """Truncate to fit in OG/Twitter title limits without breaking words."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return f"{cut}…"


_INDEX_PAGE_CSS = """\
@import url("https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&family=Space+Grotesk:wght@600;700&display=swap");
:root { --bg:#eef5ff; --ink:#12233a; --muted:#587091; --line:#d3e0f4; --accent:#1662f3; }
* { box-sizing:border-box; }
body { margin:0; min-height:100vh; font-family:"Manrope","Segoe UI",sans-serif; color:var(--ink);
  background:linear-gradient(180deg,#f8fbff 0%,var(--bg) 54%,#edf3ff 100%); }
.shell { width:min(860px,94vw); margin:40px auto 60px; }
.card { border:1px solid var(--line); border-radius:18px; background:rgba(255,255,255,0.94);
  box-shadow:0 10px 32px rgba(16,40,73,0.09); padding:28px; }
h1 { margin:0 0 6px; font-family:"Space Grotesk","Manrope",sans-serif; font-size:clamp(1.4rem,3vw,2rem); }
h2 { font-family:"Space Grotesk","Manrope",sans-serif; font-size:1.05rem; margin:24px 0 10px; }
.breadcrumb { margin-bottom:16px; font-size:0.9rem; color:var(--muted); }
.breadcrumb a { color:var(--accent); text-decoration:none; font-weight:600; }
.intro { color:var(--ink); font-size:0.96rem; line-height:1.55; margin:0 0 16px; }
.subhead { color:var(--muted); margin:0 0 18px; font-size:0.95rem; }
ul.entries { list-style:none; padding:0; margin:0; columns:2; column-gap:24px; }
ul.entries.flat { columns:1; }
ul.entries li { margin:6px 0; break-inside:avoid; }
ul.entries a { color:var(--accent); font-weight:700; text-decoration:none; }
.muted-meta { color:var(--muted); font-size:0.86rem; }
.metro-section { margin-top:20px; }
.metro-section h2 { margin-top:18px; }
.about-card {
  margin:32px 0 0; padding:18px 22px;
  border:1px solid var(--line); border-radius:12px;
  background:rgba(255,255,255,0.7); color:var(--ink);
  font-size:0.9rem; line-height:1.55;
}
.about-card h2 {
  margin:0 0 6px; font-size:0.85rem; color:var(--muted);
  text-transform:uppercase; letter-spacing:0.06em; font-weight:700;
}
.about-card p { margin:0; }
"""


def _generic_state_intro(state_full: str, total: int, n_cities: int) -> str:
    return (
        f"Homeowners associations in {state_full} are governed by both state law — including the "
        "rules each state sets for HOA documents, member meetings, voting, and records access — and "
        "by each association's own recorded CC&Rs, bylaws, rules, and amendments. HOAproxy collects "
        f"those governing documents for {total} {state_full} HOAs across {n_cities} cit"
        f"{'ies' if n_cities != 1 else 'y'} and makes the full text searchable, so members can find "
        "the rules that actually apply to their property without paging through hundreds of recorded pages."
    )


def _build_metro_groups(
    state_code: str, cities: list[dict]
) -> tuple[list[tuple[str, list[dict]]], list[dict]]:
    """Group cities into named metros for states in `_STATE_METROS`.

    Returns ``(metros, leftover)`` where ``metros`` is a list of
    ``(metro_name, [city_row, ...])`` and ``leftover`` are cities that
    didn't match any metro (rendered under "Other cities"). For states
    not in `_STATE_METROS`, returns ``([], cities)``.
    """
    rules = _STATE_METROS.get(state_code.lower())
    if not rules:
        return [], cities

    by_lower = {c["city"].lower(): c for c in cities if c.get("city")}
    used: set[str] = set()
    out: list[tuple[str, list[dict]]] = []
    for metro_name, city_list in rules:
        bucket = []
        for cn in city_list:
            row = by_lower.get(cn.lower())
            if row and cn.lower() not in used:
                bucket.append(row)
                used.add(cn.lower())
        if bucket:
            out.append((metro_name, bucket))
    leftover = [c for c in cities if c.get("city", "").lower() not in used]
    return out, leftover


def _city_list_html(state_code: str, cities: list[dict]) -> str:
    parts: list[str] = []
    for c in cities:
        city_slug = db.slugify_city(c["city"])
        href = f"/hoa/{state_code.lower()}/{html_escape(city_slug)}/"
        name = html_escape(c["city"])
        count = c["hoa_count"]
        parts.append(
            f'<li><a href="{href}">{name}</a> '
            f'<span class="muted-meta">— {count} HOA{"s" if count != 1 else ""}</span></li>'
        )
    return "".join(parts)


@app.get("/hoa/{state}/{city}/", include_in_schema=False)
def hoa_city_index(state: str, city: str) -> HTMLResponse:
    """List all HOAs in a city, with intro copy + a featured-HOAs section."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoas = db.list_hoas_in_city(conn, state, city)
    if not hoas:
        raise HTTPException(status_code=404, detail="No HOAs found for this city")
    city_display = hoas[0]["city"]
    state_upper = state.upper()
    state_lower = state.lower()
    state_full = _STATE_NAMES.get(state_lower, state_upper)
    title = f"HOAs in {html_escape(city_display)}, {state_upper} | HOAproxy"
    desc = (
        f"Browse {len(hoas)} homeowners associations in {html_escape(city_display)}, {state_upper}. "
        "View CC&Rs, bylaws, and governing documents."
    )
    intro = (
        f"There {'is' if len(hoas) == 1 else 'are'} {len(hoas)} homeowners association"
        f"{'s' if len(hoas) != 1 else ''} on HOAproxy in {html_escape(city_display)}, {state_upper}. "
        f"Each {html_escape(city_display)} HOA on the site has its own profile page where you can browse "
        "governing documents (CC&Rs, bylaws, rules, amendments), search the full text, file a proxy vote, "
        "and check participation history."
    )

    # Full alphabetical list
    rows_html: list[str] = []
    for h in hoas:
        href = html_escape(db.build_hoa_path(h["hoa_name"], h["city"], h["state"]))
        name = html_escape(h["hoa_name"])
        docs = h["doc_count"]
        rows_html.append(
            f'<li><a href="{href}">{name}</a> '
            f'<span class="muted-meta">— {docs} doc{"s" if docs != 1 else ""}</span></li>'
        )

    canonical = f"https://hoaproxy.org/hoa/{state_lower}/{db.slugify_city(city_display)}/"
    breadcrumb_ld = {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "HOAproxy", "item": "https://hoaproxy.org/"},
            {"@type": "ListItem", "position": 2, "name": state_full, "item": f"https://hoaproxy.org/hoa/{state_lower}/"},
            {"@type": "ListItem", "position": 3, "name": city_display, "item": canonical},
        ],
    }
    collection_ld = {
        "@context": "https://schema.org", "@type": "CollectionPage",
        "name": f"HOAs in {city_display}, {state_upper}",
        "description": desc,
        "url": canonical,
    }

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{html_escape(desc)}">
<link rel="canonical" href="{html_escape(canonical)}">
{_og_meta_block(title=title, desc=desc, canonical=canonical)}
<meta name="ga-measurement-id" content="G-BV7JXG4JDE">
<script src="/static/js/analytics.js"></script>
<title>{title}</title>
<link rel="stylesheet" href="/static/css/mobile.css">
<style>{_INDEX_PAGE_CSS}</style>
<script type="application/ld+json">{json.dumps(breadcrumb_ld, ensure_ascii=False)}</script>
<script type="application/ld+json">{json.dumps(collection_ld, ensure_ascii=False)}</script>
</head><body>
<main class="shell"><div class="card">
<div class="breadcrumb"><a href="/">HOAproxy</a> › <a href="/hoa/{state_lower}/">{html_escape(state_full)}</a> › {html_escape(city_display)}</div>
<h1>HOAs in {html_escape(city_display)}, {state_upper}</h1>
<p class="subhead">{len(hoas)} homeowners association{"s" if len(hoas) != 1 else ""}</p>
<ul class="entries">{"".join(rows_html)}</ul>
<section class="about-card"><h2>About {html_escape(city_display)} HOAs on HOAproxy</h2><p>{intro}</p></section>
</div></main></body></html>"""
    return HTMLResponse(content=html)


@app.get("/hoa/{state}/", include_in_schema=False)
def hoa_state_index(state: str) -> HTMLResponse:
    """List cities (grouped by metro where applicable) + featured HOAs."""
    state_lower = state.lower()
    state_upper = state.upper()
    state_full = _STATE_NAMES.get(state_lower, state_upper)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        cities = db.list_cities_in_state(conn, state)
    if not cities:
        raise HTTPException(status_code=404, detail="No HOAs found for this state")
    total = sum(c["hoa_count"] for c in cities)
    title = f"HOAs in {html_escape(state_full)} | HOAproxy"
    desc = f"Browse {total} homeowners associations across {len(cities)} cities in {html_escape(state_full)}."
    intro = _STATE_INTROS.get(state_lower) or _generic_state_intro(state_full, total, len(cities))

    metros, leftover = _build_metro_groups(state_lower, cities)

    if metros:
        body_parts: list[str] = []
        for metro_name, city_rows in metros:
            metro_total = sum(c["hoa_count"] for c in city_rows)
            body_parts.append(
                f'<section class="metro-section"><h2>{html_escape(metro_name)} '
                f'<span class="muted-meta" style="font-weight:400">— {metro_total} HOA'
                f'{"s" if metro_total != 1 else ""}</span></h2>'
                f'<ul class="entries">{_city_list_html(state_lower, city_rows)}</ul></section>'
            )
        if leftover:
            body_parts.append(
                f'<section class="metro-section"><h2>Other cities</h2>'
                f'<ul class="entries">{_city_list_html(state_lower, leftover)}</ul></section>'
            )
        cities_html = "".join(body_parts)
    else:
        cities_html = (
            f'<h2>Cities</h2>'
            f'<ul class="entries">{_city_list_html(state_lower, cities)}</ul>'
        )

    canonical = f"https://hoaproxy.org/hoa/{state_lower}/"
    breadcrumb_ld = {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "HOAproxy", "item": "https://hoaproxy.org/"},
            {"@type": "ListItem", "position": 2, "name": state_full, "item": canonical},
        ],
    }
    collection_ld = {
        "@context": "https://schema.org", "@type": "CollectionPage",
        "name": f"HOAs in {state_full}",
        "description": desc,
        "url": canonical,
    }

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{html_escape(desc)}">
<link rel="canonical" href="{html_escape(canonical)}">
{_og_meta_block(title=title, desc=desc, canonical=canonical)}
<meta name="ga-measurement-id" content="G-BV7JXG4JDE">
<script src="/static/js/analytics.js"></script>
<title>{title}</title>
<link rel="stylesheet" href="/static/css/mobile.css">
<style>{_INDEX_PAGE_CSS}</style>
<script type="application/ld+json">{json.dumps(breadcrumb_ld, ensure_ascii=False)}</script>
<script type="application/ld+json">{json.dumps(collection_ld, ensure_ascii=False)}</script>
</head><body>
<main class="shell"><div class="card">
<div class="breadcrumb"><a href="/">HOAproxy</a> › {html_escape(state_full)}</div>
<h1>HOAs in {html_escape(state_full)}</h1>
<p class="subhead">{total} homeowners association{"s" if total != 1 else ""} across {len(cities)} cit{"ies" if len(cities) != 1 else "y"}</p>
{cities_html}
<section class="about-card"><h2>About HOAs in {html_escape(state_full)}</h2><p>{intro}</p></section>
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
            overview = db.get_hoa_overview(conn, result["hoa_id"])
        return _render_hoa_page(
            hoa_name=result["hoa_name"],
            hoa_id=result["hoa_id"],
            city=result.get("city"),
            state=result.get("state"),
            doc_count=doc_count,
            overview=overview,
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


def _prepared_gcs_bucket(bucket_name: str | None = None):
    from google.cloud import storage as gcs

    client = gcs.Client()
    return client.bucket(bucket_name or prepared_ingest.DEFAULT_PREPARED_BUCKET)


def _prepared_target_pdf_path(
    *,
    hoa_dir: Path,
    filename: str,
    expected_sha256: str,
    pdf_bytes: bytes,
) -> Path:
    prepared_name = filename.strip() if filename else ""
    if not prepared_name.lower().endswith(".pdf"):
        stem = Path(prepared_name).name.strip() if prepared_name else ""
        if not stem or stem in {".", ".."}:
            stem = expected_sha256[:12]
        prepared_name = f"{stem}.pdf"
    safe_name = _safe_pdf_filename(prepared_name)
    actual_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    if actual_sha256 != expected_sha256:
        raise prepared_ingest.PreparedIngestError(
            f"PDF checksum mismatch for {filename}: expected {expected_sha256}, got {actual_sha256}"
        )

    target = hoa_dir / safe_name
    if target.exists():
        existing_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        if existing_sha256 == expected_sha256:
            return target
        target = hoa_dir / f"{expected_sha256[:12]}-{safe_name}"
    target.write_bytes(pdf_bytes)
    return target


def _prepared_bundle_location_fields(bundle: prepared_ingest.PreparedBundle) -> dict:
    address = bundle.address or {}
    geometry = bundle.geometry or {}
    boundary_raw = geometry.get("boundary_geojson")
    if isinstance(boundary_raw, dict):
        boundary_input = json.dumps(boundary_raw)
    elif isinstance(boundary_raw, str):
        boundary_input = boundary_raw
    else:
        boundary_input = None
    boundary_geojson = _parse_boundary_geojson(boundary_input)

    latitude = geometry.get("latitude")
    longitude = geometry.get("longitude")
    try:
        latitude = float(latitude) if latitude is not None else None
        longitude = float(longitude) if longitude is not None else None
    except (TypeError, ValueError):
        latitude = None
        longitude = None
    if (latitude is None or longitude is None) and boundary_geojson:
        center = _center_from_boundary_geojson(boundary_geojson)
        if center:
            latitude, longitude = center

    metadata_type = (bundle.metadata_type or "").strip().lower() or None
    if metadata_type not in {"hoa", "condo", "coop", "timeshare"}:
        metadata_type = None
    quality_hint = geometry.get("location_quality")
    if quality_hint not in {"polygon", "address", "place_centroid", "zip_centroid", "city_only", "unknown"}:
        quality_hint = None

    return {
        "metadata_type": metadata_type,
        "website_url": _normalize_website_url(bundle.website_url),
        "street": (address.get("street") or None),
        "city": (address.get("city") or None),
        "state": ((address.get("state") or bundle.state).strip().upper()),
        "postal_code": (address.get("postal_code") or None),
        "country": ((address.get("country") or "US").strip().upper()),
        "latitude": latitude,
        "longitude": longitude,
        "boundary_geojson": boundary_geojson,
        "location_quality": quality_hint or (
            _derive_location_quality(
                has_boundary=bool(boundary_geojson),
                street=address.get("street"),
                postal_code=address.get("postal_code"),
            )
            if latitude is not None and longitude is not None
            else None
        ),
    }


def _async_ingest_enabled() -> bool:
    """Phase 2 feature flag — when on, /upload and /admin/ingest-ready-gcs
    enqueue into pending_ingest instead of running the synchronous ingest
    body. The hoaproxy-ingest worker drains the queue. Default OFF until
    cutover (see docs/phase2-cutover.md)."""
    return os.environ.get("ASYNC_INGEST_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}


def _process_prepared_bundle(
    prefix: str,
    *,
    state_n: str,
    bucket=None,
    bucket_name: str | None = None,
    settings=None,
    claim: bool = True,
    dry_run: bool = False,
) -> dict:
    """Synchronous body of /admin/ingest-ready-gcs for a single bundle prefix.

    Shared between the (legacy) sync admin route and the Phase 2 background
    worker (hoaware.ingest_worker). The caller is responsible for any
    state-level validation and disk-free precheck.

    Returns the per-prefix result dict (always — never raises). On failure
    the bucket status.json is updated to 'failed' (unless dry_run).
    """
    settings = settings or load_settings()
    if bucket is None:
        bucket = _prepared_gcs_bucket(bucket_name)

    if claim and not dry_run:
        claimed = prepared_ingest.claim_ready_bundle(bucket, prefix)
        if not claimed:
            # The worker may re-attempt a bundle whose GCS status is already
            # 'claimed' (it was claimed in a previous attempt and we crashed
            # before marking imported/failed). Allow that to proceed.
            status_blob = bucket.blob(prepared_ingest.status_blob_name(prefix))
            try:
                status_data, _ = prepared_ingest._load_status_with_generation(status_blob)
            except Exception:
                return {"prefix": prefix, "status": "skipped", "reason": "not_ready"}
            if status_data.get("status") not in {"claimed"}:
                return {"prefix": prefix, "status": "skipped", "reason": "not_ready"}

    try:
        bundle_payload = prepared_ingest.load_json_blob(
            bucket, prepared_ingest.bundle_blob_name(prefix)
        )
        bundle = prepared_ingest.validate_bundle(bundle_payload, expected_state=state_n)
        resolved_hoa = _resolve_hoa_name(bundle.hoa_name)

        settings.docs_root.mkdir(parents=True, exist_ok=True)
        hoa_dir = settings.docs_root / resolved_hoa
        if not dry_run:
            hoa_dir.mkdir(parents=True, exist_ok=True)

        saved_paths: list[Path] = []
        metadata_by_path: dict[Path, dict] = {}
        imported_docai_pages = 0
        for doc in bundle.documents:
            pdf_uri = prepared_ingest.parse_gcs_uri(doc.pdf_gcs_path)
            text_uri = prepared_ingest.parse_gcs_uri(doc.text_gcs_path)
            if pdf_uri.bucket != bucket.name or text_uri.bucket != bucket.name:
                raise prepared_ingest.PreparedIngestError(
                    "prepared document paths must point at the prepared queue bucket"
                )

            sidecar_payload = prepared_ingest.load_json_blob(bucket, text_uri.blob)
            pages, sidecar_docai_pages = prepared_ingest.validate_text_sidecar(sidecar_payload)
            imported_docai_pages += sidecar_docai_pages

            if dry_run:
                continue

            _check_disk_free(settings.docs_root)
            pdf_bytes = prepared_ingest.download_blob_bytes(bucket, pdf_uri.blob)
            target = _prepared_target_pdf_path(
                hoa_dir=hoa_dir,
                filename=doc.filename,
                expected_sha256=doc.sha256,
                pdf_bytes=pdf_bytes,
            )
            saved_paths.append(target)
            metadata_by_path[target] = {
                "category": doc.category,
                "text_extractable": doc.text_extractable,
                "source_url": doc.source_url,
                "pre_extracted_pages": pages,
                "docai_pages": sidecar_docai_pages,
            }

        if dry_run:
            return {
                "prefix": prefix,
                "status": "ready",
                "hoa": resolved_hoa,
                "documents": len(bundle.documents),
            }

        if any(
            meta.get("pre_extracted_pages") is None
            for meta in metadata_by_path.values()
        ):
            raise prepared_ingest.PreparedIngestError(
                "prepared bundle is missing text sidecars"
            )

        location = _prepared_bundle_location_fields(bundle)
        with db.get_connection(settings.db_path) as conn:
            existing_location = conn.execute(
                """
                SELECT l.state
                FROM hoa_locations l
                JOIN hoas h ON h.id = l.hoa_id
                WHERE lower(h.name) = lower(?)
                """,
                (resolved_hoa,),
            ).fetchone()
            has_new_spatial = (
                location["latitude"] is not None
                and location["longitude"] is not None
            ) or bool(location["boundary_geojson"])
            clear_stale_spatial = (
                not has_new_spatial
                and existing_location is not None
                and (existing_location["state"] or "").upper() != location["state"]
            )
            db.upsert_hoa_location(
                conn,
                resolved_hoa,
                metadata_type=location["metadata_type"],
                website_url=location["website_url"],
                street=location["street"],
                city=location["city"],
                state=location["state"],
                postal_code=location["postal_code"],
                country=location["country"],
                latitude=location["latitude"],
                longitude=location["longitude"],
                boundary_geojson=location["boundary_geojson"],
                source="gcs_prepared_ingest",
                location_quality=location["location_quality"],
                clear_coordinates=clear_stale_spatial,
                clear_boundary_geojson=clear_stale_spatial,
            )

        stats = ingest_pdf_paths(
            resolved_hoa,
            saved_paths,
            settings=settings,
            show_progress=False,
            metadata_by_path=metadata_by_path,
        )
        for path, meta in metadata_by_path.items():
            if meta.get("pre_extracted_pages") is not None:
                rel = path.relative_to(settings.docs_root).as_posix()
                pages_used = int(meta.get("docai_pages") or 0)
                if pages_used:
                    log_docai_usage(pages_used, document=rel)

        status = "imported" if stats.failed == 0 else "failed"
        result = {
            "prefix": prefix,
            "status": status,
            "hoa": resolved_hoa,
            "processed": stats.processed,
            "indexed": stats.indexed,
            "skipped": stats.skipped,
            "failed": stats.failed,
            "docai_pages": imported_docai_pages,
        }
        prepared_ingest.update_bundle_status(
            bucket,
            prefix,
            status=status,
            error=None if status == "imported" else "ingest_pdf_paths reported failures",
            extra={"import_result": result},
        )
        return result
    except prepared_ingest.PreparedIngestError as exc:
        if not dry_run:
            try:
                prepared_ingest.update_bundle_status(
                    bucket, prefix, status="failed", error=str(exc)
                )
            except Exception:
                logger.exception("Failed to update prepared-ingest status for %s", prefix)
        return {"prefix": prefix, "status": "failed", "error": str(exc)}
    except Exception as exc:
        logger.exception("Prepared GCS ingest failed for %s", prefix)
        if not dry_run:
            try:
                prepared_ingest.update_bundle_status(
                    bucket, prefix, status="failed", error=str(exc)
                )
            except Exception:
                logger.exception("Failed to update prepared-ingest status for %s", prefix)
        return {"prefix": prefix, "status": "failed", "error": str(exc)}


@app.post("/admin/ingest-ready-gcs")
def admin_ingest_ready_gcs(
    request: Request,
    state: str,
    limit: int = 1,
    dry_run: bool = False,
    bucket_name: str | None = None,
):
    """Import prepared GCS bundles that already contain extracted text sidecars.

    Path is intentionally separate from /upload. It never falls back to
    server-side PDF extraction or DocAI; missing sidecars fail the bundle.

    Phase 2 (scaling-proposal.md §Phase 2): when ASYNC_INGEST_ENABLED=1
    this endpoint becomes a thin enqueue — it lists ready bundles and
    inserts a `pending_ingest` row per prefix without touching DocAI,
    embeddings, or the disk. The hoaproxy-ingest worker drains the queue.
    """
    _require_admin(request)
    try:
        state_n = state.strip().upper()
        if len(state_n) != 2 or not state_n.isalpha():
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="state must be a two-letter abbreviation")
    if limit < 1 or limit > 50:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 50")

    settings = load_settings()
    if not settings.openai_api_key and not dry_run:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is required for ingestion")
    if not dry_run and not _async_ingest_enabled():
        _check_disk_free(settings.docs_root)

    bucket = _prepared_gcs_bucket(bucket_name)
    prefixes = prepared_ingest.list_ready_bundle_prefixes(bucket, state=state_n, limit=limit)

    # Phase 2 async path: just enqueue. The web service does NOT touch
    # DocAI / embeddings / disk on this call.
    if _async_ingest_enabled() and not dry_run:
        enqueued: list[dict] = []
        skipped: list[dict] = []
        with db.get_connection(settings.db_path) as conn:
            # Skip prefixes we've already enqueued and that are not yet 'done'
            # or 'dead'. ('dead' rows mean prior runs exhausted retries — let
            # the operator decide whether to /admin/ingest/retry-dead.)
            for prefix in prefixes:
                bundle_uri = prepared_ingest.gcs_uri(bucket.name, prefix)
                existing = conn.execute(
                    "SELECT job_id, status FROM pending_ingest WHERE bundle_uri = ? "
                    "AND status IN ('pending', 'in_progress')",
                    (bundle_uri,),
                ).fetchone()
                if existing is not None:
                    skipped.append({"prefix": prefix, "job_id": existing["job_id"], "reason": "already_enqueued"})
                    continue
                job_id = _new_job_id()
                # Claim the GCS-side status now so duplicate worker pickups
                # (or a parallel sync call) can't double-process. The worker
                # accepts an already-'claimed' status as resumable.
                claimed_ok = prepared_ingest.claim_ready_bundle(bucket, prefix)
                if not claimed_ok:
                    skipped.append({"prefix": prefix, "reason": "not_ready"})
                    continue
                db.enqueue_pending_ingest(
                    conn,
                    job_id=job_id,
                    bundle_uri=bundle_uri,
                    state=state_n,
                    source="ingest-ready-gcs",
                )
                enqueued.append({"prefix": prefix, "job_id": job_id})
        return {
            "state": state_n,
            "bucket": bucket.name,
            "dry_run": False,
            "async": True,
            "requested_limit": limit,
            "found": len(prefixes),
            "enqueued": len(enqueued),
            "skipped": skipped,
            "job_ids": [item["job_id"] for item in enqueued],
            "results": enqueued,
        }

    results: list[dict] = []
    for prefix in prefixes:
        result = _process_prepared_bundle(
            prefix,
            state_n=state_n,
            bucket=bucket,
            settings=settings,
            claim=not dry_run,
            dry_run=dry_run,
        )
        results.append(result)

    return {
        "state": state_n,
        "bucket": bucket.name,
        "dry_run": dry_run,
        "async": False,
        "requested_limit": limit,
        "found": len(prefixes),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Phase 2 — pending_ingest queue inspection / control
# (see docs/scaling-proposal.md §Phase 2 + docs/phase2-cutover.md)
# ---------------------------------------------------------------------------


@app.get("/ingest/status/{job_id}")
def ingest_status(request: Request, job_id: str) -> dict:
    """Return the current state of an enqueued ingest job. Rate-limited.

    Public read — the job_id itself is opaque enough that no auth is
    required; callers only ever see job_ids returned from their own
    /upload or /admin/ingest-ready-gcs call.
    """
    _check_rate_limit(request, limit=60)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        row = db.get_pending_ingest(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="job_id not found")
    return {
        "job_id": row["job_id"],
        "status": row["status"],
        "state": row["state"],
        "attempts": int(row["attempts"]),
        "enqueued_at": row["enqueued_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "failed_at": row["failed_at"],
        "error": row["error"],
        "source": row["source"],
        "bundle_uri": row["bundle_uri"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
    }


@app.get("/admin/ingest/queue-stats")
def admin_ingest_queue_stats(request: Request) -> dict:
    _require_admin(request)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        counts = db.count_pending_ingest_by_status(conn)
        by_state = conn.execute(
            "SELECT state, status, COUNT(*) AS n FROM pending_ingest "
            "GROUP BY state, status ORDER BY state, status"
        ).fetchall()
    return {
        "counts": counts,
        "by_state": [
            {"state": row["state"], "status": row["status"], "n": int(row["n"])}
            for row in by_state
        ],
    }


@app.post("/admin/ingest/retry-dead")
def admin_ingest_retry_dead(request: Request) -> dict:
    """Flip all dead (max-attempts) jobs back to pending. Admin only.

    Useful after fixing a deploy bug that mass-failed jobs. Bumps the worker's
    chance to drain stuck rows without manual DB surgery.
    """
    _require_admin(request)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        n = db.reset_dead_pending_ingest(conn)
    return {"reset_count": int(n)}


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


@app.post("/admin/wal-checkpoint")
def admin_wal_checkpoint(request: Request):
    """Force a SQLite WAL checkpoint (TRUNCATE mode) to drain pending writes
    back into the main DB and shrink the .wal file to zero. Safe — no data
    loss; just collapses the WAL into the DB.
    """
    _require_admin(request)
    settings = load_settings()
    db_path = Path(settings.db_path)
    wal_path = db_path.with_suffix(db_path.suffix + "-wal")
    before_wal = wal_path.stat().st_size if wal_path.exists() else 0

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()

    after_wal = wal_path.stat().st_size if wal_path.exists() else 0
    return {
        "checkpoint_result": list(result) if result else None,
        "wal_bytes_before": before_wal,
        "wal_bytes_after": after_wal,
        "wal_freed_bytes": before_wal - after_wal,
    }


@app.post("/admin/cleanup-qdrant-local")
def admin_cleanup_qdrant_local(request: Request):
    """Delete the orphaned /var/data/qdrant_local directory. Safe once
    HOA_DISABLE_QDRANT=1 has been in place — search uses sqlite-vec only.
    """
    _require_admin(request)
    import shutil

    target = Path("/var/data/qdrant_local")
    if not target.exists():
        return {"deleted": False, "reason": "path does not exist", "path": str(target)}

    def _du(path: Path) -> int:
        total = 0
        for entry in path.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                continue
        return total

    size_before = _du(target)
    shutil.rmtree(target)
    return {"deleted": True, "path": str(target), "bytes_freed": size_before}


@app.get("/admin/zero-chunk-docs")
def admin_zero_chunk_docs(request: Request):
    """Diagnostic: list documents that have no chunks. Useful for finding
    PDFs that were uploaded but never successfully parsed (e.g., OCR failed,
    DocAI budget exceeded mid-upload, scanned pages with no text layer when
    text_extractable was True, etc.). Returns hidden_reason + text_extractable
    so you can separate real misses from intentionally-hidden docs.
    """
    _require_admin(request)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        cur = conn.execute(
            """
            SELECT
                h.name AS hoa_name,
                d.id AS document_id,
                d.relative_path,
                d.bytes,
                d.page_count,
                d.category,
                d.text_extractable,
                d.hidden_reason,
                d.last_ingested
            FROM documents d
            JOIN hoas h ON h.id = d.hoa_id
            LEFT JOIN chunks c ON c.document_id = d.id
            GROUP BY d.id
            HAVING COUNT(c.id) = 0
            ORDER BY h.name COLLATE NOCASE, d.relative_path COLLATE NOCASE
            """
        )
        rows = cur.fetchall()
    items = [
        {
            "hoa_name": str(r["hoa_name"]),
            "document_id": int(r["document_id"]),
            "relative_path": str(r["relative_path"]),
            "bytes": int(r["bytes"]),
            "page_count": int(r["page_count"]) if r["page_count"] is not None else None,
            "category": str(r["category"]) if r["category"] is not None else None,
            "text_extractable": (bool(r["text_extractable"])
                                 if r["text_extractable"] is not None else None),
            "hidden_reason": str(r["hidden_reason"]) if r["hidden_reason"] is not None else None,
            "last_ingested": str(r["last_ingested"]) if r["last_ingested"] is not None else None,
        }
        for r in rows
    ]
    visible = [i for i in items if not i["hidden_reason"]]
    return {
        "total": len(items),
        "visible_count": len(visible),
        "hidden_count": len(items) - len(visible),
        "items": items,
    }


@app.post("/admin/reingest-failed")
def admin_reingest_failed(
    request: Request,
    limit: int = 10,
    dry_run: bool = False,
    skip_extractable_true: bool = True,
):
    """One-shot recovery: re-attempt ingestion for documents that have 0
    chunks AND no prior loud-OCR failure marker. This catches the silent
    pre-fix cohort (hidden_reason IS NULL, 0 chunks) on first pass; on
    re-failure the loud path sets hidden_reason='ocr_failed:*' which the
    selector then excludes — so persistent failures don't loop forever.

    Operates on PDFs already on disk under HOA_DOCS_ROOT — no re-upload.
    Pre-flights against DAILY_DOCAI_BUDGET_USD; refuses if over. Loop
    client-side until `remaining` is 0.

    `skip_extractable_true=True` (default) leaves the 71 text_extractable=True
    docs alone — they failed via PyPDF, not OCR; bulk reingest won't
    help them and they merit hand-investigation.
    """
    _require_admin(request)
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be 1..500")
    settings = load_settings()
    docs_root = Path(settings.docs_root)

    te_filter = ""
    if skip_extractable_true:
        # text_extractable stored as 0/1/NULL — exclude only the explicit-True case
        te_filter = "AND (d.text_extractable IS NULL OR d.text_extractable = 0)"

    # Exclude only TERMINAL failures from the pool. Transient failures
    # (docai_failed = could be billing flap / Google flake; quota_exceeded =
    # waits out the rolling window) get retried on the next pass — this
    # makes the loop self-healing if billing/quota recovers mid-run.
    # Terminal: file_missing, path_escape, not_configured, page_cap_exceeded.
    terminal = (
        "'ocr_failed:file_missing', 'ocr_failed:path_escape', "
        "'ocr_failed:not_configured', 'ocr_failed:page_cap_exceeded'"
    )
    failed_filter = (
        f"AND (d.hidden_reason IS NULL OR d.hidden_reason NOT IN ({terminal}))"
    )

    with db.get_connection(settings.db_path) as conn:
        cur = conn.execute(
            f"""
            SELECT
                d.id, d.relative_path, d.page_count, d.category,
                d.text_extractable, d.source_url, d.hidden_reason,
                h.name AS hoa_name
            FROM documents d
            JOIN hoas h ON h.id = d.hoa_id
            LEFT JOIN chunks c ON c.document_id = d.id
            WHERE 1=1 {te_filter} {failed_filter}
            GROUP BY d.id
            HAVING COUNT(c.id) = 0
            ORDER BY d.id ASC
            LIMIT ?
            """,
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        remaining_total = conn.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT d.id
                FROM documents d
                LEFT JOIN chunks c ON c.document_id = d.id
                WHERE 1=1 {te_filter} {failed_filter}
                GROUP BY d.id
                HAVING COUNT(c.id) = 0
            )
            """
        ).fetchone()[0]

    if not rows:
        return {
            "indexed": 0, "skipped": 0, "failed": 0,
            "remaining": int(remaining_total), "selected": 0,
            "message": "no rows match",
        }

    # Pre-flight budget check on the OCR-needing subset
    projected_pages = 0
    for r in rows:
        if r["text_extractable"] in (0, None) and r["page_count"]:
            projected_pages += int(r["page_count"])
    try:
        _check_daily_docai_budget(projected_pages)
    except HTTPException as exc:
        return {
            "indexed": 0, "skipped": 0, "failed": 0,
            "remaining": int(remaining_total), "selected": len(rows),
            "projected_ocr_pages": projected_pages,
            "blocked": True, "detail": exc.detail,
        }

    if dry_run:
        return {
            "dry_run": True,
            "selected": len(rows),
            "projected_ocr_pages": projected_pages,
            "projected_ocr_cost_usd": round(projected_pages * COST_DOCAI_PER_PAGE, 4),
            "remaining": int(remaining_total),
            "sample": rows[:5],
        }

    # Group rows by HOA and run ingest. Each HOA opens one DB connection
    # via ingest_pdf_paths; doing it per-HOA keeps the existing semantics.
    by_hoa: dict[str, list[dict]] = {}
    for r in rows:
        by_hoa.setdefault(r["hoa_name"], []).append(r)

    total_indexed = 0
    total_skipped = 0
    total_failed = 0
    quota_aborted = False

    def _mark_failed(hoa_id: int, r: dict, reason: str) -> None:
        """Park a doc that can't be reingested (missing file, path escape) so
        it's excluded from the selector and we don't loop on it forever."""
        with db.get_connection(settings.db_path) as c:
            c.execute(
                "UPDATE documents SET hidden_reason = ? WHERE id = ?",
                (f"ocr_failed:{reason}", r["id"]),
            )
            c.commit()

    for hoa_name, hoa_rows in by_hoa.items():
        paths: list[Path] = []
        metadata_by_path: dict[Path, dict] = {}
        # Resolve hoa_id once per group for the marker helper
        with db.get_connection(settings.db_path) as _c:
            row = _c.execute("SELECT id FROM hoas WHERE name = ?", (hoa_name,)).fetchone()
            hoa_id = int(row["id"]) if row else 0
        for r in hoa_rows:
            p = (docs_root / r["relative_path"]).resolve()
            try:
                p.relative_to(docs_root.resolve())
            except ValueError:
                logger.error("reingest skip — path escape: %s", r["relative_path"])
                _mark_failed(hoa_id, r, "path_escape")
                total_failed += 1
                continue
            if not p.exists():
                logger.error("reingest skip — file missing on disk: %s", p)
                _mark_failed(hoa_id, r, "file_missing")
                total_failed += 1
                continue
            paths.append(p)
            metadata_by_path[p] = {
                "category": r["category"],
                "text_extractable": (
                    None if r["text_extractable"] is None
                    else bool(r["text_extractable"])
                ),
                "source_url": r["source_url"],
            }
        if not paths:
            continue
        try:
            stats = ingest_pdf_paths(
                hoa_name, paths,
                settings=settings, show_progress=False,
                metadata_by_path=metadata_by_path,
            )
            total_indexed += stats.indexed
            total_skipped += stats.skipped
            total_failed += stats.failed
        except Exception as exc:
            # ingest_pdf_paths re-raises on quota_exceeded — that's the
            # signal to stop the whole reingest call and return what we
            # got so far.
            logger.exception("reingest aborted for HOA %s: %s", hoa_name, exc)
            if "quota_exceeded" in str(exc):
                quota_aborted = True
            break

    with db.get_connection(settings.db_path) as conn:
        remaining_total = conn.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT d.id
                FROM documents d
                LEFT JOIN chunks c ON c.document_id = d.id
                WHERE 1=1 {te_filter}
                GROUP BY d.id
                HAVING COUNT(c.id) = 0
            )
            """
        ).fetchone()[0]

    return {
        "indexed": total_indexed,
        "skipped": total_skipped,
        "failed": total_failed,
        "selected": len(rows),
        "projected_ocr_pages": projected_pages,
        "remaining": int(remaining_total),
        "quota_aborted": quota_aborted,
    }


@app.get("/admin/state-doc-coverage")
def admin_state_doc_coverage(request: Request):
    """Per-state count of HOAs with vs without governing documents.

    Single SQL aggregation (one GROUP BY) — fast even on the full live
    DB. Used to track which states are docless-stub-heavy and should
    therefore be prioritized for ``namelist_discover.py`` document
    discovery sweeps.

    Returns:
      {
        "results": [
          {"state": "CA", "live": 25670, "with_docs": 312,
           "without_docs": 25358, "with_docs_pct": 1.2},
          ...
        ],
        "totals": {"live": ..., "with_docs": ..., "without_docs": ...,
                   "with_docs_pct": ...}
      }
    """
    _require_admin(request)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                l.state,
                COUNT(DISTINCT h.id) AS live,
                COUNT(DISTINCT CASE WHEN d.hoa_id IS NOT NULL THEN h.id END) AS with_docs
            FROM hoas h
            JOIN hoa_locations l ON l.hoa_id = h.id
            LEFT JOIN documents d ON d.hoa_id = h.id
            WHERE l.state IS NOT NULL AND l.state != ''
            GROUP BY l.state
            ORDER BY l.state
            """
        ).fetchall()

    results = []
    tot_live = 0
    tot_with = 0
    for r in rows:
        state = str(r["state"]).upper()
        live = int(r["live"])
        with_docs = int(r["with_docs"])
        without_docs = live - with_docs
        pct = round(100.0 * with_docs / live, 2) if live else 0.0
        results.append({
            "state": state,
            "live": live,
            "with_docs": with_docs,
            "without_docs": without_docs,
            "with_docs_pct": pct,
        })
        tot_live += live
        tot_with += with_docs

    return {
        "results": results,
        "totals": {
            "live": tot_live,
            "with_docs": tot_with,
            "without_docs": tot_live - tot_with,
            "with_docs_pct": round(100.0 * tot_with / tot_live, 2) if tot_live else 0.0,
        },
    }


@app.get("/admin/disk-usage")
def admin_disk_usage(request: Request):
    """Temporary diagnostic: report on-disk sizes under /var/data so we can
    see how much the orphaned qdrant_local store is using before deleting it.
    Remove this endpoint once the cleanup decision is made.
    """
    _require_admin(request)
    import shutil

    def _du_bytes(path: Path) -> int:
        total = 0
        if not path.exists():
            return 0
        if path.is_file():
            try:
                return path.stat().st_size
            except OSError:
                return 0
        for entry in path.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                continue
        return total

    def _humanize(n: int) -> str:
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PiB"

    root = Path("/var/data")
    out: dict = {"root": str(root), "exists": root.exists()}
    if not root.exists():
        return out

    top_entries = []
    try:
        for child in sorted(root.iterdir()):
            size = _du_bytes(child)
            top_entries.append({"path": str(child), "bytes": size, "size": _humanize(size)})
    except OSError as exc:
        out["error"] = str(exc)
        return out
    out["entries"] = top_entries

    qdrant_dir = root / "qdrant_local"
    if qdrant_dir.exists():
        segments_dir = qdrant_dir / "collection" / "hoa_all" / "segments"
        seg_breakdown = []
        if segments_dir.exists():
            for seg in sorted(segments_dir.iterdir()):
                if seg.is_dir():
                    size = _du_bytes(seg)
                    seg_breakdown.append({"segment": seg.name, "bytes": size, "size": _humanize(size)})
        out["qdrant_local"] = {
            "total_bytes": _du_bytes(qdrant_dir),
            "total_size": _humanize(_du_bytes(qdrant_dir)),
            "segments": seg_breakdown,
        }

    try:
        usage = shutil.disk_usage(str(root))
        out["disk"] = {
            "total": _humanize(usage.total),
            "used": _humanize(usage.used),
            "free": _humanize(usage.free),
        }
    except OSError:
        pass

    return out


@app.post("/admin/backfill-locations")
def admin_backfill_locations(request: Request, body: dict):
    """Bulk upsert hoa_locations from a posted JSON body. Each entry sets
    location fields and optionally a location_quality flag that gates the map
    filter. Lookup is by exact HOA name (case-insensitive), no creation.

    Body shape:
      {"records": [
        {"hoa": "Park Village",
         "latitude": 35.77, "longitude": -78.85,
         "street": "100 Weston Estates Way", "postal_code": "27513",
         "city": "Cary", "state": "NC",
         "location_quality": "address"},
        ...
      ]}

    Allowed location_quality values: polygon, address, place_centroid,
    zip_centroid, city_only, unknown. Pass "city_only" plus
    clear_coordinates/clear_boundary_geojson to demote stale cross-state map
    geometry when a later state import has no trustworthy location evidence.
    """
    _require_admin(request)
    settings = load_settings()
    records = body.get("records") or []
    valid_quality = {"polygon", "address", "place_centroid", "zip_centroid", "city_only", "unknown"}
    matched = 0
    not_found = 0
    bad_quality = 0
    by_quality: dict[str, int] = {}

    with db.get_connection(settings.db_path) as conn:
        rows = conn.execute("SELECT id, name FROM hoas").fetchall()
        hoa_id_by_lower = {row["name"].lower(): str(row["name"]) for row in rows}

        for entry in records:
            hoa_in = (entry.get("hoa") or "").strip()
            if not hoa_in:
                not_found += 1
                continue
            resolved = hoa_id_by_lower.get(hoa_in.lower())
            if not resolved:
                not_found += 1
                continue
            quality = entry.get("location_quality")
            if quality is not None:
                quality = str(quality).strip()
                if quality not in valid_quality:
                    bad_quality += 1
                    continue
            lat = entry.get("latitude")
            lon = entry.get("longitude")
            if lat is not None:
                lat = float(lat)
                if not (-90 <= lat <= 90):
                    continue
            if lon is not None:
                lon = float(lon)
                if not (-180 <= lon <= 180):
                    continue
            boundary = entry.get("boundary_geojson")
            normalized_boundary = None
            if boundary:
                try:
                    normalized_boundary = _parse_boundary_geojson(boundary)
                except HTTPException:
                    continue
                if normalized_boundary and (lat is None or lon is None):
                    center = _center_from_boundary_geojson(normalized_boundary)
                    if center:
                        lat, lon = center
            db.upsert_hoa_location(
                conn,
                resolved,
                street=(entry.get("street") or None),
                city=(entry.get("city") or None),
                state=(entry.get("state").upper() if entry.get("state") else None),
                postal_code=(entry.get("postal_code") or None),
                country=(entry.get("country").upper() if entry.get("country") else None),
                latitude=lat,
                longitude=lon,
                boundary_geojson=normalized_boundary,
                source=(entry.get("source") or None),
                location_quality=quality,
                clear_coordinates=bool(entry.get("clear_coordinates")),
                clear_boundary_geojson=bool(entry.get("clear_boundary_geojson")),
            )
            matched += 1
            if quality:
                by_quality[quality] = by_quality.get(quality, 0) + 1
        conn.commit()

    return {
        "matched": matched,
        "not_found": not_found,
        "bad_quality": bad_quality,
        "by_quality": by_quality,
    }


@app.post("/admin/rename-hoa")
def admin_rename_hoa(request: Request, body: dict):
    """Rename an HOA in place, or merge it into an existing one if the new
    name is already taken.

    Body shape:
      {"renames": [{"hoa_id": 123, "new_name": "..."}], "dry_run": false}
      OR a single rename via {"hoa_id": 123, "new_name": "...", "dry_run": false}

    Behavior:
      - If `new_name` does not match another HOA, this is a pure rename
        (UPDATE hoas.name). Documents/chunks/locations stay attached.
      - If `new_name` matches an existing HOA (target), this becomes a merge:
        documents whose relative_path is unique to the source are reattached
        to the target. Duplicate documents on the source are dropped (the
        target's row wins). Chunks follow their documents via FK; the vec0
        partition key is rewritten by re-touching chunks.embedding so the
        chunks_vec_update trigger re-derives hoa_id from documents.hoa_id.
        Source's hoa_locations is kept only if target has none. Source row
        is finally deleted.

    Returns counts and per-rename outcomes. Idempotent: renaming to the
    current name is a no-op.
    """
    _require_admin(request)
    import sqlite3
    settings = load_settings()
    items = body.get("renames")
    if not items:
        if "hoa_id" in body and "new_name" in body:
            items = [{"hoa_id": body["hoa_id"], "new_name": body["new_name"]}]
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="renames[] required")
    dry_run = bool(body.get("dry_run") or False)

    results: list[dict] = []
    renamed = merged = noop = errors = 0

    with db.get_connection(settings.db_path) as conn:
        for entry in items:
            try:
                source_id = int(entry.get("hoa_id"))
            except Exception:
                results.append({"status": "error", "reason": "bad hoa_id"})
                errors += 1
                continue
            new_name = (entry.get("new_name") or "").strip()
            if not new_name or len(new_name) > 200:
                results.append({"hoa_id": source_id, "status": "error", "reason": "bad new_name"})
                errors += 1
                continue

            src = conn.execute(
                "SELECT id, name FROM hoas WHERE id = ?", (source_id,)
            ).fetchone()
            if not src:
                results.append({"hoa_id": source_id, "status": "error", "reason": "not_found"})
                errors += 1
                continue
            old_name = str(src["name"])
            if old_name == new_name:
                results.append({"hoa_id": source_id, "status": "noop", "name": new_name})
                noop += 1
                continue

            target = conn.execute(
                "SELECT id FROM hoas WHERE name = ?", (new_name,)
            ).fetchone()
            try:
                if target is None or int(target["id"]) == source_id:
                    if not dry_run:
                        conn.execute("UPDATE hoas SET name = ? WHERE id = ?", (new_name, source_id))
                    results.append({
                        "hoa_id": source_id,
                        "status": "renamed",
                        "old_name": old_name,
                        "new_name": new_name,
                    })
                    renamed += 1
                else:
                    target_id = int(target["id"])
                    if dry_run:
                        results.append({
                            "hoa_id": source_id,
                            "status": "would_merge",
                            "old_name": old_name,
                            "new_name": new_name,
                            "target_id": target_id,
                        })
                        continue
                    # Move documents the target doesn't already have
                    conn.execute(
                        """
                        UPDATE documents SET hoa_id = ?
                        WHERE hoa_id = ?
                          AND relative_path NOT IN (
                            SELECT relative_path FROM documents WHERE hoa_id = ?
                          )
                        """,
                        (target_id, source_id, target_id),
                    )
                    # Re-touch embeddings so chunks_vec_update re-derives hoa_id
                    conn.execute(
                        """
                        UPDATE chunks SET embedding = embedding
                        WHERE document_id IN (SELECT id FROM documents WHERE hoa_id = ?)
                          AND embedding IS NOT NULL AND length(embedding) > 0
                        """,
                        (target_id,),
                    )
                    # Drop any leftover documents on the source (duplicate paths)
                    conn.execute("DELETE FROM documents WHERE hoa_id = ?", (source_id,))
                    # Move location iff target has none
                    has_target_loc = conn.execute(
                        "SELECT 1 FROM hoa_locations WHERE hoa_id = ?", (target_id,)
                    ).fetchone()
                    if has_target_loc is None:
                        conn.execute(
                            "UPDATE hoa_locations SET hoa_id = ? WHERE hoa_id = ?",
                            (target_id, source_id),
                        )
                    else:
                        conn.execute("DELETE FROM hoa_locations WHERE hoa_id = ?", (source_id,))
                    # Other FK-bearing tables: drop source-side rows so the
                    # final hoas DELETE doesn't get blocked.
                    for sql in (
                        "DELETE FROM membership_claims WHERE hoa_id = ?",
                        "DELETE FROM delegates WHERE hoa_id = ?",
                        "DELETE FROM proxy_assignments WHERE hoa_id = ?",
                        "DELETE FROM proxy_audit WHERE hoa_id = ?",
                        "DELETE FROM proposals WHERE hoa_id = ?",
                        "DELETE FROM meetings WHERE hoa_id = ?",
                    ):
                        try:
                            conn.execute(sql, (source_id,))
                        except sqlite3.OperationalError:
                            pass
                    conn.execute("DELETE FROM hoas WHERE id = ?", (source_id,))
                    results.append({
                        "hoa_id": source_id,
                        "status": "merged",
                        "old_name": old_name,
                        "new_name": new_name,
                        "target_id": target_id,
                    })
                    merged += 1
            except sqlite3.IntegrityError as exc:
                results.append({"hoa_id": source_id, "status": "error", "reason": str(exc)})
                errors += 1

        if not dry_run:
            conn.commit()

    return {
        "dry_run": dry_run,
        "renamed": renamed,
        "merged": merged,
        "noop": noop,
        "errors": errors,
        "results": results,
    }


@app.post("/admin/delete-hoa")
def admin_delete_hoa(request: Request, body: dict):
    """Hard-delete one or more HOAs and all their attached rows.

    Body: {"hoa_ids": [123, 456], "dry_run": false}

    Cascades: chunks (via document_id), documents, hoa_locations, and the
    proxy/membership tables — same set the rename-hoa merge path clears for
    its source row.
    """
    _require_admin(request)
    import sqlite3
    settings = load_settings()
    ids = body.get("hoa_ids") or ([body["hoa_id"]] if "hoa_id" in body else [])
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="hoa_ids[] required")
    try:
        ids = [int(x) for x in ids]
    except Exception:
        raise HTTPException(status_code=400, detail="hoa_ids must be integers")
    dry_run = bool(body.get("dry_run") or False)

    deleted: list[dict] = []
    errors = 0
    with db.get_connection(settings.db_path) as conn:
        for hid in ids:
            row = conn.execute("SELECT id, name FROM hoas WHERE id = ?", (hid,)).fetchone()
            if not row:
                deleted.append({"hoa_id": hid, "status": "not_found"})
                errors += 1
                continue
            doc_count = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE hoa_id = ?", (hid,)
            ).fetchone()[0]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE document_id IN (SELECT id FROM documents WHERE hoa_id = ?)",
                (hid,),
            ).fetchone()[0]
            if dry_run:
                deleted.append({
                    "hoa_id": hid, "name": row["name"], "status": "would_delete",
                    "doc_count": doc_count, "chunk_count": chunk_count,
                })
                continue
            try:
                conn.execute(
                    "DELETE FROM chunks WHERE document_id IN (SELECT id FROM documents WHERE hoa_id = ?)",
                    (hid,),
                )
                conn.execute("DELETE FROM documents WHERE hoa_id = ?", (hid,))
                conn.execute("DELETE FROM hoa_locations WHERE hoa_id = ?", (hid,))
                for sql in (
                    "DELETE FROM membership_claims WHERE hoa_id = ?",
                    "DELETE FROM delegates WHERE hoa_id = ?",
                    "DELETE FROM proxy_assignments WHERE hoa_id = ?",
                    "DELETE FROM proxy_audit WHERE hoa_id = ?",
                    "DELETE FROM proposals WHERE hoa_id = ?",
                    "DELETE FROM meetings WHERE hoa_id = ?",
                ):
                    try:
                        conn.execute(sql, (hid,))
                    except sqlite3.OperationalError:
                        pass
                conn.execute("DELETE FROM hoas WHERE id = ?", (hid,))
                deleted.append({
                    "hoa_id": hid, "name": row["name"], "status": "deleted",
                    "doc_count": doc_count, "chunk_count": chunk_count,
                })
            except sqlite3.Error as exc:
                deleted.append({"hoa_id": hid, "status": "error", "reason": str(exc)})
                errors += 1
        if not dry_run:
            conn.commit()
    return {
        "dry_run": dry_run,
        "deleted": sum(1 for d in deleted if d.get("status") == "deleted"),
        "would_delete": sum(1 for d in deleted if d.get("status") == "would_delete"),
        "errors": errors,
        "results": deleted,
    }


@app.post("/admin/clear-hoa-docs")
def admin_clear_hoa_docs(request: Request, body: dict):
    """Delete one or more HOAs' documents and chunks while preserving the
    entity row, its hoa_locations geometry, and any proxy/membership state.

    Use this when content audit flags a row's banked docs as junk but the
    entity itself is a real registered HOA worth keeping as a docless stub.
    Calling /admin/delete-hoa followed by /admin/create-stub-hoas was the
    previous workflow and silently lost lat/lon/boundary_geojson/street/
    postal_code/location_quality because the cascade clears hoa_locations
    and the stub recreate carries only name/state/city.

    Body: {"hoa_ids": [123, 456], "dry_run": false}

    Cascades: chunks (via document_id), documents. Does NOT touch hoas,
    hoa_locations, membership_claims, delegates, proxy_*, proposals,
    or meetings.

    Returns the same shape as /admin/delete-hoa, with a "cleared" count
    instead of "deleted".
    """
    _require_admin(request)
    import sqlite3
    settings = load_settings()
    ids = body.get("hoa_ids") or ([body["hoa_id"]] if "hoa_id" in body else [])
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="hoa_ids[] required")
    try:
        ids = [int(x) for x in ids]
    except Exception:
        raise HTTPException(status_code=400, detail="hoa_ids must be integers")
    dry_run = bool(body.get("dry_run") or False)

    results: list[dict] = []
    errors = 0
    with db.get_connection(settings.db_path) as conn:
        for hid in ids:
            row = conn.execute("SELECT id, name FROM hoas WHERE id = ?", (hid,)).fetchone()
            if not row:
                results.append({"hoa_id": hid, "status": "not_found"})
                errors += 1
                continue
            doc_count = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE hoa_id = ?", (hid,)
            ).fetchone()[0]
            chunk_count = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE document_id IN (SELECT id FROM documents WHERE hoa_id = ?)",
                (hid,),
            ).fetchone()[0]
            if dry_run:
                results.append({
                    "hoa_id": hid, "name": row["name"], "status": "would_clear",
                    "doc_count": doc_count, "chunk_count": chunk_count,
                })
                continue
            try:
                conn.execute(
                    "DELETE FROM chunks WHERE document_id IN (SELECT id FROM documents WHERE hoa_id = ?)",
                    (hid,),
                )
                conn.execute("DELETE FROM documents WHERE hoa_id = ?", (hid,))
                results.append({
                    "hoa_id": hid, "name": row["name"], "status": "cleared",
                    "doc_count": doc_count, "chunk_count": chunk_count,
                })
            except sqlite3.Error as exc:
                results.append({"hoa_id": hid, "status": "error", "reason": str(exc)})
                errors += 1
        if not dry_run:
            conn.commit()
    return {
        "dry_run": dry_run,
        "cleared": sum(1 for r in results if r.get("status") == "cleared"),
        "would_clear": sum(1 for r in results if r.get("status") == "would_clear"),
        "errors": errors,
        "results": results,
    }


@app.post("/admin/snapshot-hoa-docs-to-gcs")
def admin_snapshot_hoa_docs_to_gcs(request: Request, body: dict | None = None):
    """One-shot mirror of HOA_DOCS_ROOT to gs://hoaproxy-backups/hoa_docs/.

    Used during the Render → Hetzner migration to capture the binary doc tree
    that lives only on Render's persistent disk. The restore script on the new
    host downloads from the same prefix.

    Body (all optional):
      {
        "prefix": "",                 # only upload paths starting with this (relative to docs_root)
        "dry_run": false,
        "max_files": null,            # cap for testing; null = no cap
        "skip_existing": true         # skip blobs already present in GCS with the same size
      }

    Returns {uploaded, skipped, errors, total_bytes, elapsed_sec, sample}.
    Idempotent — safe to re-run; only new/changed files are uploaded.
    """
    _require_admin(request)
    import time as _time
    from pathlib import Path as _Path

    body = body or {}
    prefix = (body.get("prefix") or "").strip().lstrip("/")
    dry_run = bool(body.get("dry_run", False))
    max_files = body.get("max_files")
    skip_existing = bool(body.get("skip_existing", True))

    try:
        from google.cloud import storage as _gcs  # type: ignore
    except ImportError:
        raise HTTPException(status_code=500, detail="google-cloud-storage not installed")

    docs_root = _Path(os.environ.get("HOA_DOCS_ROOT", "hoa_docs")).resolve()
    if not docs_root.exists():
        raise HTTPException(status_code=500, detail=f"HOA_DOCS_ROOT does not exist: {docs_root}")

    bucket_name = os.environ.get("HOA_BACKUP_GCS_BUCKET", "hoaproxy-backups")
    client = _gcs.Client()
    bucket = client.bucket(bucket_name)

    started = _time.time()
    uploaded = 0
    skipped = 0
    errors = 0
    total_bytes = 0
    sample: List[dict] = []

    existing_sizes: dict[str, int] = {}
    if skip_existing:
        try:
            for blob in client.list_blobs(bucket, prefix=f"hoa_docs/{prefix}" if prefix else "hoa_docs/"):
                existing_sizes[blob.name] = blob.size or 0
        except Exception as exc:
            return {"error": f"failed to list existing blobs: {exc}"}

    walk_root = docs_root / prefix if prefix else docs_root
    if not walk_root.exists():
        return {"uploaded": 0, "skipped": 0, "errors": 0, "total_bytes": 0,
                "elapsed_sec": 0.0, "note": f"prefix {prefix!r} not present under docs_root"}

    for path in walk_root.rglob("*"):
        if not path.is_file():
            continue
        if max_files is not None and (uploaded + skipped) >= int(max_files):
            break

        try:
            rel = path.relative_to(docs_root).as_posix()
            blob_name = f"hoa_docs/{rel}"
            size = path.stat().st_size

            if skip_existing and existing_sizes.get(blob_name, -1) == size:
                skipped += 1
                continue

            if not dry_run:
                blob = bucket.blob(blob_name)
                blob.upload_from_filename(str(path))
            uploaded += 1
            total_bytes += size
            if len(sample) < 5:
                sample.append({"path": rel, "bytes": size})
        except Exception as exc:
            errors += 1
            logger.warning(f"snapshot upload failed for {path}: {exc}")

    return {
        "dry_run": dry_run,
        "bucket": bucket_name,
        "prefix": f"hoa_docs/{prefix}" if prefix else "hoa_docs/",
        "uploaded": uploaded,
        "skipped": skipped,
        "errors": errors,
        "total_bytes": total_bytes,
        "elapsed_sec": round(_time.time() - started, 1),
        "sample": sample,
    }


@app.post("/admin/create-stub-hoas")
def admin_create_stub_hoas(request: Request, body: dict):
    """Bulk-create or upsert HOAs *without* requiring documents. Used for
    registry-derived entities where governing docs aren't publicly available
    (e.g. DC condominium projects whose docs sit behind paywalled DC Recorder
    of Deeds, or HI condos behind Bureau of Conveyances).

    Each entry creates an `hoas` row (if name is new) and inserts/updates the
    matching `hoa_locations` row. No documents, chunks, or embeddings are
    created — these stub HOAs will appear in /hoas/summary with doc_count=0
    and chunk_count=0 until governing docs are uploaded later.

    Body shape:
      {
        "records": [
          {
            "name": "218 Vista Condo",
            "metadata_type": "condo",
            "street": "218 Vista St NE",
            "city": "Washington",
            "state": "DC",
            "postal_code": "20002",
            "latitude": 38.91,
            "longitude": -77.00,
            "location_quality": "address",
            "source": "dc-gis-cama-condo-regime",
            "source_url": "https://maps2.dcgis.dc.gov/.../FeatureServer/72",
            "website_url": null
          },
          ...
        ]
      }

    Allowed location_quality values match /admin/backfill-locations.
    Returns counts of created vs updated stubs.

    Cross-state collision policy is per-record via ``on_collision``:
      - ``"skip"`` (default): refuse the upsert and log a ``state_collision``
        skip. This is the bleed-stop guard added after the 2026-05-09 audit
        incident; safest when the caller cannot guarantee the input set is
        free of names that already live in another state.
      - ``"disambiguate"``: detect the collision and create a separate row
        under ``"{name} ({STATE})"``, with ``display_name`` set to the
        original ``name`` so the UI surfaces the clean form. Use this for
        bulk imports where the same legal name being registered in
        multiple states is expected (e.g. "Lakewood Estates HOA").
    """
    _require_admin(request)
    settings = load_settings()
    records = body.get("records") or []
    valid_quality = {"polygon", "address", "place_centroid", "zip_centroid", "city_only", "unknown"}
    valid_on_collision = {"skip", "disambiguate"}
    default_on_collision = (body.get("on_collision") or "skip").strip().lower()
    if default_on_collision not in valid_on_collision:
        raise HTTPException(
            status_code=400,
            detail=f"on_collision must be one of {sorted(valid_on_collision)}",
        )
    created = 0
    updated = 0
    disambiguated = 0
    skipped: list[dict] = []
    by_quality: dict[str, int] = {}

    with db.get_connection(settings.db_path) as conn:
        for entry in records:
            name = (entry.get("name") or "").strip()
            if not name:
                skipped.append({"reason": "empty_name", "entry": entry})
                continue
            quality = entry.get("location_quality")
            if quality is not None:
                quality = str(quality).strip()
                if quality not in valid_quality:
                    skipped.append({"reason": f"bad_quality:{quality}", "name": name})
                    continue
            entry_state = entry.get("state")
            entry_on_collision = (entry.get("on_collision") or default_on_collision).strip().lower()
            if entry_on_collision not in valid_on_collision:
                skipped.append({"reason": f"bad_on_collision:{entry_on_collision}", "name": name})
                continue

            canonical = name
            display_name = entry.get("display_name")
            existing = conn.execute(
                "SELECT id FROM hoas WHERE name = ?", (name,)
            ).fetchone()
            is_new = existing is None

            if not is_new and entry_state:
                cur_state = conn.execute(
                    "SELECT state FROM hoa_locations WHERE hoa_id = ?",
                    (existing["id"],),
                ).fetchone()
                cur_state_val = cur_state[0] if cur_state else None
                if cur_state_val and str(cur_state_val).upper() != str(entry_state).upper():
                    if entry_on_collision == "skip":
                        # Bleed-stop guard from the 2026-05-09 audit incident:
                        # cross-state name collisions silently corrupted state/
                        # city/location_quality on the colliding row. Refuse
                        # outright unless the caller opts into disambiguation.
                        skipped.append({
                            "reason": "state_collision",
                            "name": name,
                            "incoming_state": entry_state,
                            "existing_state": cur_state_val,
                        })
                        continue
                    # disambiguate: route this record to a separate row
                    # carrying "{name} ({STATE})" as its canonical name; keep
                    # display_name = the clean original.
                    disambiguated_target = f"{name} ({str(entry_state).upper()})"
                    pre_check = conn.execute(
                        "SELECT id FROM hoas WHERE name = ?", (disambiguated_target,)
                    ).fetchone()
                    _hid, canonical = db.get_or_create_hoa_state_aware(conn, name, entry_state)
                    if canonical != name:
                        disambiguated += 1
                        if display_name is None:
                            display_name = name
                        # `is_new` for accounting tracks the disambiguated
                        # row, not the colliding original.
                        is_new = pre_check is None

            db.upsert_hoa_location(
                conn,
                hoa_name=canonical,
                metadata_type=entry.get("metadata_type"),
                display_name=display_name,
                website_url=entry.get("website_url"),
                street=entry.get("street"),
                city=entry.get("city"),
                state=entry_state,
                postal_code=entry.get("postal_code"),
                latitude=entry.get("latitude"),
                longitude=entry.get("longitude"),
                source=entry.get("source") or "registry-stub",
                location_quality=quality,
            )
            if is_new:
                created += 1
            else:
                updated += 1
            by_quality[quality or "none"] = by_quality.get(quality or "none", 0) + 1

    return {
        "created": created,
        "updated": updated,
        "disambiguated": disambiguated,
        "skipped": len(skipped),
        "skipped_sample": skipped[:10],
        "by_quality": by_quality,
        "total_in_request": len(records),
        "on_collision_default": default_on_collision,
    }


@app.post("/admin/list-corruption-targets")
def admin_list_corruption_targets(request: Request, body: dict):
    """Read-only helper for the audit-corruption repair script.

    Returns hoa_locations rows whose ``source`` matches any of the supplied
    audit source strings, along with all the fields the repair logic needs to
    decide on a fix (state, city, postal_code, latitude, longitude,
    boundary_geojson, street, location_quality).

    Body: ``{"sources": ["tx-trec-...", ...], "require_lat": false}``

    ``require_lat=true`` restricts to rows with latitude IS NOT NULL — useful
    for Pass A's bbox-classify step (Pass B wants the opposite).
    """
    _require_admin(request)
    settings = load_settings()
    sources = body.get("sources") or []
    if not isinstance(sources, list) or not sources:
        raise HTTPException(status_code=400, detail="sources[] required")
    require_lat = bool(body.get("require_lat") or False)

    placeholders = ",".join("?" * len(sources))
    sql = f"""
        SELECT h.id AS hoa_id, h.name AS hoa,
               l.metadata_type, l.street, l.city, l.state, l.postal_code,
               l.latitude, l.longitude, l.boundary_geojson,
               l.source, l.location_quality
        FROM hoa_locations l
        JOIN hoas h ON h.id = l.hoa_id
        WHERE l.source IN ({placeholders})
    """
    if require_lat:
        sql += " AND l.latitude IS NOT NULL"

    rows: list[dict] = []
    with db.get_connection(settings.db_path) as conn:
        for r in conn.execute(sql, sources).fetchall():
            rows.append({
                "hoa_id": r["hoa_id"],
                "hoa": r["hoa"],
                "metadata_type": r["metadata_type"],
                "street": r["street"],
                "city": r["city"],
                "state": r["state"],
                "postal_code": r["postal_code"],
                "latitude": r["latitude"],
                "longitude": r["longitude"],
                "has_boundary": r["boundary_geojson"] is not None and r["boundary_geojson"] != "",
                "source": r["source"],
                "location_quality": r["location_quality"],
            })
    return {"count": len(rows), "rows": rows}


@app.post("/admin/extract-doc-zips")
def admin_extract_doc_zips(request: Request, state: str | None = None, limit: int = 5000):
    """Scan each HOA's chunked text for postal-code mentions and return the
    top 3 most-frequent ZIPs per HOA, filtered to ZIPs whose first digit is
    plausible for the HOA's recorded state. CC&Rs reliably mention the
    subdivision's actual ZIP many times (common-area address, recorded plat,
    metes-and-bounds endpoint); the property-manager's ZIP appears only once
    or twice. Caller geocodes the top ZIP locally via a ZCTA gazetteer.

    Response: [{hoa, recorded_city, recorded_state, top_zips: [{zip, count}]}, ...]
    """
    _require_admin(request)
    import re
    from collections import Counter

    # First digit by state — used as a sanity filter to drop ZIPs from other
    # states (e.g. an attorney's office in NY appearing in a CA HOA's CC&R).
    state_zip_prefix = {
        "CA": ("9",), "OR": ("9",), "WA": ("9",), "NV": ("8", "9"),
        "TX": ("7",), "OK": ("7",), "AR": ("7",), "LA": ("7",),
        "CO": ("8",), "NM": ("8", "7"), "UT": ("8",), "WY": ("8",), "MT": ("5", "8"), "ID": ("8",),
        "AZ": ("8",), "AK": ("9",), "HI": ("9",),
        "FL": ("3",), "GA": ("3",), "AL": ("3",), "TN": ("3", "4"),
        "NC": ("2",), "SC": ("2",), "VA": ("2",), "WV": ("2",), "MD": ("2",), "DC": ("2",),
        "DE": ("1",), "PA": ("1",), "NJ": ("0",), "NY": ("0", "1"), "CT": ("0",), "MA": ("0",), "RI": ("0",), "VT": ("0",), "NH": ("0",), "ME": ("0",),
        "OH": ("4",), "MI": ("4",), "IN": ("4",), "KY": ("4",),
        "IL": ("6",), "MO": ("6",), "KS": ("6",), "WI": ("5",), "MN": ("5",), "ND": ("5",), "SD": ("5",), "IA": ("5",), "NE": ("6",), "MS": ("3",),
    }

    zip_re = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
    settings = load_settings()
    out = []
    with db.get_connection(settings.db_path) as conn:
        # HOAs in the requested state with at least one document
        params: list = []
        where = "WHERE EXISTS (SELECT 1 FROM documents d WHERE d.hoa_id = h.id)"
        if state:
            where += " AND l.state = ?"
            params.append(state.upper())
        rows = conn.execute(
            f"""
            SELECT h.name AS hoa, l.city AS city, l.state AS state
            FROM hoas h
            LEFT JOIN hoa_locations l ON l.hoa_id = h.id
            {where}
            ORDER BY h.name COLLATE NOCASE
            LIMIT ?
            """,
            (*params, int(limit)),
        ).fetchall()

        for r in rows:
            hoa = str(r["hoa"])
            recorded_state = (r["state"] or state or "").upper() if (r["state"] or state) else None
            chunks = db.get_chunk_text_for_hoa(conn, hoa, limit=300)
            if not chunks:
                continue
            text = "\n".join(chunks)
            zips_found = zip_re.findall(text)
            if not zips_found:
                continue
            # Filter to ZIPs plausible for the state
            allowed = state_zip_prefix.get(recorded_state) if recorded_state else None
            if allowed:
                zips_found = [z for z in zips_found if z[0] in allowed]
            if not zips_found:
                continue
            counts = Counter(zips_found).most_common(3)
            out.append({
                "hoa": hoa,
                "recorded_city": r["city"],
                "recorded_state": recorded_state,
                "top_zips": [{"zip": z, "count": c} for z, c in counts],
            })
    return {"hoas": out, "total": len(out)}


# Tables holding user-created or user-action data. Everything else in the DB
# (catalog, embeddings, legal corpus) is rebuildable from the bank or by
# re-running ingest, so it's deliberately excluded from the backup.
_BACKUP_TABLES = (
    "users",
    "membership_claims",
    "delegates",
    "proxy_assignments",
    "proxy_audit",
    "proposals",
    "proposal_cosigners",
    "proposal_upvotes",
    "participation_records",
)


def _dump_tables_sql(conn, tables: tuple[str, ...]) -> str:
    # Emit a SQL text dump of just the named tables (schema + data + their
    # indexes). Restorable with `sqlite3 newdb.db < dump.sql`.
    out: list[str] = ["PRAGMA foreign_keys=OFF;", "BEGIN TRANSACTION;"]
    for table in tables:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not row:
            continue
        out.append(f"DROP TABLE IF EXISTS {table};")
        out.append(f"{row[0]};")
        for record in conn.execute(f"SELECT * FROM {table}"):
            vals = []
            for v in record:
                if v is None:
                    vals.append("NULL")
                elif isinstance(v, bool):
                    vals.append("1" if v else "0")
                elif isinstance(v, (int, float)):
                    vals.append(repr(v))
                elif isinstance(v, bytes):
                    vals.append("X'" + v.hex() + "'")
                else:
                    vals.append("'" + str(v).replace("'", "''") + "'")
            out.append(f"INSERT INTO {table} VALUES ({','.join(vals)});")
    placeholders = ",".join("?" * len(tables))
    for row in conn.execute(
        f"SELECT sql FROM sqlite_master "
        f"WHERE type IN ('index','trigger') "
        f"  AND tbl_name IN ({placeholders}) "
        f"  AND sql IS NOT NULL",
        tables,
    ).fetchall():
        out.append(f"{row[0]};")
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


@app.post("/admin/backup-hoa-tables")
def admin_backup_hoa_tables(request: Request):
    """Dump only ``hoas`` + ``hoa_locations`` as gzipped SQL to GCS.

    Sized for the rare-edit recovery path: ~100K rows, ~50 MB compressed,
    synchronous, completes in seconds. Use this immediately after any large
    manual edit to hoa_locations (audit cleanups, location backfills,
    manual fixes) so the live state is recoverable without a full DB
    snapshot.
    """
    _require_admin(request)
    import gzip
    import sqlite3 as _sqlite3
    from datetime import datetime, timezone
    from google.cloud import storage as gcs

    settings = load_settings()
    bucket_name = os.environ.get("BACKUP_GCS_BUCKET", "hoaproxy-backups")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log = logging.getLogger("admin.backup-hoa-tables")
    started = time.monotonic()

    src = _sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    try:
        sql_text = _dump_tables_sql(src, ("hoas", "hoa_locations"))
    finally:
        src.close()

    payload = gzip.compress(sql_text.encode("utf-8"))
    blob_name = f"db/hoa-tables-{stamp}.sql.gz"
    gcs.Client().bucket(bucket_name).blob(blob_name).upload_from_string(
        payload, content_type="application/gzip"
    )
    elapsed = time.monotonic() - started
    log.info(
        "backup-hoa-tables ok elapsed=%.1fs blob=%s sql_bytes=%d gz_bytes=%d",
        elapsed, blob_name, len(sql_text), len(payload),
    )
    return {
        "status": "ok",
        "uploaded": blob_name,
        "sql_bytes": len(sql_text),
        "gz_bytes": len(payload),
        "elapsed_sec": round(elapsed, 2),
    }


@app.post("/admin/backup-full")
def admin_backup_full(request: Request):
    """VACUUM INTO the full SQLite DB and upload to GCS in a detached child.

    Returns immediately with the planned blob name. Render's 5-minute HTTP
    timeout kills synchronous full-DB uploads (the live DB is ~1 GB), so the
    work runs in a separate process. We use Popen with start_new_session=True
    rather than a daemon thread because uvicorn worker recycles, graceful
    shutdowns on deploy, and OOM kills of the request worker would all silently
    take a daemon thread with them — leaving no log line and no blob.

    Output: ``gs://{BACKUP_GCS_BUCKET}/db/hoa_index-{stamp}.db``.
    Worker logs land at ``{db_dir}/_backup-{stamp}.log``; tail with
    GET /admin/backup-full-log?stamp=...

    Use /admin/backup for the precious-only user-state SQL dump (small,
    synchronous). This is the full binary snapshot for irrecoverable-edit
    recovery.
    """
    _require_admin(request)
    import shutil as _shutil
    import subprocess as _sp
    import sys as _sys
    from datetime import datetime, timezone

    settings = load_settings()
    bucket_name = os.environ.get("BACKUP_GCS_BUCKET", "hoaproxy-backups")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    blob_name = f"db/hoa_index-{stamp}.db"

    # VACUUM INTO needs a fresh file path; land it next to the live DB so we
    # use the persistent disk's free space, then delete after upload.
    src_dir = os.path.dirname(settings.db_path) or "."
    snapshot_path = os.path.join(src_dir, f"_backup-{stamp}.db")
    log_path = os.path.join(src_dir, f"_backup-{stamp}.log")

    helper = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "scripts", "backup_full_worker.py")
    )

    # OS-level priority: nice (CPU) + ionice -c 3 (I/O idle class). Idle-class
    # I/O only runs when no other process needs the disk, so the backup yields
    # to API requests instead of competing with them — solves the "VACUUM
    # saturates the disk and Render docker-stops the container" failure mode
    # that killed the daemon-thread and threaded-backup attempts. Both tools
    # are skipped if not in PATH (e.g. local dev on macOS).
    cmd: list[str] = []
    nice_path = _shutil.which("nice")
    ionice_path = _shutil.which("ionice")
    if nice_path:
        cmd.extend([nice_path, "-n", "19"])
    if ionice_path:
        cmd.extend([ionice_path, "-c", "3"])
    cmd.extend([_sys.executable, helper, settings.db_path, bucket_name, blob_name, snapshot_path])

    # Open the log file in the parent and pass the fd to the child. The child
    # gets its own dup of the fd, so closing it here doesn't affect the worker.
    log_fh = open(log_path, "wb")
    try:
        proc = _sp.Popen(
            cmd,
            stdout=log_fh,
            stderr=_sp.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_fh.close()

    return {
        "status": "started",
        "uploaded": blob_name,
        "snapshot_path": snapshot_path,
        "log_path": log_path,
        "stamp": stamp,
        "pid": proc.pid,
        "note": "running in detached child process; poll gs://"
                f"{bucket_name}/{blob_name} for completion, or "
                f"GET /admin/backup-full-log?stamp={stamp}",
    }


@app.get("/admin/backup-full-log")
def admin_backup_full_log(request: Request, stamp: str):
    """Return the tail of a /admin/backup-full worker's log.

    Used to confirm the detached subprocess actually ran VACUUM + upload, and
    to surface its traceback if the GCS blob never landed.
    """
    _require_admin(request)
    settings = load_settings()
    src_dir = os.path.dirname(settings.db_path) or "."
    # Defend against path traversal: only accept the YYYYMMDD-HHMMSS shape.
    import re as _re
    if not _re.fullmatch(r"\d{8}-\d{6}", stamp):
        raise HTTPException(status_code=400, detail="invalid stamp")
    log_path = os.path.join(src_dir, f"_backup-{stamp}.log")
    if not os.path.exists(log_path):
        return {"exists": False, "log_path": log_path}
    size = os.path.getsize(log_path)
    with open(log_path, "rb") as f:
        if size > 32_000:
            f.seek(size - 32_000)
        body = f.read()
    return {
        "exists": True,
        "log_path": log_path,
        "size": size,
        "tail": body.decode("utf-8", errors="replace"),
    }


@app.post("/admin/cleanup-backup-orphans")
def admin_cleanup_backup_orphans(request: Request, min_age_minutes: int = 30, dry_run: bool = False):
    """Delete leftover ``_backup-*.db``, ``-journal``, and ``.log`` files in
    the DB directory whose mtime is older than ``min_age_minutes`` (default
    30). These accumulate when /admin/backup-full's worker is killed mid-VACUUM
    or mid-upload — the daemon-thread implementation produced 51 GiB of dead
    snapshots on 2026-05-10 alone before being switched to detached subprocess.

    The age guard is the only safety against deleting an in-flight backup. Set
    it generously when triggering manually; the detached worker now self-cleans
    its own snapshot in its ``finally`` block, so future orphans should only
    appear when the worker itself is SIGKILL-ed.

    Pass ``dry_run=true`` to preview without deleting.
    """
    _require_admin(request)
    import re as _re
    import time as _time
    settings = load_settings()
    src_dir = Path(os.path.dirname(settings.db_path) or ".")

    pattern = _re.compile(r"^_backup-\d{8}-\d{6}\.(db|db-journal|log)$")
    cutoff = _time.time() - int(min_age_minutes) * 60

    candidates = []
    for entry in src_dir.iterdir():
        if not entry.is_file():
            continue
        if not pattern.match(entry.name):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_mtime > cutoff:
            continue
        candidates.append({
            "path": str(entry),
            "bytes": stat.st_size,
            "age_minutes": round((_time.time() - stat.st_mtime) / 60, 1),
        })

    deleted = []
    skipped: list[dict] = []
    bytes_freed = 0
    for c in candidates:
        if dry_run:
            skipped.append({**c, "reason": "dry_run"})
            continue
        try:
            os.remove(c["path"])
            deleted.append(c)
            bytes_freed += c["bytes"]
        except OSError as e:
            skipped.append({**c, "reason": f"unlink_failed: {e}"})

    return {
        "dry_run": dry_run,
        "min_age_minutes": min_age_minutes,
        "scanned_dir": str(src_dir),
        "deleted_count": len(deleted),
        "bytes_freed": bytes_freed,
        "deleted": deleted,
        "skipped": skipped,
    }


@app.post("/admin/backup")
def admin_backup(request: Request):
    """Snapshot the user-created tables to GCS as a gzipped SQL dump.

    Deliberately excludes catalog/embedding/legal tables and uploaded PDFs:
    those are either in the bank or rebuildable from ingest. The dump is
    typically a few MB and finishes in seconds, so this is synchronous.
    """
    _require_admin(request)
    import gzip
    import sqlite3 as _sqlite3
    from datetime import datetime, timezone
    from google.cloud import storage as gcs

    settings = load_settings()
    bucket_name = os.environ.get("BACKUP_GCS_BUCKET", "hoaproxy-backups")
    max_backups = int(os.environ.get("BACKUP_MAX_COPIES", "30"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log = logging.getLogger("admin.backup")
    started = time.monotonic()

    src_uri = f"file:{settings.db_path}?mode=ro"
    src = _sqlite3.connect(src_uri, uri=True)
    try:
        sql_text = _dump_tables_sql(src, _BACKUP_TABLES)
    finally:
        src.close()

    payload = gzip.compress(sql_text.encode("utf-8"))
    blob_name = f"db/precious-{stamp}.sql.gz"
    client = gcs.Client()
    gcs_bucket = client.bucket(bucket_name)
    gcs_bucket.blob(blob_name).upload_from_string(
        payload, content_type="application/gzip"
    )

    pruned = 0
    try:
        blobs = sorted(
            gcs_bucket.list_blobs(prefix="db/precious-"),
            key=lambda b: b.name,
            reverse=True,
        )
        for old in blobs[max_backups:]:
            old.delete()
            pruned += 1
    except Exception:
        log.exception("backup retention failed")

    elapsed = time.monotonic() - started
    log.info(
        "backup ok elapsed=%.1fs blob=%s sql_bytes=%d gz_bytes=%d pruned=%d",
        elapsed, blob_name, len(sql_text), len(payload), pruned,
    )
    return {
        "status": "ok",
        "uploaded": blob_name,
        "sql_bytes": len(sql_text),
        "gz_bytes": len(payload),
        "elapsed_sec": round(elapsed, 2),
        "tables": list(_BACKUP_TABLES),
        "retention": f"keeping last {max_backups}",
    }


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

def _enforce_tos_acceptance(request: Request, body_version: str | None) -> None:
    """Block registration unless the request body matches the current TOS_VERSION.

    Matches the _check_rate_limit convention: TestClient (`request.client.host
    == "testclient"`) bypasses the check so existing fixture-style auth tests
    don't all need to pass an extra field. Real HTTP clients can't easily
    forge the connection's reported host; this is a test-only bypass, not a
    public escape hatch.
    """
    if request.client and request.client.host == "testclient":
        return
    if body_version != TOS_VERSION:
        raise HTTPException(
            status_code=400,
            detail=f"You must agree to the current Terms of Service (version {TOS_VERSION}).",
        )


@app.post("/auth/register", response_model=AuthResponse)
def register(request: Request, body: RegisterRequest, background_tasks: BackgroundTasks):
    _check_rate_limit(request, limit=10)
    _enforce_tos_acceptance(request, body.accepted_terms_version)
    import secrets as _secrets
    from datetime import timedelta
    from hoaware.email_service import send_verification_email
    settings = load_settings()
    display_name = body.display_name.strip() if body.display_name else None
    accepted_at = datetime.now(timezone.utc).isoformat()
    with db.get_connection(settings.db_path) as conn:
        existing = db.get_user_by_email(conn, body.email)
        if existing:
            raise HTTPException(status_code=409, detail="Email already registered")
        pw_hash = hash_password(body.password)
        user_id = db.create_user(
            conn,
            email=body.email,
            password_hash=pw_hash,
            display_name=display_name,
            terms_version_accepted=body.accepted_terms_version,
            terms_accepted_at=accepted_at,
        )
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
def list_hoas(
    request: Request,
    limit: int | None = None,
    offset: int = 0,
) -> list[str]:
    """Return HOA names that have at least one document.

    Optional ?limit and ?offset paginate the result. With no params the full
    list is returned (back-compat). Rate-limited to discourage bulk scraping.
    """
    _check_rate_limit(request, limit=10)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        names = db.list_hoa_names_with_documents(conn)
    if limit is not None:
        if limit < 1 or limit > 1000:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 1000")
        if offset < 0:
            raise HTTPException(status_code=400, detail="offset must be >= 0")
        names = names[offset : offset + limit]
    return names


@app.get("/hoas/summary", response_model=HoaSummaryPage)
def list_hoa_summary(
    request: Request,
    q: str | None = None,
    state: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> HoaSummaryPage:
    _check_rate_limit(request, limit=30)
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
def list_hoa_states(request: Request) -> List[HoaStateCount]:
    _check_rate_limit(request, limit=30)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        return [HoaStateCount(**row) for row in db.list_hoa_states(conn)]


@app.get("/hoas/map-points", response_model=List[HoaMapPoint])
def list_hoa_map_points(
    request: Request,
    q: str | None = None,
    state: str | None = None,
) -> List[HoaMapPoint]:
    """Lightweight endpoint returning only lat/lng/state/doc_count for map markers."""
    _check_rate_limit(request, limit=15)
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
def list_hoa_locations(request: Request) -> List[HoaLocation]:
    _check_rate_limit(request, limit=10)
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
    location_quality: str | None = Form(default=None),
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
    derived_quality = _derive_location_quality(
        has_boundary=bool(normalized_boundary),
        street=street,
        postal_code=postal_code,
    ) if (latitude is not None and longitude is not None) else None
    final_quality = (location_quality.strip() if location_quality else None) or derived_quality
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
            location_quality=final_quality,
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
def open_document_file(request: Request, hoa_name: str, path: str) -> FileResponse:
    # Scrape protection (docs/scrape-protection.md): cap raw PDF downloads
    # at 60/hour/IP. Legitimate users click a few docs per session; this
    # only bites at scraper-grade volumes.
    _check_rate_limit(request, limit=60)
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
    # Ownership check: the document must be attached to the resolved HOA in the
    # DB. Pure string-prefix matching breaks after rename, because rename only
    # updates hoas.name (not documents.relative_path or the on-disk dir).
    with db.get_connection(settings.db_path) as conn:
        owns = conn.execute(
            "SELECT 1 FROM documents d JOIN hoas h ON d.hoa_id = h.id "
            "WHERE h.name = ? AND d.relative_path = ? LIMIT 1",
            (resolved_hoa, rel_doc),
        ).fetchone()
    if owns is None:
        raise HTTPException(status_code=400, detail="Document does not belong to requested HOA")
    # Cache hint for Cloudflare (see docs/scrape-protection.md). PDFs are
    # content-addressed by checksum so they never change for a given URL;
    # 1-day TTL keeps load off Render without staleness.
    headers = {"Cache-Control": "public, max-age=86400, immutable"}
    return FileResponse(
        doc_path,
        media_type="application/pdf",
        filename=doc_path.name,
        headers=headers,
    )


@app.get("/hoas/{hoa_name}/documents/searchable", response_class=HTMLResponse)
def open_document_searchable(hoa_name: str, path: str) -> HTMLResponse:
    settings = load_settings()
    resolved_hoa = _resolve_hoa_name(hoa_name)
    rel_doc = _safe_relative_document_path(path)
    with db.get_connection(settings.db_path) as conn:
        owns = conn.execute(
            "SELECT 1 FROM documents d JOIN hoas h ON d.hoa_id = h.id "
            "WHERE h.name = ? AND d.relative_path = ? LIMIT 1",
            (resolved_hoa, rel_doc),
        ).fetchone()
    if owns is None:
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
    extracted_texts: List[str] | None = Form(default=None),
    user: dict = Depends(get_current_user),
) -> UploadResponse:
    settings = load_settings()
    resolved_hoa = _resolve_hoa_name(hoa)
    if not settings.openai_api_key:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is required for ingestion")
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required")
    _check_disk_free(settings.docs_root)
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
    sidecars = _parse_extracted_text_sidecars(len(files), extracted_texts)

    settings.docs_root.mkdir(parents=True, exist_ok=True)
    hoa_dir = settings.docs_root / resolved_hoa
    hoa_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[Path] = []
    saved_files: list[str] = []
    metadata_by_path: dict[Path, dict] = {}
    for upload, meta, sidecar in zip(files, per_file_meta, sidecars):
        filename = _safe_pdf_filename(upload.filename)
        target = hoa_dir / filename
        with target.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        saved_paths.append(target)
        saved_files.append(filename)
        if sidecar is not None:
            meta = {**meta, "pre_extracted_pages": sidecar["pages"]}
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
        upload_quality = _derive_location_quality(
            has_boundary=bool(normalized_boundary),
            street=street,
            postal_code=postal_code,
        ) if (latitude is not None and longitude is not None) else None
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
                location_quality=upload_quality,
            )
        location_saved = True

    # Log DocAI pages the agent OCR'd locally so the rolling 24h tracker
    # reflects total spend (local + server). Done before the cap check so
    # the projection includes them.
    for path, sidecar in zip(saved_paths, sidecars):
        if sidecar and sidecar.get("docai_pages"):
            log_docai_usage(
                int(sidecar["docai_pages"]),
                document=path.relative_to(settings.docs_root).as_posix(),
            )

    # Daily DocAI budget guard: count pages the SERVER will OCR (files the
    # agent flagged text_extractable=False that did NOT come with a sidecar).
    projected_ocr_pages = _projected_docai_pages(saved_paths, metadata_by_path)
    _check_daily_docai_budget(projected_ocr_pages)

    # Phase 2 async path — enqueue, return 202-shape, let the worker drain.
    if _async_ingest_enabled():
        job_id, sidecar_uri = _persist_upload_sidecar(
            resolved_hoa, saved_paths, metadata_by_path, docs_root=settings.docs_root
        )
        with db.get_connection(settings.db_path) as conn:
            db.enqueue_pending_ingest(
                conn,
                job_id=job_id,
                bundle_uri=sidecar_uri,
                state=(state or "").strip().upper() or "??",
                source="upload",
            )
        return UploadResponse(
            hoa=resolved_hoa,
            saved_files=saved_files,
            indexed=0,
            skipped=0,
            failed=0,
            queued=True,
            location_saved=location_saved,
            job_id=job_id,
            status_url=f"/ingest/status/{job_id}",
        )

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
    extracted_texts: List[str] | None = Form(default=None),
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
    _check_disk_free(settings.docs_root)
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
    sidecars = _parse_extracted_text_sidecars(len(files), extracted_texts)

    settings.docs_root.mkdir(parents=True, exist_ok=True)
    hoa_dir = settings.docs_root / resolved_hoa
    hoa_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[Path] = []
    saved_files: list[str] = []
    metadata_by_path: dict[Path, dict] = {}
    for upload, meta, sidecar in zip(files, per_file_meta, sidecars):
        filename = _safe_pdf_filename(upload.filename)
        target = hoa_dir / filename
        with target.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        saved_paths.append(target)
        saved_files.append(filename)
        if sidecar is not None:
            meta = {**meta, "pre_extracted_pages": sidecar["pages"]}
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
        anon_quality = _derive_location_quality(
            has_boundary=bool(normalized_boundary),
            street=street,
            postal_code=postal_code,
        ) if (latitude is not None and longitude is not None) else None
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
                location_quality=anon_quality,
            )
        location_saved = True

    logger.info("anonymous_upload hoa=%s email=%s files=%d ip=%s",
                resolved_hoa, email, len(saved_files),
                request.client.host if request.client else "unknown")

    for path, sidecar in zip(saved_paths, sidecars):
        if sidecar and sidecar.get("docai_pages"):
            log_docai_usage(
                int(sidecar["docai_pages"]),
                document=path.relative_to(settings.docs_root).as_posix(),
            )

    projected_ocr_pages = _projected_docai_pages(saved_paths, metadata_by_path)
    _check_daily_docai_budget(projected_ocr_pages)

    if _async_ingest_enabled():
        job_id, sidecar_uri = _persist_upload_sidecar(
            resolved_hoa, saved_paths, metadata_by_path, docs_root=settings.docs_root
        )
        with db.get_connection(settings.db_path) as conn:
            db.enqueue_pending_ingest(
                conn,
                job_id=job_id,
                bundle_uri=sidecar_uri,
                state=(state or "").strip().upper() or "??",
                source="upload-anonymous",
            )
        return UploadResponse(
            hoa=resolved_hoa,
            saved_files=saved_files,
            indexed=0,
            skipped=0,
            failed=0,
            queued=True,
            location_saved=location_saved,
            job_id=job_id,
            status_url=f"/ingest/status/{job_id}",
        )

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
    full_text = ""

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

    if (
        not suggested_category
        and full_text.strip()
        and os.environ.get("HOA_ENABLE_LLM_CLASSIFIER", "0") in {"1", "true", "True"}
    ):
        try:
            clf = classify_with_llm(
                full_text,
                body.hoa or "",
                source_url=body.url or "",
                filename=body.filename or "",
            )
            if clf:
                suggested_category = clf["category"]
                model_note = f" model={clf.get('model')}" if clf.get("model") else ""
                notes.append(f"category via llm (conf={clf['confidence']:.2f}{model_note})")
        except Exception as exc:
            notes.append(f"llm classifier failed: {type(exc).__name__}")

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
def search(request: Request, body: SearchRequest) -> SearchResponse:
    _check_rate_limit(request, limit=30)
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
def search_multi(request: Request, body: MultiSearchRequest) -> MultiSearchResponse:
    _check_rate_limit(request, limit=15)
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
def qa(request: Request, body: QARequest) -> QAResponse:
    _check_rate_limit(request, limit=20)
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
    except QATemporaryError as exc:
        raise HTTPException(
            status_code=503,
            detail="Q&A provider is temporarily unavailable. Please try again shortly.",
        ) from exc
    except QAProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not results:
        raise HTTPException(status_code=404, detail="No context found for query.")
    return QAResponse(answer=answer, sources=citations)


@app.post("/qa/multi", response_model=QAResponse)
def qa_multi(request: Request, body: MultiQARequest) -> QAResponse:
    _check_rate_limit(request, limit=10)
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
    except QATemporaryError as exc:
        raise HTTPException(
            status_code=503,
            detail="Q&A provider is temporarily unavailable. Please try again shortly.",
        ) from exc
    except QAProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
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
def law_qa(request: Request, body: LawQARequest) -> LawQAResponse:
    _check_rate_limit(request, limit=20)
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
