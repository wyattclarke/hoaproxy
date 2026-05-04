#!/usr/bin/env python3
"""OpenRouter-assisted Kansas HOA discovery planning.

Use this for judgment-heavy work that would otherwise consume agent context:

- validate-leads: turn noisy Serper candidate leads into bank-safe JSONL
- county-queries: generate focused county/city search queries
"""

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


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "google/gemini-3.1-pro-preview"
FALLBACK_MODEL = "deepseek/deepseek-v4-pro"
TIMEOUT_SECONDS = 60


def client() -> OpenAI:
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("QA_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY or QA_API_KEY is required")
    return OpenAI(
        api_key=key,
        base_url=os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL),
        timeout=float(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", TIMEOUT_SECONDS)),
        max_retries=0,
    )


def parse_json(text: str) -> Any:
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


def chat_json(orc: OpenAI, model: str, prompt: dict[str, Any], *, max_tokens: int = 3000) -> tuple[Any, dict[str, Any]]:
    response = orc.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "Return valid JSON only. Be strict and concise."},
            {"role": "user", "content": json.dumps(prompt, sort_keys=True)},
        ],
        temperature=0.1,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        extra_body={"include_reasoning": False},
    )
    usage = getattr(response, "usage", None)
    usage_payload = {}
    if usage:
        usage_payload = {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }
    return parse_json(response.choices[0].message.content or "{}"), usage_payload


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            print(json.dumps(row, sort_keys=True), file=f)


def _compact_candidate(row: dict[str, Any], idx: int) -> dict[str, Any]:
    return {
        "index": idx,
        "name": row.get("name"),
        "website": row.get("website"),
        "source_url": row.get("source_url"),
        "state": row.get("state"),
        "source": row.get("source"),
    }


def validate_batch(
    orc: OpenAI,
    model: str,
    batch: list[dict[str, Any]],
    *,
    state: str,
    county: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prompt = {
        "task": "Validate noisy public-web leads before probing/banking HOA governing documents.",
        "state": state,
        "county_focus": county,
        "rules": [
            "Keep only plausible HOA, homes association, homeowners association, condo/townhome association, or property owners association leads in Kansas.",
            "For this run, require evidence that the lead is in the county focus or one of its cities. Reject generic Kansas leads and leads that are probably from another county.",
            "Reject generic legal-info pages, social posts, management-company marketing pages without a specific community, government pages, and malformed names.",
            "Reject nonprofit tax filings, ProPublica/IRS pages, real estate listings, law firm pages, newspaper articles, court/case-law pages, and generic HOA explainer pages.",
            "If the name is malformed but the URL/title clearly identifies a community, repair it.",
            "Prefer community document/governing-doc pages over generic home pages.",
            "Return decisions with index, keep, repaired_name, confidence 0-1, reason.",
        ],
        "candidates": [_compact_candidate(row, idx) for idx, row in enumerate(batch)],
    }
    data, usage = chat_json(orc, model, prompt, max_tokens=2200)
    if isinstance(data, list):
        decisions = data
    elif isinstance(data, dict):
        decisions = data.get("decisions", [])
    else:
        decisions = []
    kept: list[dict[str, Any]] = []
    for decision in decisions:
        if not isinstance(decision, dict) or not decision.get("keep"):
            continue
        idx = int(decision.get("index", -1))
        if idx < 0 or idx >= len(batch):
            continue
        confidence = float(decision.get("confidence") or 0)
        if confidence < 0.65:
            continue
        row = dict(batch[idx])
        repaired = str(decision.get("repaired_name") or row.get("name") or "").strip()
        if len(repaired) < 4:
            continue
        row["name"] = repaired[:120]
        row["state"] = state
        row["county"] = county
        row["source"] = row.get("source") or f"openrouter-{model}-validated"
        row["validation"] = {
            "model": model,
            "confidence": confidence,
            "reason": str(decision.get("reason") or "")[:300],
        }
        kept.append(row)
    return kept, {"usage": usage, "raw": data}


def cmd_validate_leads(args: argparse.Namespace) -> int:
    rows = read_jsonl(Path(args.input))
    orc = client()
    kept_all: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    model = args.model
    for start in range(0, len(rows), args.batch_size):
        batch = rows[start:start + args.batch_size]
        try:
            kept, info = validate_batch(orc, model, batch, state=args.state, county=args.county)
        except Exception as exc:
            if args.fallback_model and args.fallback_model != model:
                try:
                    kept, info = validate_batch(orc, args.fallback_model, batch, state=args.state, county=args.county)
                    info["fallback_from"] = model
                    info["model"] = args.fallback_model
                except Exception as fallback_exc:
                    kept = []
                    info = {
                        "error": str(exc),
                        "fallback_model": args.fallback_model,
                        "fallback_error": str(fallback_exc),
                    }
            else:
                kept = []
                info = {"error": str(exc)}
        kept_all.extend(kept)
        audit.append({"start": start, "count": len(batch), "kept": len(kept), **info})
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for row in kept_all:
        url = str(row.get("website") or row.get("source_url") or "")
        key = (str(row.get("name") or "").lower(), re.sub(r"^https?://(www\.)?", "", url).split("/", 1)[0].lower())
        if key in seen:
            continue
        seen.add(key)
        lead = Lead(
            name=str(row["name"]),
            source=str(row.get("source") or "openrouter-validated"),
            source_url=str(row.get("source_url") or row.get("website") or ""),
            state=args.state,
            county=args.county,
            city=row.get("city"),
            website=row.get("website"),
        )
        deduped.append(asdict(lead))
    write_jsonl(Path(args.output), deduped)
    Path(args.audit).write_text(json.dumps(audit, indent=2, sort_keys=True))
    print(json.dumps({"input": len(rows), "kept": len(deduped), "output": args.output, "audit": args.audit}))
    return 0


def cmd_county_queries(args: argparse.Namespace) -> int:
    orc = client()
    prompt = {
        "task": "Generate high-yield Google/Serper search queries to find public HOA governing document pages.",
        "state": args.state,
        "county": args.county,
        "rules": [
            "Focus county by county.",
            "Prefer public community websites and document pages.",
            "Include city names, 'homes association', 'HOA documents', 'governing documents', 'deed restrictions', 'declaration of restrictions'.",
            "Avoid logged-in portals, generic legal-info sites, and broad state-law pages.",
            "Return JSON with key queries.",
        ],
        "max_queries": args.count,
    }
    model = args.model
    try:
        data, usage = chat_json(orc, model, prompt, max_tokens=3200)
    except Exception:
        if not args.fallback_model or args.fallback_model == model:
            raise
        model = args.fallback_model
        data, usage = chat_json(orc, model, prompt, max_tokens=3200)
    queries = [str(q).strip() for q in data.get("queries", []) if str(q).strip()]
    Path(args.output).write_text("\n".join(queries[: args.count]) + "\n")
    print(json.dumps({"model": model, "queries": len(queries[: args.count]), "output": args.output, "usage": usage}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenRouter-assisted KS discovery planner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    validate = sub.add_parser("validate-leads")
    validate.add_argument("input")
    validate.add_argument("--output", required=True)
    validate.add_argument("--audit", required=True)
    validate.add_argument("--state", default="KS")
    validate.add_argument("--county", required=True)
    validate.add_argument("--model", default=DEFAULT_MODEL)
    validate.add_argument("--fallback-model", default=FALLBACK_MODEL)
    validate.add_argument("--batch-size", type=int, default=20)
    validate.set_defaults(func=cmd_validate_leads)

    queries = sub.add_parser("county-queries")
    queries.add_argument("--output", required=True)
    queries.add_argument("--state", default="KS")
    queries.add_argument("--county", required=True)
    queries.add_argument("--model", default=DEFAULT_MODEL)
    queries.add_argument("--fallback-model", default=FALLBACK_MODEL)
    queries.add_argument("--count", type=int, default=40)
    queries.set_defaults(func=cmd_county_queries)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
