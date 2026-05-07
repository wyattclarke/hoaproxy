"""Clean up dirty HOA names on the live site.

For each live GA HOA whose name looks like an OCR fragment ("LAWS OF -
Keystone ... These By-Laws are by-laws of the Buckhead Homeowners
Association"), pull the first ~3000 chars of indexed OCR text and ask
DeepSeek (Kimi fallback) to extract the canonical HOA name.

Outputs a rename map JSONL (dry-run) and optionally POSTs it to
/admin/rename-hoa with merge-on-collision.

Usage:
  python state_scrapers/ga/scripts/clean_dirty_hoa_names.py \
    --state GA --limit 200

Add --apply to actually rename.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

BASE_URL = "https://hoaproxy.org"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
FALLBACK_MODEL = "moonshotai/kimi-k2.6"

# ---------------------------------------------------------------------------
# Dirty-name detection
# ---------------------------------------------------------------------------

_BAD_PREFIX = re.compile(
    r"^(?:by-?laws?|declarations?|articles?|covenants?|deed|amendment|supplement|"
    r"plat|this |consideration|common|accordance|city of|county of|members of|"
    r"voting|the property|is the |a homeowners|page |section |exhibit|schedule|"
    r"such as|in addition|unless |any |all |or other|or by|a typical|recorded |"
    r"submitted |squarespace|attachment |appendix |of for |of to |amended and )",
    re.I,
)

# Phrases that almost always indicate a doc-title fragment leaked into the
# HOA name — even when they appear mid-string (caught the Lake Laceola case).
_DOC_FRAGMENT_RE = re.compile(
    r"\b(?:exhibit\s+[A-Za-z](?:\b|-\d)|"
    r"supplemental\s+dec(?:laration)?|amended\s+and\s+restated|"
    r"architectural\s+design\s+guidelines?|"
    r"declaration\s+of(?:\s+covenants)?|"
    r"by-?laws\s+of|articles\s+of\s+incorporation\s+of|"
    r"protective\s+covenants?|wetland(?:-|\s)mitigation)\b",
    re.I,
)

# Trailing "<stop word> HOA" — names like "Bridgeberry Amenity and HOA" where
# OCR truncated mid-phrase and "HOA" got stuck on the end.
_TAIL_TRUNCATION_RE = re.compile(
    r"\b(?:and|or|of|to|the|for|with|on|in|by|both)\s+HOA$", re.I
)

# "<County> County OF.<Name>" or "<County> County of <Name>" — county leaked
# in as a prefix (e.g. "Gwinnett County OF. FAITH HOLLOW Homeowners …").
_COUNTY_PREFIX_RE = re.compile(
    r"^[A-Z][a-z]+\s+County\s+(?:of|OF\.?)\s+", re.I
)

# Doubled-name pattern: "<X> POA <X-CAPS> PROPERTY OWNERS ASSOCIATION".
_DOUBLED_NAME_RE = re.compile(
    r"\b(POA|HOA)\s+[A-Z][A-Z &]{3,}\s+(?:PROPERTY|HOMEOWNERS|OWNERS)\s+ASSOCIATION\b"
)


def is_dirty(name: str) -> tuple[bool, str]:
    n = name or ""
    if " - " in n and len(n) > 50:
        return True, "long_dashed_phrase"
    if n[:1].islower():
        return True, "starts_lowercase"
    if re.match(r"^\d+\s*[-)]\s*", n):
        return True, "numeric_prefix"
    # Year prefix like "2018 Exhibit A …" or "1985 Amended Bylaws of …"
    if re.match(r"^(?:19|20)\d{2}\s+[A-Za-z]", n):
        return True, "year_prefix"
    # Long digit run prefix like "5021942267194390towne park pooler"
    if re.match(r"^\d{6,}", n):
        return True, "longdigit_prefix"
    # Street-address prefix like "6318 Suwanee Dam Rd HOA"
    if re.match(
        r"^\d+\s+[A-Z][a-z]+(?:\s+[A-Z][a-zA-Z]+){0,4}\s+"
        r"(?:Rd|Road|St|Street|Ave|Avenue|Dr|Drive|Lane|Ln|Way|Cir|"
        r"Ct|Court|Blvd|Boulevard|Place|Pl|Pkwy|Parkway|Trail|Tr|Hwy|Highway)\b",
        n,
        re.I,
    ):
        return True, "street_address_prefix"
    if re.match(r"^[A-Z][A-Z &\-]{3,}\s+", n) and len(n) > 40:
        return True, "shouting_prefix"
    if len(n) <= 4 and not re.search(r"hoa|poa", n, re.I):
        return True, "too_short"
    if _BAD_PREFIX.match(n):
        return True, "stopword_prefix"
    if _COUNTY_PREFIX_RE.match(n):
        return True, "county_prefix"
    if _DOC_FRAGMENT_RE.search(n):
        return True, "doc_fragment_anywhere"
    if _TAIL_TRUNCATION_RE.search(n):
        return True, "tail_truncation"
    if _DOUBLED_NAME_RE.search(n):
        return True, "doubled_name"
    # Garbled hyphenated acronym like "GL-LB-BAR HOA"
    if re.search(r"\b[A-Z]{2,}-[A-Z]{2,}-[A-Z]{2,}\b", n):
        return True, "garbled_acronym"
    if len(n) > 70:
        return True, "very_long"
    if re.search(r"\bbook \d|page \d|paragraph", n, re.I):
        return True, "citation_in_name"
    if re.search(r"\bcc&?rs?\b", n, re.I) and len(n) > 30:
        return True, "ccr_in_name_long"
    return False, ""


# ---------------------------------------------------------------------------
# Live API helpers
# ---------------------------------------------------------------------------


def _live_admin_token() -> str | None:
    if os.environ.get("HOAPROXY_ADMIN_BEARER"):
        return os.environ["HOAPROXY_ADMIN_BEARER"]
    api_key = os.environ.get("RENDER_API_KEY")
    service_id = os.environ.get("RENDER_SERVICE_ID")
    if api_key and service_id:
        try:
            r = requests.get(
                f"https://api.render.com/v1/services/{service_id}/env-vars",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            r.raise_for_status()
            for env in r.json():
                e = env.get("envVar", env)
                if e.get("key") == "JWT_SECRET" and e.get("value"):
                    return e["value"]
        except requests.RequestException:
            pass
    return os.environ.get("JWT_SECRET")


def _fetch_summaries(base_url: str, state: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        r = requests.get(
            f"{base_url}/hoas/summary",
            params={"state": state, "limit": 500, "offset": offset},
            timeout=120,
        )
        r.raise_for_status()
        payload = r.json()
        batch = payload.get("results") or []
        rows.extend(batch)
        if len(rows) >= int(payload.get("total") or 0) or not batch:
            return rows
        offset += len(batch)


def _fetch_doc_text(base_url: str, hoa: str, max_chars: int = 3500) -> str:
    docs = requests.get(
        f"{base_url}/hoas/{requests.utils.quote(hoa, safe='')}/documents",
        timeout=60,
    )
    if not docs.ok:
        return ""
    paths = [
        d.get("relative_path") or d.get("path")
        for d in (docs.json() or [])
        if d.get("relative_path") or d.get("path")
    ]
    if not paths:
        return ""
    rendered = requests.get(
        f"{base_url}/hoas/{requests.utils.quote(hoa, safe='')}/documents/searchable",
        params={"path": paths[0]},
        timeout=120,
    )
    if not rendered.ok:
        return ""
    pre = re.findall(r"<pre>(.*?)</pre>", rendered.text, flags=re.S | re.I)
    text = "\n".join(html.unescape(re.sub(r"<[^>]+>", " ", part)) for part in pre)
    return text[:max_chars]


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


def _llm_client() -> OpenAI:
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("QA_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY or QA_API_KEY is required")
    return OpenAI(
        api_key=key,
        base_url=os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL),
        timeout=float(os.environ.get("OPENROUTER_TIMEOUT_SECONDS", "60")),
        max_retries=0,
    )


SYSTEM = (
    "You normalize HOA / condominium / property-owners-association names. "
    "Given a possibly-garbled current name plus the first chunk of the HOA's "
    "governing-document OCR text, return the canonical legal name of the "
    "association. Prefer the exact phrase that appears in the document body "
    "(e.g. 'Buckhead Homeowners Association', 'Cumberland Harbour "
    "Association, Inc.'). If the document does not clearly state an HOA name, "
    "return null. Do not invent a name from a street address or a city. Strip "
    "OCR fragments, page headers, all-caps shouting, and document titles like "
    "'BY-LAWS OF', 'DECLARATION OF', 'ARTICLES OF INCORPORATION OF'."
)


def _prompt(name: str, text: str) -> str:
    return (
        f"current_name: {name!r}\n\n"
        f"document_excerpt:\n{text or '(none)'}\n\n"
        "Return strict JSON: {\"canonical_name\": <string or null>, "
        "\"confidence\": <0-1>, \"reason\": <short string>}. "
        "Do not include any text outside the JSON object."
    )


_HOA_SUFFIX_RE = re.compile(
    r"\b("
    r"homeowners(?:'?\s|\s+)?association(?:,?\s+inc\.?)?|"
    r"homes\s+association|home\s+owners\s+association|"
    r"property\s+owners(?:'?\s+|\s+)association(?:,?\s+inc\.?)?|"
    r"owners\s+association(?:,?\s+inc\.?)?|"
    r"community\s+association|"
    r"condominium\s+association(?:,?\s+inc\.?)?|"
    r"condominium\s+owners\s+association|"
    r"townhome\s+association|"
    r"hoa|poa"
    r")\b\.?\s*$",
    re.I,
)

# Common doc-title / OCR-noise prefixes that prepend the real name.
_PREFIX_NOISE_RE = re.compile(
    r"^("
    r"(?:19|20)\d{2}\s+(?:exhibit\s+[a-z]\s+)?(?:supplemental\s+)?dec(?:laration)?\s+|"
    r"(?:19|20)\d{2}\s+exhibit\s+[a-z]\s+|"
    r"(?:19|20)\d{2}\s+(?:amended|restated|amended\s+and\s+restated)\s+|"
    r"and\s+restated(?:\s*-\s*|\s+)|"
    r"amended\s+and\s+restated\s+|"
    r"squarespace\s*\.?\s*of\.?\s*|"
    r"squarespace\s*[-.]\s*|"
    r"architectural\s+design\s+guidelines?\s+(?:for\s+)?|"
    r"design\s+guidelines?\s+(?:for\s+)?|"
    r"declaration\s+of(?:\s+covenants(?:,?\s+conditions(?:,?\s+and\s+restrictions)?)?\s+)?(?:for\s+|of\s+)?|"
    r"by-?laws\s+of\s+(?:the\s+)?|"
    r"articles\s+of\s+incorporation\s+of\s+(?:the\s+)?|"
    r"protective\s+covenants?\s+(?:for\s+|of\s+)?|"
    r"supplemental\s+declaration\s+(?:for\s+|of\s+)?|"
    r"\d{6,}|"
    r"[A-Z][a-z]+\s+county\s+(?:of|OF\.?)\s+"
    r")",
    re.I,
)


def _try_strip_prefix(name: str) -> str | None:
    """Deterministically peel off doc-title noise. Returns the cleaned
    name only when (a) the original has a recognized HOA-shaped suffix
    and (b) something clearly junk-like was actually stripped from the
    front. Returns None if no safe strip is possible."""
    n = (name or "").strip()
    if not _HOA_SUFFIX_RE.search(n):
        # No HOA suffix to anchor — leave to the LLM.
        return None
    cleaned = n
    changed = False
    for _ in range(4):
        m = _PREFIX_NOISE_RE.match(cleaned)
        if not m:
            break
        cleaned = cleaned[m.end():].lstrip(" -.,;:")
        changed = True
    if not changed:
        return None
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -.,;:")
    if len(cleaned) < 4:
        return None
    return cleaned


def _ask_llm(client: OpenAI, model: str, name: str, text: str) -> dict[str, Any] | None:
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": _prompt(name, text)},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        return {"_error": str(exc)}
    choices = getattr(resp, "choices", None) or []
    if not choices:
        return {"_error": "empty_choices"}
    raw = (choices[0].message.content or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {"_error": "invalid_json", "raw": raw[:200]}


def _looks_canonical(name: str | None) -> bool:
    if not name or not isinstance(name, str):
        return False
    n = name.strip()
    if len(n) < 4 or len(n) > 90:
        return False
    if is_dirty(n)[0]:
        return False
    if not re.search(
        r"\b(association|hoa|poa|condominium|community|estates|homeowners|"
        r"property\s+owners|owners|villas?|townhomes?|club|residences?|inc\.?)\b",
        n,
        re.I,
    ):
        return False
    return True


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _normalize_for_compare(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", default="GA")
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--out", default="state_scrapers/ga/results/hoa_name_renames.jsonl")
    p.add_argument("--limit", type=int, default=0, help="0 = no limit")
    p.add_argument("--min-confidence", type=float, default=0.7)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--fallback-model", default=FALLBACK_MODEL)
    p.add_argument("--max-text-chars", type=int, default=3500)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--sleep-s", type=float, default=0.05)
    args = p.parse_args()

    client = _llm_client()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    summaries = _fetch_summaries(args.base_url, args.state.upper())
    dirty = []
    for r in summaries:
        ok, why = is_dirty(r.get("hoa") or "")
        if ok:
            dirty.append((r, why))
    if args.limit:
        dirty = dirty[: args.limit]
    print(f"dirty HOAs to process: {len(dirty)}", file=sys.stderr)

    decisions: list[dict[str, Any]] = []
    seen_names: set[str] = {(r.get("hoa") or "") for r in summaries}
    propose_renames: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for i, (row, why) in enumerate(dirty, 1):
        old = row.get("hoa") or ""
        # Fast path: try a deterministic prefix-strip first. If we get a
        # name that already looks canonical we can skip the LLM call,
        # which both saves money and avoids the LLM's tendency to
        # decline a perfectly-fine in-name suffix as "unconfirmed".
        stripped = _try_strip_prefix(old)
        if stripped and _looks_canonical(stripped) and stripped != old:
            decision = {
                "hoa_id": row.get("hoa_id"),
                "old_name": old,
                "dirty_reason": why,
                "canonical_name": stripped,
                "confidence": 0.95,
                "llm_reason": "deterministic_prefix_strip",
                "doc_count": row.get("doc_count"),
                "chunk_count": row.get("chunk_count"),
            }
            decisions.append(decision)
            propose_renames.append(
                {"hoa_id": row.get("hoa_id"), "new_name": stripped}
            )
            if i % 10 == 0:
                print(f"  processed {i}/{len(dirty)}", file=sys.stderr)
                out_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
            continue
        text = _fetch_doc_text(args.base_url, old, max_chars=args.max_text_chars)
        ans = _ask_llm(client, args.model, old, text)
        if ans is None or ans.get("_error"):
            ans = _ask_llm(client, args.fallback_model, old, text) or {}
        canonical = (ans or {}).get("canonical_name")
        confidence = float((ans or {}).get("confidence") or 0)
        reason = (ans or {}).get("reason") or (ans or {}).get("_error") or ""
        decision = {
            "hoa_id": row.get("hoa_id"),
            "old_name": old,
            "dirty_reason": why,
            "canonical_name": canonical,
            "confidence": confidence,
            "llm_reason": reason,
            "doc_count": row.get("doc_count"),
            "chunk_count": row.get("chunk_count"),
        }
        decisions.append(decision)
        if (
            _looks_canonical(canonical)
            and confidence >= args.min_confidence
            and _normalize_for_compare(canonical) != _normalize_for_compare(old)
        ):
            propose_renames.append(
                {"hoa_id": row.get("hoa_id"), "new_name": canonical.strip()}
            )
        else:
            skipped.append(decision)
        if i % 10 == 0:
            print(f"  processed {i}/{len(dirty)}", file=sys.stderr)
            # incremental flush so a mid-run crash doesn't lose progress
            out_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
        time.sleep(args.sleep_s)

    out_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
    print(f"wrote decisions: {out_path}", file=sys.stderr)
    print(
        json.dumps(
            {
                "scanned": len(dirty),
                "proposed_renames": len(propose_renames),
                "skipped": len(skipped),
            },
            sort_keys=True,
        )
    )

    if args.apply and propose_renames:
        token = _live_admin_token()
        if not token:
            print("no admin token; skipping apply", file=sys.stderr)
            return 1
        # batch in chunks of 100 to avoid huge requests
        applied_total = 0
        merged_total = 0
        errs_total = 0
        for start in range(0, len(propose_renames), 100):
            chunk = propose_renames[start : start + 100]
            r = requests.post(
                f"{args.base_url}/admin/rename-hoa",
                headers={"Authorization": f"Bearer {token}"},
                json={"renames": chunk},
                timeout=180,
            )
            r.raise_for_status()
            payload = r.json()
            applied_total += int(payload.get("renamed") or 0)
            merged_total += int(payload.get("merged") or 0)
            errs_total += int(payload.get("errors") or 0)
            print(
                f"applied chunk {start//100 + 1}: renamed={payload.get('renamed')}"
                f" merged={payload.get('merged')} errors={payload.get('errors')}",
                file=sys.stderr,
            )
        print(
            json.dumps(
                {
                    "applied": True,
                    "renamed": applied_total,
                    "merged": merged_total,
                    "errors": errs_total,
                },
                sort_keys=True,
            )
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
