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
from hoaware.model_usage import CallTimer, assert_discovery_model_allowed, log_llm_call  # noqa: E402


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
FALLBACK_MODEL = "moonshotai/kimi-k2.6"
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


def chat_json(
    orc: OpenAI,
    model: str,
    prompt: dict[str, Any],
    *,
    max_tokens: int = 3000,
    operation: str = "openrouter_ks_planner.chat_json",
) -> tuple[Any, dict[str, Any]]:
    assert_discovery_model_allowed(model)
    base_url = os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL)
    timer = CallTimer()
    log_metadata = {
        "task": prompt.get("task"),
        "state": prompt.get("state"),
        "county_focus": prompt.get("county_focus") or prompt.get("county"),
        "candidate_count": len(prompt.get("candidates", [])) if isinstance(prompt.get("candidates"), list) else None,
    }
    try:
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
    except Exception as exc:
        log_llm_call(
            operation=operation,
            model=model,
            api_base_url=base_url,
            status="error",
            error=str(exc),
            elapsed_ms=timer.elapsed_ms(),
            metadata=log_metadata,
        )
        raise
    log_llm_call(
        operation=operation,
        model=model,
        api_base_url=base_url,
        response=response,
        elapsed_ms=timer.elapsed_ms(),
        metadata=log_metadata,
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
        "county": row.get("county"),
        "city": row.get("city"),
        "source": row.get("source"),
    }


def _clean_state(value: Any, fallback: str) -> str:
    state = re.sub(r"[^A-Za-z]", "", str(value or "")).upper()
    return state[:2] if len(state) == 2 else fallback.upper()


def _clean_county(value: Any) -> str | None:
    county = re.sub(r"\s+", " ", str(value or "")).strip(" .,-")
    if not county:
        return None
    county = re.sub(r"(?i)\s+county$", "", county).strip(" .,-")
    if not county or len(county) > 80:
        return None
    return county


def _decisions_from_data(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        decisions = data.get("decisions", [])
        return decisions if isinstance(decisions, list) else []
    return []


def _decisions_by_index(data: Any, batch_len: int) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for decision in _decisions_from_data(data):
        if not isinstance(decision, dict):
            continue
        try:
            idx = int(decision.get("index", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < batch_len:
            out[idx] = decision
    return out


def _validation_prompt(
    batch: list[dict[str, Any]],
    *,
    state: str,
    county: str | None,
    reason: str | None = None,
) -> dict[str, Any]:
    # Per the playbook ("Out-Of-State And Out-Of-County Hits Are Free Wins,
    # Not Rejects"), border-metro hits get re-routed downstream — don't
    # reject at validation. Just note the focus county/state so the model
    # prefers them but doesn't drop other-state hits.
    scope_rule = (
        f"This run targets {county} County, {state}. Prefer leads in {county} County, but if a lead is clearly a mandatory HOA in another county or another US state (e.g. a border-metro hit), KEEP it — the bank routes by the lead's own evidence, not by this sweep's scope."
        if county
        else f"This run targets {state}. Prefer leads in {state}, but if a lead is clearly a mandatory HOA in another US state, KEEP it — the bank routes by the lead's own evidence."
    )
    prompt: dict[str, Any] = {
        "task": "Validate noisy public-web leads before probing/banking HOA governing documents.",
        "state": state,
        "county_focus": county,
        "rules": [
            f"Keep only plausible HOA, homes association, homeowners association, condo/townhome association, or property owners association leads in {state}.",
            scope_rule,
            "REJECT voluntary neighborhood/civic associations and garden clubs that do not have recorded deed restrictions. Only mandatory associations created by recorded covenants (Declaration of Covenants, CC&Rs, Restrictive Covenants, Master Deed, Articles of Incorporation of an HOA) qualify. If the only signal is 'architectural guidelines' or 'design guidelines' with no mandatory-HOA evidence, reject.",
            "Reject generic legal-info pages, social posts, management-company marketing pages without a specific community, government pages, and malformed names.",
            "Reject nonprofit tax filings, ProPublica/IRS pages, real estate listings, law firm pages, newspaper articles, court/case-law pages, and generic HOA explainer pages.",
            "If the name is malformed but the URL/title clearly identifies a community, repair it.",
            "If evidence clearly shows a different US state or county, keep the lead and return repaired_state as the two-letter state code and repaired_county without the word County. If state/county are unclear, leave them null.",
            "Prefer community document/governing-doc pages over generic home pages.",
            "Return decisions with index, keep, repaired_name, repaired_state, repaired_county, confidence 0-1, reason.",
        ],
        "candidates": [_compact_candidate(row, idx) for idx, row in enumerate(batch)],
    }
    if reason:
        prompt["quality_fallback_reason"] = reason
    return prompt


def _needs_quality_fallback(decision: dict[str, Any] | None, threshold: float) -> bool:
    if not decision:
        return True
    try:
        confidence = float(decision.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    if not decision.get("keep"):
        return True
    if confidence < threshold:
        return True
    repaired = str(decision.get("repaired_name") or "").strip()
    return len(repaired) < 4


def _kept_from_decisions(
    batch: list[dict[str, Any]],
    decisions_by_index: dict[int, dict[str, Any]],
    *,
    model: str,
    state: str,
    county: str | None,
    min_confidence: float,
) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for idx in range(len(batch)):
        decision = decisions_by_index.get(idx)
        if not decision or not decision.get("keep"):
            continue
        confidence = float(decision.get("confidence") or 0)
        if confidence < min_confidence:
            continue
        row = dict(batch[idx])
        repaired = str(decision.get("repaired_name") or row.get("name") or "").strip()
        if len(repaired) < 4:
            continue
        row["name"] = repaired[:120]
        row["state"] = _clean_state(decision.get("repaired_state") or row.get("state"), state)
        repaired_county = _clean_county(decision.get("repaired_county") or row.get("county"))
        if repaired_county:
            row["county"] = repaired_county
        elif row["state"] == state.upper() and county:
            row["county"] = county
        else:
            row.pop("county", None)
        row["source"] = row.get("source") or f"openrouter-{model}-validated"
        row["validation"] = {
            "model": str(decision.get("quality_fallback_model") or model),
            "primary_model": str(decision.get("primary_model") or model),
            "quality_fallback_from": decision.get("quality_fallback_from"),
            "confidence": confidence,
            "reason": str(decision.get("reason") or "")[:300],
            "target_state": state,
            "target_county": county,
        }
        kept.append(row)
    return kept


def validate_batch(
    orc: OpenAI,
    model: str,
    batch: list[dict[str, Any]],
    *,
    state: str,
    county: str | None,
    min_confidence: float = 0.65,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prompt = _validation_prompt(batch, state=state, county=county)
    data, usage = chat_json(orc, model, prompt, max_tokens=2200, operation="openrouter_ks_planner.validate_leads")
    decisions_by_index = _decisions_by_index(data, len(batch))
    kept = _kept_from_decisions(
        batch,
        decisions_by_index,
        model=model,
        state=state,
        county=county,
        min_confidence=min_confidence,
    )
    return kept, {"usage": usage, "raw": data, "decisions_by_index": decisions_by_index}


def cmd_validate_leads(args: argparse.Namespace) -> int:
    rows = read_jsonl(Path(args.input))
    orc = client()
    kept_all: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    model = args.model
    for start in range(0, len(rows), args.batch_size):
        batch = rows[start:start + args.batch_size]
        try:
            kept, info = validate_batch(
                orc,
                model,
                batch,
                state=args.state,
                county=args.county,
                min_confidence=args.min_confidence,
            )
            if (
                args.fallback_model
                and args.fallback_model != model
                and args.quality_fallback_max_candidates > 0
            ):
                primary_decisions = info.pop("decisions_by_index", {})
                fallback_indices = [
                    idx
                    for idx in range(len(batch))
                    if _needs_quality_fallback(primary_decisions.get(idx), args.quality_fallback_threshold)
                ][: args.quality_fallback_max_candidates]
                if fallback_indices:
                    fallback_batch = [batch[idx] for idx in fallback_indices]
                    fallback_prompt = _validation_prompt(
                        fallback_batch,
                        state=args.state,
                        county=args.county,
                        reason=(
                            f"Quality fallback for candidates the primary model rejected, could not name, "
                            f"or scored below {args.quality_fallback_threshold}."
                        ),
                    )
                    try:
                        fallback_data, fallback_usage = chat_json(
                            orc,
                            args.fallback_model,
                            fallback_prompt,
                            max_tokens=1800,
                            operation="openrouter_ks_planner.validate_leads.quality_fallback",
                        )
                        fallback_decisions = _decisions_by_index(fallback_data, len(fallback_batch))
                        merged_decisions = dict(primary_decisions)
                        for fallback_idx, decision in fallback_decisions.items():
                            merged_decisions[fallback_indices[fallback_idx]] = {
                                **decision,
                                "quality_fallback_model": args.fallback_model,
                                "quality_fallback_from": model,
                                "primary_model": model,
                            }
                        kept = _kept_from_decisions(
                            batch,
                            merged_decisions,
                            model=model,
                            state=args.state,
                            county=args.county,
                            min_confidence=args.min_confidence,
                        )
                        info["quality_fallback"] = {
                            "fallback_model": args.fallback_model,
                            "fallback_indices": fallback_indices,
                            "usage": fallback_usage,
                            "raw": fallback_data,
                        }
                    except Exception as fallback_exc:
                        info["quality_fallback"] = {
                            "fallback_model": args.fallback_model,
                            "fallback_indices": fallback_indices,
                            "error": str(fallback_exc),
                        }
        except Exception as exc:
            kept = []
            info = {"error": str(exc)}
        else:
            info.pop("decisions_by_index", None)
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
            state=str(row.get("state") or args.state).upper(),
            county=row.get("county") or (args.county if str(row.get("state") or args.state).upper() == args.state.upper() else None),
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
    data, usage = chat_json(orc, model, prompt, max_tokens=3200, operation="openrouter_ks_planner.county_queries")
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
    validate.add_argument("--county")
    validate.add_argument("--model", default=DEFAULT_MODEL)
    validate.add_argument("--fallback-model", default=FALLBACK_MODEL, help="Bounded quality fallback model; never used to retry a whole failed batch.")
    validate.add_argument("--batch-size", type=int, default=20)
    validate.add_argument("--min-confidence", type=float, default=0.65)
    validate.add_argument("--quality-fallback-threshold", type=float, default=0.82)
    validate.add_argument("--quality-fallback-max-candidates", type=int, default=8)
    validate.set_defaults(func=cmd_validate_leads)

    queries = sub.add_parser("county-queries")
    queries.add_argument("--output", required=True)
    queries.add_argument("--state", default="KS")
    queries.add_argument("--county", required=True)
    queries.add_argument("--model", default=DEFAULT_MODEL)
    queries.add_argument("--fallback-model", default=None, help=argparse.SUPPRESS)
    queries.add_argument("--count", type=int, default=40)
    queries.set_defaults(func=cmd_county_queries)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
