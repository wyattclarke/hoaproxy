#!/usr/bin/env python3
"""LLM-assisted county backfill for stubborn GA `_unknown-county/` manifests.

Picks up where `scripts/ga_county_backfill.py` leaves off. For every
`_unknown-county/<slug>/` manifest where the heuristic backfill could not
infer a county:

  1. Re-run the heuristic (in case PDF text changed since last pass).
  2. If still unrouted AND a PDF exists, send a compact text snippet to
     DeepSeek (`deepseek/deepseek-v4-flash`, fallback `moonshotai/kimi-k2.6`)
     and ask for `(county, repaired_name)`.
  3. Validate the model's county against the canonical 159-county GA list.
  4. If both come back valid, GCS-rewrite the manifest under
     `gs://hoaproxy-bank/v1/GA/<county>/<repaired_slug>/...` (server-side
     copy + delete).

No prompt content includes secrets or document text beyond the snippet —
the snippet is the same first/last few pages used by the heuristic backfill.

Spend: roughly 1 model call per heuristic-failure manifest (~$0.01 each
on DeepSeek). At 250-350 stubborn manifests this is ~$2.50-3.50 total.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from google.cloud import storage as gcs  # noqa: E402

# Reuse the existing backfill helpers (county list, PDF text extraction,
# heuristic county inference, GCS copy/delete).
from ga_county_backfill import (  # noqa: E402
    BUCKET_NAME,
    GA_COUNTIES,
    UNKNOWN_PREFIX,
    copy_prefix,
    delete_prefix,
    extract_pdf_text,
    infer_county_from_city_in_text,
    infer_county_from_name_or_url,
    infer_county_from_text,
    list_unknown_county_manifests,
    update_manifest_county,
)

from hoaware.bank import slugify  # noqa: E402
from hoaware.model_usage import CallTimer, assert_discovery_model_allowed, log_llm_call  # noqa: E402


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
FALLBACK_MODEL = "moonshotai/kimi-k2.6"
TIMEOUT_SECONDS = 60

GA_COUNTY_SET = {c.lower(): c for c in GA_COUNTIES}


def client() -> OpenAI:
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("QA_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY or QA_API_KEY required")
    return OpenAI(
        api_key=key,
        base_url=os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL),
        timeout=float(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", TIMEOUT_SECONDS)),
        max_retries=0,
    )


def parse_json(text: str) -> dict:
    """Pull the first JSON object out of the model's reply, robustly."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def ask_model(orc: OpenAI, model: str, *, name: str, source_url: str | None,
              text_snippet: str) -> dict:
    """Single compact request: which Georgia county? what's the cleaner name?"""
    assert_discovery_model_allowed(model)
    prompt = {
        "task": "Identify the Georgia county and the canonical HOA name for this banked manifest.",
        "rules": [
            "Use only the snippet evidence; do not invent counties.",
            "If the snippet clearly indicates a Georgia county, return it (just the county name, no 'County' suffix).",
            "If the snippet is from another US state or you cannot tell the GA county, return county=null.",
            "Prefer the recorded-document county language (\"X County, Georgia\") over city mentions.",
            "Return a cleaned HOA name when the current name is malformed (e.g. starts with 'of', 'and', a section number, or boilerplate). Keep the existing name if it already looks like a real HOA name.",
            "Output JSON only: {\"county\": \"<name or null>\", \"repaired_name\": \"<name>\", \"confidence\": <0-1>, \"reason\": \"<short>\"}.",
        ],
        "current_name": name,
        "source_url": source_url,
        "snippet": text_snippet[:6000],
    }
    base_url = os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL)
    timer = CallTimer()
    try:
        response = orc.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Return valid JSON only. Be concise."},
                {"role": "user", "content": json.dumps(prompt, sort_keys=True)},
            ],
            temperature=0.1,
            max_tokens=400,
            response_format={"type": "json_object"},
            extra_body={"include_reasoning": False},
        )
    except Exception as exc:
        log_llm_call(
            operation="ga_backfill_with_llm.ask_model",
            model=model,
            api_base_url=base_url,
            status="error",
            error=str(exc),
            elapsed_ms=timer.elapsed_ms(),
            metadata={"slug": name[:80]},
        )
        raise
    log_llm_call(
        operation="ga_backfill_with_llm.ask_model",
        model=model,
        api_base_url=base_url,
        response=response,
        elapsed_ms=timer.elapsed_ms(),
        metadata={"slug": name[:80]},
    )
    return parse_json(response.choices[0].message.content or "{}")


def canonicalize_county(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip().strip('"\'').lower()
    cleaned = re.sub(r"\s+county\s*$", "", cleaned).strip()
    cleaned = re.sub(r",\s*georgia.*$", "", cleaned).strip()
    return GA_COUNTY_SET.get(cleaned)


def name_looks_clean(name: str) -> bool:
    if not name or len(name) < 4:
        return False
    low = name.strip().lower()
    bad_prefixes = (
        "of ", "and ", "a ", "by ", "the of ", "section ", "ated ",
        "laws of ", "by-laws of ", "consideration ", "all residents",
        "this ", "that ",
    )
    return not any(low.startswith(p) for p in bad_prefixes)


def process_manifest(
    orc: OpenAI,
    client_gcs: gcs.Client,
    manifest_blob: gcs.Blob,
    *,
    model: str,
    fallback_model: str,
    min_confidence: float,
    dry_run: bool,
) -> dict:
    bucket = client_gcs.bucket(BUCKET_NAME)
    name_parts = manifest_blob.name.split("/")
    if len(name_parts) < 5:
        return {"status": "skip_bad_path", "name": manifest_blob.name}
    hoa_slug = name_parts[3]
    old_prefix = "/".join(name_parts[:4])

    try:
        manifest = json.loads(manifest_blob.download_as_bytes())
    except Exception as exc:
        return {"status": "skip_bad_manifest", "name": manifest_blob.name, "error": str(exc)}

    name = manifest.get("name") or hoa_slug
    docs = manifest.get("documents") or []
    metadata_sources = manifest.get("metadata_sources") or []
    source_url = next(
        (s.get("source_url") for s in metadata_sources if s.get("source_url")),
        None,
    )

    # 1. Heuristic first.
    pdf_text = ""
    county: str | None = None
    if docs:
        for doc in docs[:3]:
            gcs_path = doc.get("gcs_path", "")
            if not gcs_path.startswith(f"gs://{BUCKET_NAME}/"):
                continue
            doc_blob = bucket.blob(gcs_path[len(f"gs://{BUCKET_NAME}/"):])
            if not doc_blob.exists():
                continue
            pdf_text = extract_pdf_text(doc_blob)
            county = infer_county_from_text(pdf_text)
            if county:
                break

    # Also check the manifest name + slug + source URL for an explicit
    # "X County" reference — many leads carry the county right in the
    # extracted-name boilerplate (e.g. "21, Chatham County, Georgia ..."
    # which got slugged to "21-chatham-county-georgia-...").
    if not county:
        haystack = " ".join(filter(None, [name, hoa_slug.replace("-", " "), source_url]))
        county = infer_county_from_text(haystack)

    if not county:
        county = infer_county_from_name_or_url(name, source_url)
    if not county and pdf_text:
        county = infer_county_from_city_in_text(pdf_text)

    repaired_name = name
    used_llm = False

    # 2. LLM fallback when heuristic still has nothing AND we have text to feed.
    if not county and pdf_text:
        used_llm = True
        try:
            data = ask_model(
                orc, model, name=name, source_url=source_url, text_snippet=pdf_text
            )
        except Exception:
            try:
                data = ask_model(
                    orc, fallback_model, name=name, source_url=source_url,
                    text_snippet=pdf_text,
                )
            except Exception as exc:
                return {
                    "status": "llm_error", "slug": hoa_slug, "error": str(exc)[:200],
                }
        confidence = float(data.get("confidence") or 0)
        if confidence < min_confidence:
            return {
                "status": "llm_low_confidence", "slug": hoa_slug,
                "confidence": confidence, "raw": data,
            }
        county = canonicalize_county(data.get("county"))
        candidate_name = (data.get("repaired_name") or "").strip()
        if name_looks_clean(candidate_name) and len(candidate_name) >= 4:
            repaired_name = candidate_name[:120]

    if not county:
        return {"status": "no_county", "slug": hoa_slug, "name": name[:60], "used_llm": used_llm}

    # Slug from possibly-repaired name.
    new_slug = slugify(repaired_name) or hoa_slug
    county_slug = slugify(county)
    new_prefix = f"v1/GA/{county_slug}/{new_slug}"
    if new_prefix == old_prefix:
        return {"status": "already_routed", "slug": hoa_slug, "county": county}

    new_manifest = bucket.blob(f"{new_prefix}/manifest.json")
    if new_manifest.exists():
        return {
            "status": "collision", "slug": hoa_slug, "county": county,
            "new_slug": new_slug, "name": repaired_name[:60],
        }

    if dry_run:
        return {
            "status": "dry_would_move", "slug": hoa_slug, "county": county,
            "new_slug": new_slug, "name": repaired_name[:60],
            "used_llm": used_llm,
        }

    copied = copy_prefix(client_gcs, old_prefix, new_prefix)
    new_gcs_paths: dict[str, str] = {}
    for old_blob in client_gcs.list_blobs(bucket, prefix=old_prefix + "/"):
        if old_blob.name.endswith("/original.pdf"):
            old_uri = f"gs://{BUCKET_NAME}/{old_blob.name}"
            new_uri = f"gs://{BUCKET_NAME}/{new_prefix}/{old_blob.name[len(old_prefix) + 1:]}"
            new_gcs_paths[old_uri] = new_uri

    update_manifest_county(client_gcs, new_prefix, county, new_gcs_paths)
    # Also rewrite the manifest's name field if we repaired it.
    if repaired_name != name:
        m_blob = bucket.blob(f"{new_prefix}/manifest.json")
        try:
            data = json.loads(m_blob.download_as_bytes())
            data["name"] = repaired_name
            m_blob.upload_from_string(
                json.dumps(data, indent=2, sort_keys=True),
                content_type="application/json",
            )
        except Exception:
            pass

    deleted = delete_prefix(client_gcs, old_prefix)
    return {
        "status": "moved", "slug": hoa_slug, "county": county,
        "new_slug": new_slug, "name": repaired_name[:60],
        "copied": len(copied), "deleted": deleted, "used_llm": used_llm,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM-assisted GA county backfill")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--fallback-model", default=FALLBACK_MODEL)
    parser.add_argument("--min-confidence", type=float, default=0.6)
    parser.add_argument("--llm-only", action="store_true",
                        help="Skip heuristic-only successes; only spend tokens on cases the heuristic still can't solve.")
    args = parser.parse_args()

    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "hoaware"

    orc = client()
    client_gcs = gcs.Client()
    manifests = list_unknown_county_manifests(client_gcs)
    if args.limit:
        manifests = manifests[: args.limit]
    print(f"Found {len(manifests)} _unknown-county manifests under v1/GA/", file=sys.stderr)

    summary: dict[str, int] = {}
    for i, blob in enumerate(manifests, 1):
        result = process_manifest(
            orc, client_gcs, blob,
            model=args.model,
            fallback_model=args.fallback_model,
            min_confidence=args.min_confidence,
            dry_run=args.dry_run,
        )
        summary[result["status"]] = summary.get(result["status"], 0) + 1
        print(json.dumps({"i": i, **result}))
    print(json.dumps({"summary": summary}, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
