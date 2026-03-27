from __future__ import annotations

import math
from typing import List, Tuple

from openai import OpenAI
from rich.console import Console
from rich.panel import Panel

from .config import Settings, load_settings, normalize_hoa_name
from .cost_tracker import log_chat_usage
from .embeddings import batch_embeddings
from .vector_store import build_client, ensure_collection, search as qdrant_search

console = Console()


def _build_prompt(question: str, context: List[dict], scope_label: str) -> list[dict]:
    system = (
        f"You are an assistant for {scope_label}. "
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


def _search_embedding_for_hoa(
    embedding: list[float],
    hoa_name: str,
    limit: int,
    settings: Settings,
    qdrant_client,
) -> List[dict]:
    collection = f"{settings.collection_prefix}_{normalize_hoa_name(hoa_name)}"
    ensure_collection(qdrant_client, collection)
    return qdrant_search(
        qdrant_client,
        collection_name=collection,
        query_vector=embedding,
        limit=limit,
        hoa_name=hoa_name,
    )


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
    return _search_embedding_for_hoa(
        embedding=embedding,
        hoa_name=hoa_name,
        limit=k,
        settings=settings,
        qdrant_client=qdrant_client,
    )


def retrieve_context_multi(
    question: str,
    hoa_names: List[str],
    k: int,
    settings: Settings,
) -> List[dict]:
    if not hoa_names:
        raise ValueError("hoas is required")
    normalized_hoas = [hoa.strip() for hoa in hoa_names if str(hoa).strip()]
    if not normalized_hoas:
        raise ValueError("hoas is required")

    openai_client = OpenAI(api_key=settings.openai_api_key)
    embedding = batch_embeddings([question], openai_client, settings.embedding_model)[0]
    qdrant_client = build_client(
        settings.qdrant_url,
        settings.qdrant_api_key,
        local_path=settings.qdrant_local_path,
    )
    per_hoa_limit = max(3, math.ceil(k / len(normalized_hoas)) + 2)
    merged: List[dict] = []
    for hoa_name in normalized_hoas:
        for item in _search_embedding_for_hoa(
            embedding=embedding,
            hoa_name=hoa_name,
            limit=per_hoa_limit,
            settings=settings,
            qdrant_client=qdrant_client,
        ):
            payload = dict(item.get("payload") or {})
            payload.setdefault("hoa", hoa_name)
            merged.append({"score": float(item["score"]), "payload": payload})
    merged.sort(key=lambda row: float(row.get("score") or 0.0), reverse=True)
    return merged[:k]


def build_citations(results: List[dict]) -> List[dict]:
    citations: List[dict] = []
    for item in results:
        payload = item["payload"]
        citation = {
            "document": payload.get("document", "unknown"),
            "pages": f"{payload.get('start_page')}–{payload.get('end_page')}",
        }
        if payload.get("hoa"):
            citation["hoa"] = str(payload.get("hoa"))
        citations.append(citation)
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

    messages = _build_prompt(question, results, f"the {hoa_name} HOA")
    # GPT-5 models currently require default temperature handling.
    completion_kwargs = {
        "model": model,
        "messages": messages,
    }
    if not model.startswith("gpt-5"):
        completion_kwargs["temperature"] = 0.2
    completion = openai_client.chat.completions.create(**completion_kwargs)
    if hasattr(completion, "usage") and completion.usage:
        log_chat_usage(completion.usage.prompt_tokens, completion.usage.completion_tokens, model=model)
    answer = completion.choices[0].message.content or ""
    citations = build_citations(results)
    return answer.strip(), citations, results


def get_answer_multi(
    question: str,
    hoa_names: List[str],
    k: int,
    model: str,
    settings: Settings,
) -> Tuple[str, List[dict], List[dict]]:
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for retrieval and QA.")
    if not question.strip():
        raise ValueError("question is required")
    normalized_hoas = [hoa.strip() for hoa in hoa_names if str(hoa).strip()]
    if not normalized_hoas:
        raise ValueError("hoas is required")

    openai_client = OpenAI(api_key=settings.openai_api_key)
    results = retrieve_context_multi(question, normalized_hoas, k, settings)
    if not results:
        return "No context retrieved; cannot answer.", [], []

    scope_label = f"the selected HOAs ({', '.join(normalized_hoas)})"
    messages = _build_prompt(question, results, scope_label)
    completion_kwargs = {
        "model": model,
        "messages": messages,
    }
    if not model.startswith("gpt-5"):
        completion_kwargs["temperature"] = 0.2
    completion = openai_client.chat.completions.create(**completion_kwargs)
    if hasattr(completion, "usage") and completion.usage:
        log_chat_usage(completion.usage.prompt_tokens, completion.usage.completion_tokens, model=model)
    answer = completion.choices[0].message.content or ""
    citations = build_citations(results)
    return answer.strip(), citations, results


def answer_question(
    question: str,
    hoa_name: str,
    k: int = 6,
    model: str = "gpt-5-mini",
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
