from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import List, Tuple

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError
from rich.console import Console
from rich.panel import Panel

from . import db
from .config import Settings, load_settings
from .cost_tracker import log_chat_usage
from .embeddings import batch_embeddings

console = Console()


class QATemporaryError(RuntimeError):
    """Raised when the configured QA provider is temporarily unavailable."""


class QAProviderError(RuntimeError):
    """Raised when the configured QA provider rejects or fails a request."""


_TRANSIENT_PROVIDER_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


@dataclass
class QACompletionResult:
    completion: object
    model: str
    used_fallback: bool = False


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


def retrieve_context(
    question: str,
    hoa_name: str,
    k: int,
    settings: Settings,
) -> List[dict]:
    openai_client = OpenAI(api_key=settings.openai_api_key)
    embedding = batch_embeddings([question], openai_client, settings.embedding_model)[0]
    with db.get_connection(settings.db_path) as conn:
        return db.vector_search(conn, hoa_name, embedding, limit=k)


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
    per_hoa_limit = max(3, math.ceil(k / len(normalized_hoas)) + 2)
    merged: List[dict] = []
    with db.get_connection(settings.db_path) as conn:
        for hoa_name in normalized_hoas:
            for item in db.vector_search(conn, hoa_name, embedding, limit=per_hoa_limit):
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


def _qa_chat_client(settings: Settings) -> OpenAI:
    """Build an OpenAI-compatible client for the configured QA endpoint.

    Falls back to OpenAI's API if QA_API_KEY isn't set but OPENAI_API_KEY is.
    """
    if settings.qa_api_key:
        return OpenAI(api_key=settings.qa_api_key, base_url=settings.qa_api_base_url)
    return OpenAI(api_key=settings.openai_api_key)


def _qa_fallback_chat_client(settings: Settings, primary_model: str) -> OpenAI | None:
    if not settings.qa_fallback_model or not settings.qa_fallback_api_key:
        return None
    primary_is_direct_openai = not settings.qa_api_key
    if primary_is_direct_openai and settings.qa_fallback_model == primary_model:
        return None
    if settings.qa_fallback_api_base_url:
        return OpenAI(api_key=settings.qa_fallback_api_key, base_url=settings.qa_fallback_api_base_url)
    return OpenAI(api_key=settings.qa_fallback_api_key)


def _resolve_qa_model(requested: str, settings: Settings) -> str:
    """Use the configured QA model unless caller passed an explicit override."""
    if requested and not requested.startswith("gpt-"):
        return requested
    if not settings.qa_api_key and settings.qa_model == "llama-3.3-70b-versatile":
        return requested or "gpt-5-mini"
    return settings.qa_model


def _call_chat_provider(chat_client: OpenAI, *, model: str, messages: list[dict]):
    return chat_client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.2,
    )


def _is_transient_provider_error(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError)):
        return True
    return isinstance(exc, APIStatusError) and exc.status_code in _TRANSIENT_PROVIDER_STATUS_CODES


def _raise_provider_error(exc: Exception) -> None:
    if isinstance(exc, APIStatusError):
        if exc.status_code in _TRANSIENT_PROVIDER_STATUS_CODES:
            raise QATemporaryError(f"Q&A provider temporarily unavailable (HTTP {exc.status_code})") from exc
        raise QAProviderError(f"Q&A provider returned HTTP {exc.status_code}") from exc
    raise QATemporaryError("Q&A provider temporarily unavailable") from exc


def _create_chat_completion(
    chat_client: OpenAI,
    *,
    model: str,
    messages: list[dict],
    fallback_client: OpenAI | None = None,
    fallback_model: str | None = None,
) -> QACompletionResult:
    """Call primary QA provider, then a low-latency fallback on transient failure."""
    try:
        completion = _call_chat_provider(chat_client, model=model, messages=messages)
        return QACompletionResult(completion=completion, model=model)
    except Exception as exc:
        if not _is_transient_provider_error(exc):
            _raise_provider_error(exc)
        primary_exc = exc

    if fallback_client and fallback_model:
        time.sleep(0.2)
        try:
            completion = _call_chat_provider(fallback_client, model=fallback_model, messages=messages)
            return QACompletionResult(completion=completion, model=fallback_model, used_fallback=True)
        except Exception as exc:
            _raise_provider_error(exc)

    _raise_provider_error(primary_exc)


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

    chat_client = _qa_chat_client(settings)
    chat_model = _resolve_qa_model(model, settings)
    results = retrieve_context(question, hoa_name, k, settings)
    if not results:
        return "No context retrieved; cannot answer.", [], []

    messages = _build_prompt(question, results, f"the {hoa_name} HOA")
    fallback_client = _qa_fallback_chat_client(settings, chat_model)
    completion_result = _create_chat_completion(
        chat_client,
        model=chat_model,
        messages=messages,
        fallback_client=fallback_client,
        fallback_model=settings.qa_fallback_model,
    )
    completion = completion_result.completion
    if hasattr(completion, "usage") and completion.usage:
        log_chat_usage(completion.usage.prompt_tokens, completion.usage.completion_tokens, model=completion_result.model)
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

    chat_client = _qa_chat_client(settings)
    chat_model = _resolve_qa_model(model, settings)
    results = retrieve_context_multi(question, normalized_hoas, k, settings)
    if not results:
        return "No context retrieved; cannot answer.", [], []

    scope_label = f"the selected HOAs ({', '.join(normalized_hoas)})"
    messages = _build_prompt(question, results, scope_label)
    fallback_client = _qa_fallback_chat_client(settings, chat_model)
    completion_result = _create_chat_completion(
        chat_client,
        model=chat_model,
        messages=messages,
        fallback_client=fallback_client,
        fallback_model=settings.qa_fallback_model,
    )
    completion = completion_result.completion
    if hasattr(completion, "usage") and completion.usage:
        log_chat_usage(completion.usage.prompt_tokens, completion.usage.completion_tokens, model=completion_result.model)
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
