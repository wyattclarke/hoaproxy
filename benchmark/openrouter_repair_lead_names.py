#!/usr/bin/env python3
"""Use OpenRouter for compact lead-name repair after deterministic PDF cleaning."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from hoaware.discovery.leads import Lead  # noqa: E402
from hoaware.model_usage import CallTimer, assert_discovery_model_allowed, log_llm_call  # noqa: E402


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_FALLBACK = "moonshotai/kimi-k2.6"


def _client() -> OpenAI:
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("QA_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY or QA_API_KEY is required")
    return OpenAI(
        api_key=key,
        base_url=os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL),
        timeout=float(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "60")),
        max_retries=0,
    )


def _parse_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def _decisions_from_data(data: Any) -> list[Any]:
    if isinstance(data, dict):
        if "keep" in data and "index" in data:
            return [data]
        decisions = data.get("decisions", [])
        if isinstance(decisions, list):
            return decisions
        candidates = data.get("candidates", [])
        return candidates if isinstance(candidates, list) else []
    if isinstance(data, list):
        return data
    return []


def _chat_json(client: OpenAI, model: str, prompt: dict[str, Any], *, operation: str) -> Any:
    assert_discovery_model_allowed(model)
    base_url = os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL)
    timer = CallTimer()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Return valid JSON only. Be strict and concise."},
                {"role": "user", "content": json.dumps(prompt, sort_keys=True)},
            ],
            temperature=0.1,
            max_tokens=2200,
            response_format={"type": "json_object"},
            extra_body={"include_reasoning": False},
        )
    except Exception as exc:
        log_llm_call(
            operation=operation,
            model=model,
            api_base_url=base_url,
            status="error",
            error=str(exc),
            elapsed_ms=timer.elapsed_ms(),
            metadata={
                "state": prompt.get("state"),
                "candidate_count": len(prompt.get("candidates", [])),
                "task": prompt.get("task"),
            },
        )
        raise
    log_llm_call(
        operation=operation,
        model=model,
        api_base_url=base_url,
        response=response,
        elapsed_ms=timer.elapsed_ms(),
        metadata={
            "state": prompt.get("state"),
            "candidate_count": len(prompt.get("candidates", [])),
            "task": prompt.get("task"),
        },
    )
    return _parse_json(response.choices[0].message.content or "{}")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _audit_by_url(path: Path | None) -> dict[str, dict[str, Any]]:
    if not path or not path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") != "lead":
                continue
            lead = row.get("lead") or {}
            url = str(lead.get("source_url") or "")
            if url:
                out[url] = row
    return out


def _candidate(row: dict[str, Any], audit: dict[str, Any], idx: int) -> dict[str, Any]:
    return {
        "index": idx,
        "current_name": row.get("name"),
        "url": row.get("source_url"),
        "title": audit.get("title"),
        "snippet": audit.get("snippet"),
        "category": (row.get("cleaning") or {}).get("category"),
    }


def _clean_model_name(value: str) -> str | None:
    name = re.sub(r"\s+", " ", value or "").strip(" .,-:;[]()")
    name = re.sub(r"(?i)\bhome\s*owner'?s?\s+association\b", "Homeowners Association", name)
    name = re.sub(r"(?i)\bhomeowners?\s+association\b", "Homeowners Association", name)
    name = re.sub(r"(?i)\bhoa\b", "HOA", name)
    if len(name) < 5 or len(name) > 100:
        return None
    if re.search(r"(?i)\b(untitled|document|pdf|county register|godaddy|rackcdn|wordpress|overview|suite|street)\b", name):
        return None
    if not re.search(r"(?i)\b(HOA|Association|Condominium|Townhomes?)\b", name):
        name = f"{name} HOA"
    return name


def repair(args: argparse.Namespace) -> int:
    rows = _read_jsonl(Path(args.input))
    audit = _audit_by_url(Path(args.audit)) if args.audit else {}
    client = _client()
    kept: list[dict[str, Any]] = []
    audit_batches: list[dict[str, Any]] = []
    for start in range(0, len(rows), args.batch_size):
        batch = rows[start:start + args.batch_size]
        prompt = {
            "task": "Repair and validate HOA lead names for public governing-document banking.",
            "state": args.state.upper(),
            "rules": [
                f"Keep only specific HOA/condo/townhome/property-owner associations in {args.state_name}.",
                "Reject government reports, legal articles, real-estate listings, generic management pages, and candidates where the specific association name is not clear.",
                "Repair names to the community association name only, e.g. 'Hidden Harbor Homeowners Association'.",
                "Do not include document titles, boilerplate, county register language, URLs, cities, file hosts, or snippets in the repaired name.",
                "Return decisions with index, keep, repaired_name, confidence 0-1, reason.",
            ],
            "candidates": [_candidate(row, audit.get(str(row.get("source_url") or ""), {}), idx) for idx, row in enumerate(batch)],
        }
        model = args.model
        try:
            data = _chat_json(client, model, prompt, operation="openrouter_repair_lead_names")
            used_model = model
        except Exception as exc:
            if not args.fallback_model:
                audit_batches.append({"start": start, "error": str(exc)})
                continue
            data = _chat_json(client, args.fallback_model, prompt, operation="openrouter_repair_lead_names")
            used_model = args.fallback_model
        decisions = _decisions_from_data(data)
        kept_count = 0
        for decision in decisions:
            if not isinstance(decision, dict) or not decision.get("keep"):
                continue
            idx = int(decision.get("index", -1))
            if idx < 0 or idx >= len(batch):
                continue
            confidence = float(decision.get("confidence") or 0)
            if confidence < args.min_confidence:
                continue
            repaired = _clean_model_name(str(decision.get("repaired_name") or ""))
            if not repaired:
                continue
            row = dict(batch[idx])
            row["name"] = repaired
            row["state"] = args.state.upper()
            row["source"] = f"openrouter-repaired-{args.state.lower()}"
            row["validation"] = {
                "model": used_model,
                "confidence": confidence,
                "reason": str(decision.get("reason") or "")[:240],
            }
            lead = Lead(
                name=row["name"],
                source=row["source"],
                source_url=str(row.get("source_url") or ""),
                state=row["state"],
                city=row.get("city"),
                county=row.get("county"),
                website=row.get("website"),
            )
            payload = asdict(lead)
            payload["pre_discovered_pdf_urls"] = list(row.get("pre_discovered_pdf_urls") or [])
            payload["cleaning"] = row.get("cleaning")
            payload["validation"] = row["validation"]
            kept.append(payload)
            kept_count += 1
        audit_batches.append({"start": start, "count": len(batch), "kept": kept_count, "model": used_model, "raw": data})
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for row in kept:
        url = str(row.get("source_url") or "")
        key = (str(row.get("name") or "").lower(), url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.output).open("w") as f:
        for row in deduped:
            print(json.dumps(row, sort_keys=True), file=f)
    Path(args.audit_output).write_text(json.dumps(audit_batches, indent=2, sort_keys=True))
    print(json.dumps({"input": len(rows), "kept": len(deduped), "output": args.output, "audit": args.audit_output}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair direct-PDF lead names with compact OpenRouter validation")
    parser.add_argument("input")
    parser.add_argument("--audit")
    parser.add_argument("--output", required=True)
    parser.add_argument("--audit-output", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--state-name", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--fallback-model", default=DEFAULT_FALLBACK)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--min-confidence", type=float, default=0.7)
    args = parser.parse_args()
    return repair(args)


if __name__ == "__main__":
    raise SystemExit(main())
