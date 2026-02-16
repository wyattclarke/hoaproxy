from __future__ import annotations

from html import escape as html_escape
import json
import logging
import re
import shutil
from pathlib import Path
from typing import List
from urllib.parse import quote, unquote, urlparse

import requests
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hoaware import db
from hoaware.config import load_settings
from hoaware.ingest import ingest_pdf_paths
from hoaware.qa import get_answer, retrieve_context

app = FastAPI(title="HOA QA API", version="0.2.0")
_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_CITY_STATE_ZIP_RE = re.compile(
    r"\b([A-Z][A-Za-z]+(?:[\s-][A-Z][A-Za-z]+)*),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b"
)
_STREET_RE = re.compile(
    r"\b\d{2,6}\s+[A-Za-z0-9][A-Za-z0-9 .'-]{3,80}\b"
    r"(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Way|Circle|Cir|Parkway|Pkwy|Trail|Trl)\b",
    re.IGNORECASE,
)
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


class UploadResponse(BaseModel):
    hoa: str
    saved_files: List[str]
    indexed: int
    skipped: int
    failed: int
    queued: bool = False
    location_saved: bool = False


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


def _normalize_hoa_name(raw_name: str) -> str:
    cleaned = " ".join(raw_name.split()).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="hoa is required")
    if "/" in cleaned or "\\" in cleaned or cleaned in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid HOA name")
    return cleaned


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
    return {"status": "ok"}


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
