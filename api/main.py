from __future__ import annotations

from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from hoaware.config import load_settings
from hoaware.qa import get_answer

app = FastAPI(title="HOA QA API", version="0.1.0")


class QARequest(BaseModel):
    hoa: str
    question: str
    k: int = 6
    model: str = "gpt-4o-mini"


class QAResponse(BaseModel):
    answer: str
    sources: List[dict]


@app.get("/healthz")
def health() -> dict:
    return {"status": "ok"}


@app.get("/hoas")
def list_hoas() -> list[str]:
    settings = load_settings()
    if not settings.docs_root.exists():
        raise HTTPException(status_code=500, detail="Docs root not found")
    return sorted([p.name for p in settings.docs_root.iterdir() if p.is_dir()], key=str.lower)


@app.post("/qa", response_model=QAResponse)
def qa(body: QARequest) -> QAResponse:
    settings = load_settings()
    if not body.hoa:
        raise HTTPException(status_code=400, detail="hoa is required")
    answer, citations, results = get_answer(
        body.question,
        body.hoa,
        k=body.k,
        model=body.model,
        settings=settings,
    )
    if not results:
        raise HTTPException(status_code=404, detail="No context found for query.")
    return QAResponse(answer=answer, sources=citations)
