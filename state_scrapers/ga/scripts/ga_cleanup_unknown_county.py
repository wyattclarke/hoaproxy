#!/usr/bin/env python3
"""Post-hoc cleanup for the GA `_unknown-county/` manifests that survived
the heuristic + LLM-on-snippet backfill passes.

Strategy per manifest:

  1. Read manifest (name, source URL, banked PDFs).
  2. If name is malformed (matches bad-prefix patterns like "of ...",
     "and ...", "a ...", section references, all-caps boilerplate),
     ask DeepSeek to propose a clean name from name + URL + filename.
  3. Re-run the existing heuristic county detector against (name + slug
     + source URL + first PDF's text). If a county pops out, use it.
  4. If county still missing, do a single Serper search
     "<repaired_name> Georgia HOA <county-hint-if-any>" and scan the
     organic results' titles + snippets + URLs for an explicit
     "<County> County, Georgia" pattern or for any known GA city
     (using the same city→county map the heuristic backfill uses).
  5. If we now have a county, GCS-rewrite the manifest under
     v1/GA/<county>/<repaired_slug>/ (server-side copy, then delete
     old prefix). Update manifest.address.county and manifest.name.

Cost notes:
- ~$0.005-0.015 per LLM name-repair call (DeepSeek, only fired if the
  current name looks malformed).
- ~$0.001 per Serper call (only fired when both heuristic and LLM
  don't surface a county).
- Worst case for the full 309-manifest GA backlog: $3-4 in OpenRouter
  + $0.30 in Serper. Well under the remaining $2.31 OpenRouter budget
  if we cap LLM repairs to obviously-malformed names.

Idempotent: re-running on already-routed manifests is a no-op.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from google.cloud import storage as gcs  # noqa: E402

# Reuse the heuristic helpers (county list, city map, PDF text extract,
# GCS copy/delete, manifest update).
from ga_county_backfill import (  # noqa: E402
    BUCKET_NAME,
    CITY_TO_COUNTY,
    GA_COUNTIES,
    UNKNOWN_PREFIX,
    copy_prefix,
    delete_prefix,
    extract_pdf_text,
    infer_county_from_city_in_text,
    infer_county_from_text,
    list_unknown_county_manifests,
    update_manifest_county,
)

from hoaware.bank import slugify  # noqa: E402
from hoaware.model_usage import CallTimer, assert_discovery_model_allowed, log_llm_call  # noqa: E402


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
FALLBACK_MODEL = "moonshotai/kimi-k2.6"
SERPER_ENDPOINT = "https://google.serper.dev/search"
USER_AGENT = (
    os.environ.get("HOA_DISCOVERY_USER_AGENT")
    or "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
)
TIMEOUT_SECONDS = 60

GA_COUNTY_SET = {c.lower(): c for c in GA_COUNTIES}

# A name "looks malformed" if it starts with these tokens, contains a
# section-reference, or is too short. These are the patterns the
# legal-phrase cleaner produced when extracting names from PDF text.
_BAD_NAME_PREFIXES = (
    "of ", "and ", "a ", "an ", "by ", "the of ", "section ", "ated ",
    "laws of ", "by-laws of ", "consideration ", "all residents",
    "this ", "that ", "amount ", "article ",
    "agree that ", "ation ", "is made ", "is filed ", "below ",
    "city design ", "in atlanta ", "you will ",
)
_BAD_NAME_TOKENS_RE = re.compile(
    r"\b(section\s+\d|nonprofit\s+corporation|hereinafter|page\s*\d|article\s+[ivx]+\b|"
    r"chapters?\s+\d|protective\s+state\s+of)\b",
    re.IGNORECASE,
)


def name_looks_malformed(name: str) -> bool:
    if not name or len(name) < 4 or len(name) > 120:
        return True
    low = name.strip().lower()
    if any(low.startswith(p) for p in _BAD_NAME_PREFIXES):
        return True
    if _BAD_NAME_TOKENS_RE.search(name):
        return True
    return False


def name_looks_clean(name: str) -> bool:
    if not name or len(name) < 4 or len(name) > 120:
        return False
    return not name_looks_malformed(name)


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
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def repair_name_with_llm(orc: OpenAI, model: str, *, name: str,
                         source_url: str | None, filename: str | None,
                         text_snippet: str) -> dict:
    """Single compact request: clean the HOA name and attempt a county."""
    assert_discovery_model_allowed(model)
    prompt = {
        "task": "Clean up this banked GA HOA's name and, if possible, identify the Georgia county.",
        "rules": [
            "Return the canonical mandatory-HOA name. Strip boilerplate like leading 'of', 'and', 'a', 'agree that', 'amount of', 'this Second to', 'article III', 'section', 'all residents', 'is filed in', etc.",
            "Strip 'a Georgia nonprofit corporation' and similar trailing legal boilerplate.",
            "Preserve real community/road names (Magnolia Ridge, Cottages on Carter, Wellington Walk, etc.).",
            "If the snippet/URL/filename gives a Georgia county, return it; otherwise null.",
            "If you cannot recover a real HOA name, return null. Don't invent.",
            "Output JSON only: {\"repaired_name\": \"<name or null>\", \"county\": \"<GA county name without 'County' or null>\", \"confidence\": <0-1>, \"reason\": \"<short>\"}.",
        ],
        "current_name": name,
        "source_url": source_url,
        "filename": filename,
        "snippet": (text_snippet or "")[:6000],
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
            operation="ga_cleanup_unknown_county.repair_name",
            model=model,
            api_base_url=base_url,
            status="error",
            error=str(exc),
            elapsed_ms=timer.elapsed_ms(),
            metadata={"current_name": name[:80]},
        )
        raise
    log_llm_call(
        operation="ga_cleanup_unknown_county.repair_name",
        model=model,
        api_base_url=base_url,
        response=response,
        elapsed_ms=timer.elapsed_ms(),
        metadata={"current_name": name[:80]},
    )
    return parse_json(response.choices[0].message.content or "{}")


def serper_county_lookup(name: str, *, num: int = 10) -> str | None:
    """Last-ditch: search '<name> Georgia HOA' and scan organic results
    for an explicit GA county or known city. Returns the canonical county
    or None.
    """
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        return None
    query = f'"{name}" Georgia HOA county'
    try:
        response = requests.post(
            SERPER_ENDPOINT,
            headers={"X-API-KEY": key, "Content-Type": "application/json", "User-Agent": USER_AGENT},
            json={"q": query, "num": num, "gl": "us", "hl": "en"},
            timeout=20,
        )
    except requests.RequestException:
        return None
    if response.status_code >= 400:
        return None
    rows = response.json().get("organic", [])
    blob = " ".join(
        " ".join([r.get("title") or "", r.get("snippet") or "", r.get("link") or ""])
        for r in rows
    )
    # Try the heuristic patterns (works on titles+snippets too).
    county = infer_county_from_text(blob)
    if county:
        return county
    return infer_county_from_city_in_text(blob)


def canonicalize_county(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().strip('"\'').lower()
    s = re.sub(r"\s+county\s*$", "", s).strip()
    s = re.sub(r",\s*georgia.*$", "", s).strip()
    return GA_COUNTY_SET.get(s)


def best_filename(manifest: dict) -> str | None:
    for doc in manifest.get("documents") or []:
        fn = doc.get("filename")
        if fn:
            return fn
    return None


def process_manifest(
    orc: OpenAI,
    client_gcs: gcs.Client,
    manifest_blob: gcs.Blob,
    *,
    model: str,
    fallback_model: str,
    use_serper: bool,
    min_llm_confidence: float,
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

    # Pull a text snippet if any PDF is text-extractable.
    pdf_text = ""
    if docs:
        for doc in docs[:3]:
            gcs_path = doc.get("gcs_path", "")
            if not gcs_path.startswith(f"gs://{BUCKET_NAME}/"):
                continue
            doc_blob = bucket.blob(gcs_path[len(f"gs://{BUCKET_NAME}/"):])
            if doc_blob.exists():
                pdf_text = extract_pdf_text(doc_blob)
                if pdf_text:
                    break

    # 1. Heuristic county detection over name + slug + URL + PDF text.
    county = None
    haystack = " ".join(filter(None, [
        name, hoa_slug.replace("-", " "), source_url, pdf_text[:8000],
    ]))
    county = infer_county_from_text(haystack)
    if not county and pdf_text:
        county = infer_county_from_city_in_text(pdf_text)
    if not county:
        for city in sorted(CITY_TO_COUNTY.keys(), key=lambda s: -len(s)):
            if re.search(rf"\b{re.escape(city)}\b", haystack.lower()):
                county = CITY_TO_COUNTY[city]
                break

    repaired_name = name
    used_llm = False
    used_serper = False

    # 2. LLM name-repair only when the current name looks malformed.
    if name_looks_malformed(name):
        used_llm = True
        try:
            data = repair_name_with_llm(
                orc, model,
                name=name, source_url=source_url,
                filename=best_filename(manifest),
                text_snippet=pdf_text,
            )
        except Exception:
            try:
                data = repair_name_with_llm(
                    orc, fallback_model,
                    name=name, source_url=source_url,
                    filename=best_filename(manifest),
                    text_snippet=pdf_text,
                )
            except Exception as exc:
                return {"status": "llm_error", "slug": hoa_slug, "error": str(exc)[:200]}
        confidence = float(data.get("confidence") or 0)
        candidate_name = (data.get("repaired_name") or "").strip()
        if confidence >= min_llm_confidence and name_looks_clean(candidate_name):
            repaired_name = candidate_name[:120]
        if not county:
            county = canonicalize_county(data.get("county"))

    # 3. Serper address lookup as a last resort.
    if not county and use_serper and name_looks_clean(repaired_name):
        used_serper = True
        county = serper_county_lookup(repaired_name)

    if not county:
        return {
            "status": "no_county", "slug": hoa_slug, "name": repaired_name[:60],
            "used_llm": used_llm, "used_serper": used_serper,
        }

    # 4. Re-bank under the corrected slug + county.
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
            "used_llm": used_llm, "used_serper": used_serper,
        }

    if dry_run:
        return {
            "status": "dry_would_move", "slug": hoa_slug, "county": county,
            "new_slug": new_slug, "name": repaired_name[:60],
            "used_llm": used_llm, "used_serper": used_serper,
        }

    copy_prefix(client_gcs, old_prefix, new_prefix)
    new_gcs_paths: dict[str, str] = {}
    for old_blob in client_gcs.list_blobs(bucket, prefix=old_prefix + "/"):
        if old_blob.name.endswith("/original.pdf"):
            old_uri = f"gs://{BUCKET_NAME}/{old_blob.name}"
            new_uri = f"gs://{BUCKET_NAME}/{new_prefix}/{old_blob.name[len(old_prefix) + 1:]}"
            new_gcs_paths[old_uri] = new_uri

    update_manifest_county(client_gcs, new_prefix, county, new_gcs_paths)
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

    delete_prefix(client_gcs, old_prefix)
    return {
        "status": "moved", "slug": hoa_slug, "county": county,
        "new_slug": new_slug, "name": repaired_name[:60],
        "used_llm": used_llm, "used_serper": used_serper,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Post-hoc cleanup for GA _unknown-county/ manifests")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--fallback-model", default=FALLBACK_MODEL)
    parser.add_argument("--min-llm-confidence", type=float, default=0.6)
    parser.add_argument("--no-serper", action="store_true",
                        help="Skip the Serper address-lookup fallback.")
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
            use_serper=not args.no_serper,
            min_llm_confidence=args.min_llm_confidence,
            dry_run=args.dry_run,
        )
        summary[result["status"]] = summary.get(result["status"], 0) + 1
        print(json.dumps({"i": i, **result}))
    print(json.dumps({"summary": summary}, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
