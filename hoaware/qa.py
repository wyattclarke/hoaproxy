from __future__ import annotations

from typing import List, Tuple

from openai import OpenAI
from rich.console import Console
from rich.panel import Panel

from .config import Settings, load_settings, normalize_hoa_name
from .embeddings import batch_embeddings
from .vector_store import build_client, ensure_collection, search as qdrant_search

console = Console()


def _build_prompt(question: str, context: List[dict], hoa_name: str) -> list[dict]:
    system = (
        f"You are an assistant for the {hoa_name} HOA. "
        "Answer using only the provided context. "
        "Cite sources with document names and pages. "
        "If the answer is not in the context, say you cannot find it."
    )
    context_lines = []
    for item in context:
        payload = item["payload"]
        doc = payload.get("document", "unknown")
        pages = f"{payload.get('start_page')}–{payload.get('end_page')}"
        text = payload.get("text", "")
        context_lines.append(f"[{doc} | pages {pages}]\n{text}")
    context_block = "\n\n".join(context_lines)
    user = f"Question: {question}\n\nContext:\n{context_block}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def retrieve_context(
    question: str,
    hoa_name: str,
    k: int,
    settings: Settings,
) -> List[dict]:
    openai_client = OpenAI(api_key=settings.openai_api_key)
    embedding = batch_embeddings([question], openai_client, settings.embedding_model)[0]
    qdrant_client = build_client(
        settings.qdrant_url,
        settings.qdrant_api_key,
        local_path=settings.qdrant_local_path,
    )
    collection = f"{settings.collection_prefix}_{normalize_hoa_name(hoa_name)}"
    ensure_collection(qdrant_client, collection)
    return qdrant_search(
        qdrant_client,
        collection_name=collection,
        query_vector=embedding,
        limit=k,
        hoa_name=hoa_name,
    )


def build_citations(results: List[dict]) -> List[dict]:
    citations: List[dict] = []
    for item in results:
        payload = item["payload"]
        citations.append(
            {
                "document": payload.get("document", "unknown"),
                "pages": f"{payload.get('start_page')}–{payload.get('end_page')}",
            }
        )
    return citations


def get_answer(
    question: str,
    hoa_name: str,
    k: int,
    model: str,
    settings: Settings,
) -> Tuple[str, List[dict], List[dict]]:
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for retrieval and QA.")
    if not question.strip():
        raise ValueError("question is required")
    if not hoa_name.strip():
        raise ValueError("hoa_name is required")

    openai_client = OpenAI(api_key=settings.openai_api_key)
    results = retrieve_context(question, hoa_name, k, settings)
    if not results:
        return "No context retrieved; cannot answer.", [], []

    messages = _build_prompt(question, results, hoa_name)
    completion = openai_client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.2,
    )
    answer = completion.choices[0].message.content or ""
    citations = build_citations(results)
    return answer.strip(), citations, results


def answer_question(
    question: str,
    hoa_name: str,
    k: int = 6,
    model: str = "gpt-4o-mini",
    settings: Settings | None = None,
) -> None:
    settings = settings or load_settings()
    answer, citations, results = get_answer(question, hoa_name, k, model, settings)
    if not results:
        console.print("[yellow]No context retrieved; cannot answer.[/yellow]")
        return
    console.print(Panel(answer, title="Answer"))
    console.print("[bold]Sources:[/bold]")
    for cite in citations:
        console.print(f"- {cite['document']} (pages {cite['pages']})")
