from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hoaware import db
from hoaware.config import load_settings
from hoaware.ingest import ingest_pdf_paths
from hoaware.qa import get_answer, retrieve_context

app = FastAPI(title="HOA QA API", version="0.2.0")
_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
STATIC_DIR = Path(__file__).resolve().parent / "static"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class QARequest(BaseModel):
    hoa: str
    question: str
    k: int = Field(default=6, ge=1, le=20)
    model: str = "gpt-4o-mini"


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


class DocumentSummary(BaseModel):
    relative_path: str
    bytes: int
    page_count: int | None
    chunk_count: int
    last_ingested: str


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


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    if not STATIC_DIR.exists():
        raise HTTPException(status_code=404, detail="UI not available")
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
def health() -> dict:
    return {"status": "ok"}


@app.get("/hoas")
def list_hoas() -> list[str]:
    settings = load_settings()
    fs_hoas: list[str] = []
    if settings.docs_root.exists():
        fs_hoas = [p.name for p in settings.docs_root.iterdir() if p.is_dir()]
    with db.get_connection(settings.db_path) as conn:
        db_hoas = db.list_hoa_names(conn)
    return sorted(set(fs_hoas + db_hoas), key=str.lower)


@app.get("/hoas/{hoa_name}/documents", response_model=List[DocumentSummary])
def list_documents(hoa_name: str) -> List[DocumentSummary]:
    settings = load_settings()
    resolved_hoa = _resolve_hoa_name(hoa_name)
    with db.get_connection(settings.db_path) as conn:
        rows = db.list_documents_for_hoa(conn, resolved_hoa)
    return [DocumentSummary(**row) for row in rows]


@app.post("/upload", response_model=UploadResponse)
async def upload_documents(
    hoa: str = Form(...),
    files: List[UploadFile] = File(...),
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

    stats = ingest_pdf_paths(resolved_hoa, saved_paths, settings=settings, show_progress=False)
    return UploadResponse(
        hoa=resolved_hoa,
        saved_files=saved_files,
        indexed=stats.indexed,
        skipped=stats.skipped,
        failed=stats.failed,
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
