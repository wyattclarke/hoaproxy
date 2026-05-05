"""Lightweight LLM usage logging for public-document discovery.

The log intentionally stores call metadata only: model, provider endpoint,
token counts, generation id, cost when available, and compact provenance. It
does not store prompts, completions, document text, cookies, or API keys.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)


DEFAULT_LOG_PATH = "data/model_usage.jsonl"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_DISCOVERY_MODEL_BLOCKLIST = "google/gemini,qwen/qwen3.5-flash,qwen/qwen3.6-flash"


def _env_truthy(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default) in {"1", "true", "True"}


def blocked_discovery_models() -> list[str]:
    """Return model substrings blocked for autonomous discovery runs."""
    raw = os.environ.get("HOA_DISCOVERY_MODEL_BLOCKLIST", DEFAULT_DISCOVERY_MODEL_BLOCKLIST)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def discovery_model_allowed(model: str) -> bool:
    if _env_truthy("HOA_ALLOW_BLOCKLISTED_DISCOVERY_MODELS", "0"):
        return True
    model_lower = model.lower()
    return not any(blocked in model_lower for blocked in blocked_discovery_models())


def assert_discovery_model_allowed(model: str) -> None:
    if not discovery_model_allowed(model):
        raise ValueError(
            f"Model {model!r} is blocked by HOA_DISCOVERY_MODEL_BLOCKLIST. "
            "Set HOA_ALLOW_BLOCKLISTED_DISCOVERY_MODELS=1 only for an explicit experiment."
        )


def _usage_payload(usage: Any) -> dict[str, Any]:
    if not usage:
        return {}
    keys = [
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ]
    payload: dict[str, Any] = {}
    for key in keys:
        value = getattr(usage, key, None)
        if value is not None:
            payload[key] = value
    details = getattr(usage, "completion_tokens_details", None)
    if details:
        reasoning = getattr(details, "reasoning_tokens", None)
        if reasoning is not None:
            payload["completion_reasoning_tokens"] = reasoning
    return payload


def _finish_reason(response: Any) -> str | None:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    return getattr(choices[0], "finish_reason", None)


def _openrouter_key(api_key: str | None = None) -> str | None:
    return api_key or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("QA_API_KEY")


def _openrouter_generation_url(api_base_url: str | None = None) -> str:
    base = (api_base_url or os.environ.get("OPENROUTER_BASE_URL") or OPENROUTER_BASE_URL).rstrip("/")
    return f"{base}/generation"


def fetch_openrouter_generation(
    generation_id: str,
    *,
    api_key: str | None = None,
    api_base_url: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Fetch exact OpenRouter generation metadata when account permissions allow it."""
    key = _openrouter_key(api_key)
    if not key or not generation_id:
        return None
    try:
        response = requests.get(
            _openrouter_generation_url(api_base_url),
            headers={"Authorization": f"Bearer {key}"},
            params={"id": generation_id},
            timeout=timeout,
        )
        if response.status_code >= 400:
            return {"lookup_error": f"{response.status_code}: {response.text[:240]}"}
        data = response.json().get("data") or {}
        keep = [
            "id",
            "model",
            "provider_name",
            "created_at",
            "usage",
            "total_cost",
            "cost",
            "tokens_prompt",
            "tokens_completion",
            "tokens_reasoning",
            "native_tokens_prompt",
            "native_tokens_completion",
            "native_tokens_reasoning",
            "native_tokens_cached",
            "generation_time",
            "latency",
            "finish_reason",
            "native_finish_reason",
            "cancelled",
        ]
        return {key: data.get(key) for key in keep if key in data}
    except Exception as exc:  # pragma: no cover - logging must never break calls
        logger.debug("OpenRouter generation lookup failed", exc_info=True)
        return {"lookup_error": str(exc)[:240]}


def log_llm_call(
    *,
    operation: str,
    model: str,
    api_base_url: str | None = None,
    api_key: str | None = None,
    response: Any | None = None,
    usage: dict[str, Any] | None = None,
    status: str = "success",
    error: str | None = None,
    elapsed_ms: int | None = None,
    metadata: dict[str, Any] | None = None,
    log_path: str | Path | None = None,
) -> dict[str, Any]:
    """Append one JSONL usage record and return the record.

    The function is best-effort: failures are swallowed so logging never
    changes discovery behavior.
    """
    generation_id = getattr(response, "id", None) if response is not None else None
    payload: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "status": status,
        "model": model,
        "api_base_url": api_base_url,
        "generation_id": generation_id,
        "finish_reason": _finish_reason(response) if response is not None else None,
        "elapsed_ms": elapsed_ms,
        "usage": usage or _usage_payload(getattr(response, "usage", None)),
        "metadata": metadata or {},
    }
    if error:
        payload["error"] = error[:500]

    is_openrouter = bool(api_base_url and "openrouter.ai" in api_base_url)
    if (
        status == "success"
        and is_openrouter
        and generation_id
        and _env_truthy("HOA_OPENROUTER_GENERATION_LOOKUP", "1")
    ):
        payload["openrouter_generation"] = fetch_openrouter_generation(
            generation_id,
            api_key=api_key,
            api_base_url=api_base_url,
        )

    path = Path(log_path or os.environ.get("HOA_MODEL_USAGE_LOG", DEFAULT_LOG_PATH))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            print(json.dumps(payload, sort_keys=True, default=str), file=f)
    except Exception:  # pragma: no cover - logging must never break calls
        logger.debug("Failed to write model usage log", exc_info=True)
    return payload


class CallTimer:
    """Small helper for elapsed-ms logging without leaking prompt content."""

    def __init__(self) -> None:
        self.started = time.perf_counter()

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)
