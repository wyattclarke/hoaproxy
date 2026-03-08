from __future__ import annotations

from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape as html_escape
import json
import logging
import math
import re
import shutil
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

import requests
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hoaware import db
from hoaware.auth import get_current_user, hash_password, verify_password, create_access_token, optional_current_user
from hoaware.config import load_settings
from hoaware.ingest import ingest_pdf_paths
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


# ---------------------------------------------------------------------------
# Startup: ensure all DB tables exist (safe to run on existing DB)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
    _run_expiry_sweep()
    yield


app = FastAPI(title="HOA QA API", version="0.2.0", lifespan=lifespan)
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
_NEAR_BOUNDARY_M = 250.0
_NEAR_POINT_M = 1609.0
STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger(__name__)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class QARequest(BaseModel):
    hoa: str
    question: str
    k: int = Field(default=6, ge=1, le=20)
    model: str = "gpt-5-mini"


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
    model: str = "gpt-5-mini"


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
    indexed: int
    skipped: int
    failed: int
    queued: bool = False
    location_saved: bool = False


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
    hoa: str
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
    direction: str = "directed"
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
    signing_url: str | None = None
    signed_at: str | None = None
    delivered_at: str | None = None
    revoked_at: str | None = None
    revoke_reason: str | None = None
    created_at: str | None = None


class ProxyStatsResponse(BaseModel):
    total: int
    signed: int
    delivered: int


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


def _ingest_uploaded_files(hoa_name: str, saved_paths: list[Path]) -> None:
    settings = load_settings()
    try:
        stats = ingest_pdf_paths(hoa_name, saved_paths, settings=settings, show_progress=False)
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
            headers={"User-Agent": "hoaware/0.2 (local-ui-location)"},
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
            params={"q": cleaned, "format": "json", "limit": 1},
            headers={"User-Agent": "hoaware/0.2 (local-ui-location)"},
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
        points.append((lon, lat))
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
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    if abs(cross) > eps:
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    if dot < -eps:
        return False
    sq_len = (bx - ax) ** 2 + (by - ay) ** 2
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


def _suggestions_for_point(
    point_lat: float,
    point_lon: float,
    rows: list[dict],
    max_suggestions: int,
) -> list[dict]:
    suggestions: list[dict] = []
    for row in rows:
        hoa_name = str(row.get("hoa") or "").strip()
        if not hoa_name:
            continue
        boundary = row.get("boundary_geojson")
        polygons = _extract_geojson_polygons(boundary) if boundary else []
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
                }
            )
            continue

        if polygons:
            boundary_distance = _distance_to_geojson_boundary_m(point_lon, point_lat, boundary)
            if boundary_distance is not None and boundary_distance <= _NEAR_BOUNDARY_M:
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
                    }
                )
                continue

        lat = row.get("latitude")
        lon = row.get("longitude")
        if lat is None or lon is None:
            continue
        try:
            point_distance = _haversine_m(point_lat, point_lon, float(lat), float(lon))
        except Exception:
            continue
        if point_distance <= _NEAR_POINT_M:
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
                }
            )

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
    for item in suggestions[:max_suggestions]:
        cleaned.append(
            {
                "hoa": str(item["hoa"]),
                "match_type": str(item["match_type"]),
                "confidence": str(item["confidence"]),
                "default_selected": bool(item["default_selected"]),
                "distance_m": float(item["distance_m"]),
                "reason": str(item["reason"]),
            }
        )
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


@app.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    return _serve_static_page("login.html")


@app.get("/register", include_in_schema=False)
def register_page() -> FileResponse:
    return _serve_static_page("register.html")


@app.get("/dashboard", include_in_schema=False)
def dashboard_page() -> FileResponse:
    return _serve_static_page("dashboard.html")


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


@app.get("/participation/{hoa_name:path}", include_in_schema=False)
def participation_page(hoa_name: str) -> FileResponse:
    return _serve_static_page("participation.html")


@app.get("/hoa/{hoa_name:path}", include_in_schema=False)
def hoa_profile_page(hoa_name: str) -> FileResponse:
    if not STATIC_DIR.exists():
        raise HTTPException(status_code=404, detail="UI not available")
    page = STATIC_DIR / "hoa.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="HOA profile page not available")
    return FileResponse(page)


@app.get("/healthz")
def health() -> dict:
    settings = load_settings()
    required_tables = {"hoas", "users", "sessions", "membership_claims", "delegates",
                       "proxy_assignments", "proxy_audit"}
    try:
        with db.get_connection(settings.db_path) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            existing = {r["name"] for r in rows}
            missing = required_tables - existing
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}")
    if missing:
        raise HTTPException(status_code=503, detail=f"Missing tables: {missing}")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

@app.post("/auth/register", response_model=AuthResponse)
def register(request: Request, body: RegisterRequest):
    _check_rate_limit(request, limit=10)
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
    return AuthResponse(user_id=user_id, token=token)


@app.post("/auth/login", response_model=AuthResponse)
def login(request: Request, body: LoginRequest):
    _check_rate_limit(request, limit=10)
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        user = db.get_user_by_email(conn, body.email)
        if not user or not verify_password(body.password, user["password_hash"]):
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


@app.get("/auth/me", response_model=UserMeResponse)
def me(user: dict = Depends(get_current_user)):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        claims = db.list_membership_claims_for_user(conn, user["id"])
    return UserMeResponse(
        user_id=user["id"],
        email=user["email"],
        display_name=user.get("display_name"),
        hoas=[{"hoa_id": c["hoa_id"], "hoa_name": c["hoa_name"], "unit_number": c["unit_number"], "status": c["status"]} for c in claims],
    )


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
        meeting_date="2026-04-15",
        direction="directed",
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
        community_type=p["community_type"], direction=p.get("direction", "directed"),
        voting_instructions=p.get("voting_instructions"), for_meeting_date=p.get("for_meeting_date"),
        expires_at=p.get("expires_at"), status=p["status"],
        signing_url=p.get("documenso_signing_url"),
        signed_at=p.get("signed_at"),
        delivered_at=p.get("delivered_at"), revoked_at=p.get("revoked_at"),
        revoke_reason=p.get("revoke_reason"), created_at=p.get("created_at"),
    )


@app.post("/proxies", response_model=ProxyResponse)
def create_proxy(body: CreateProxyRequest, request: Request, user: dict = Depends(get_current_user)):
    _check_rate_limit(request, limit=20)
    from hoaware.proxy_templates import render_proxy_form
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        # Verify grantor is a member of the HOA
        claim = db.get_membership_claim(conn, user["id"], body.hoa_id)
        if not claim:
            raise HTTPException(status_code=403, detail="You must be a member of this HOA to create a proxy")
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

        # Render form
        grantor = db.get_user_by_id(conn, user["id"])
        delegate_user = db.get_user_by_id(conn, body.delegate_user_id)
        form_html = render_proxy_form(
            jurisdiction=jurisdiction,
            community_type="hoa",
            grantor_name=grantor.get("display_name") or grantor["email"],
            grantor_unit=claim.get("unit_number"),
            delegate_name=delegate_user.get("display_name") or delegate_user["email"],
            hoa_name=hoa_name,
            meeting_date=body.for_meeting_date,
            direction=body.direction,
        )

        proxy_id = db.create_proxy_assignment(
            conn,
            grantor_user_id=user["id"],
            delegate_user_id=body.delegate_user_id,
            hoa_id=body.hoa_id,
            jurisdiction=jurisdiction,
            community_type="hoa",
            direction=body.direction,
            voting_instructions=body.voting_instructions,
            for_meeting_date=body.for_meeting_date,
            form_html=form_html,
        )
        db.create_proxy_audit(
            conn, proxy_id=proxy_id, action="created", actor_user_id=user["id"],
            details={"direction": body.direction},
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
    """Initiate signing for a proxy.

    If Documenso is configured: creates a Documenso document, stores the signing URL,
    and returns the proxy with status='draft' and signing_url set. The status will
    move to 'signed' when Documenso calls the webhook.

    If Documenso is not configured: immediately records the click-to-sign and
    returns the proxy with status='signed'.
    """
    from hoaware.esign import create_signing_request, record_signature
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

    if settings.documenso_api_key:
        # Documenso path: create document, store signing URL, redirect user
        try:
            result = create_signing_request(
                proxy_id=proxy_id,
                form_html=proxy.get("form_html") or "",
                grantor_email=proxy["grantor_email"],
                grantor_name=proxy.get("grantor_name") or proxy["grantor_email"],
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Documenso error: {exc}") from exc

        with db.get_connection(settings.db_path) as conn:
            db.update_proxy_status(
                conn, proxy_id, "draft",
                documenso_document_id=result.get("document_id"),
                documenso_signing_url=result.get("signing_url"),
            )
            db.create_proxy_audit(
                conn, proxy_id=proxy_id, action="signing_initiated",
                actor_user_id=user["id"],
                details={"method": "documenso", "document_id": result.get("document_id")},
            )
            proxy = db.get_proxy_assignment(conn, proxy_id)
    else:
        # Click-to-sign fallback
        success = record_signature(proxy_id, user["id"], ip_address=ip, user_agent=ua)
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


# ---------------------------------------------------------------------------
# Documenso webhook (M6)
# ---------------------------------------------------------------------------

@app.post("/webhooks/documenso", include_in_schema=False)
async def documenso_webhook(request: Request):
    """Receive Documenso signing completion events.

    Expected event payload:
      {"event": "document.completed", "data": {"externalId": "<proxy_id>", ...}}

    Verifies HMAC-SHA256 signature from X-Documenso-Signature header.
    """
    from hoaware.esign import verify_webhook_signature, download_signed_pdf
    from hoaware.email_service import notify_grantor, notify_delegate

    body_bytes = await request.body()
    sig_header = request.headers.get("X-Documenso-Signature")

    if not verify_webhook_signature(body_bytes, sig_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event = payload.get("event") or payload.get("type")
    data = payload.get("data") or {}

    if event not in ("document.completed", "DOCUMENT_COMPLETED"):
        # Acknowledge other events without action
        return {"ok": True, "event": event, "action": "ignored"}

    external_id = data.get("externalId") or data.get("external_id")
    if not external_id:
        raise HTTPException(status_code=400, detail="Missing externalId in webhook payload")

    try:
        proxy_id = int(external_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid externalId: {external_id!r}")

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        proxy = db.get_proxy_assignment(conn, proxy_id)
        if not proxy:
            raise HTTPException(status_code=404, detail=f"Proxy {proxy_id} not found")
        if proxy["status"] != "draft":
            # Already processed (idempotent)
            return {"ok": True, "proxy_id": proxy_id, "action": "already_processed"}

        now = datetime.now(timezone.utc).isoformat()
        db.update_proxy_status(conn, proxy_id, "signed", signed_at=now)
        db.create_proxy_audit(
            conn, proxy_id=proxy_id, action="signed", actor_user_id=None,
            details={"method": "documenso_webhook", "event": event, "timestamp": now},
        )

    # Optionally save the signed PDF
    doc_id = proxy.get("documenso_document_id")
    if doc_id:
        pdf_bytes = download_signed_pdf(doc_id)
        if pdf_bytes:
            import hashlib, pathlib
            pdf_dir = pathlib.Path("data/signed_pdfs")
            pdf_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = pdf_dir / f"proxy_{proxy_id}.pdf"
            pdf_path.write_bytes(pdf_bytes)
            with db.get_connection(settings.db_path) as conn:
                db.update_proxy_status(conn, proxy_id, "signed", signed_pdf_path=str(pdf_path))

    notify_grantor(proxy_id, "signed")
    return {"ok": True, "proxy_id": proxy_id, "action": "signed"}


@app.get("/hoas")
def list_hoas() -> list[str]:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        return db.list_hoa_names_with_documents(conn)


@app.get("/hoas/summary", response_model=List[HoaSummary])
def list_hoa_summary() -> List[HoaSummary]:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        rows = db.list_hoa_summaries(conn)
    return [HoaSummary(**row) for row in rows]


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
    normalized_website = _normalize_website_url(website_url)
    normalized_boundary = _parse_boundary_geojson(boundary_geojson)
    if latitude is not None and not (-90 <= latitude <= 90):
        raise HTTPException(status_code=400, detail="latitude must be between -90 and 90")
    if longitude is not None and not (-180 <= longitude <= 180):
        raise HTTPException(status_code=400, detail="longitude must be between -180 and 180")
    if (latitude is None or longitude is None) and any([street, city, state, postal_code]):
        coords = _geocode_from_parts(
            street=(street.strip() if street else None),
            city=(city.strip() if city else None),
            state=(state.strip().upper() if state else None),
            postal_code=(postal_code.strip() if postal_code else None),
        )
        if coords:
            latitude, longitude = coords
    if (latitude is None or longitude is None) and normalized_boundary:
        center = _center_from_boundary_geojson(normalized_boundary)
        if center:
            latitude, longitude = center
    with db.get_connection(settings.db_path) as conn:
        db.upsert_hoa_location(
            conn,
            resolved_hoa,
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
    website_url: str | None = Form(default=None),
    street: str | None = Form(default=None),
    city: str | None = Form(default=None),
    state: str | None = Form(default=None),
    postal_code: str | None = Form(default=None),
    country: str | None = Form(default=None),
    latitude: float | None = Form(default=None),
    longitude: float | None = Form(default=None),
    boundary_geojson: str | None = Form(default=None),
) -> UploadResponse:
    settings = load_settings()
    resolved_hoa = _resolve_hoa_name(hoa)
    if not settings.openai_api_key:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is required for ingestion")
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required")

    settings.docs_root.mkdir(parents=True, exist_ok=True)
    hoa_dir = settings.docs_root / resolved_hoa
    hoa_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[Path] = []
    saved_files: list[str] = []
    for upload in files:
        filename = _safe_pdf_filename(upload.filename)
        target = hoa_dir / filename
        with target.open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        saved_paths.append(target)
        saved_files.append(filename)
        await upload.close()

    normalized_website = _normalize_website_url(website_url)
    normalized_boundary = _parse_boundary_geojson(boundary_geojson)
    location_saved = False
    if any(value is not None and str(value).strip() for value in [normalized_website, street, city, state, postal_code, country, normalized_boundary]) or (
        latitude is not None and longitude is not None
    ):
        if latitude is not None and not (-90 <= latitude <= 90):
            raise HTTPException(status_code=400, detail="latitude must be between -90 and 90")
        if longitude is not None and not (-180 <= longitude <= 180):
            raise HTTPException(status_code=400, detail="longitude must be between -180 and 180")
        if (latitude is None or longitude is None) and any([street, city, state, postal_code]):
            coords = _geocode_from_parts(
                street=(street.strip() if street else None),
                city=(city.strip() if city else None),
                state=(state.strip().upper() if state else None),
                postal_code=(postal_code.strip() if postal_code else None),
            )
            if coords:
                latitude, longitude = coords
        if (latitude is None or longitude is None) and normalized_boundary:
            center = _center_from_boundary_geojson(normalized_boundary)
            if center:
                latitude, longitude = center
        with db.get_connection(settings.db_path) as conn:
            db.upsert_hoa_location(
                conn,
                resolved_hoa,
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

    background_tasks.add_task(_ingest_uploaded_files, resolved_hoa, saved_paths)
    return UploadResponse(
        hoa=resolved_hoa,
        saved_files=saved_files,
        indexed=0,
        skipped=0,
        failed=0,
        queued=True,
        location_saved=location_saved,
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
