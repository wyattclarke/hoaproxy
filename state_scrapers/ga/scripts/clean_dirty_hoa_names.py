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
import signal
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
# OpenRouter API key is gateway-restricted to DEFAULT_MODEL. The historical
# Kimi K2 fallback is no longer permitted, so retries re-hit the primary.
FALLBACK_MODEL = DEFAULT_MODEL


class OperationTimeout(BaseException):
    """Raised by SIGALRM so broad HTTP exception handlers do not swallow it."""


def _raise_timeout(signum: int, frame: Any) -> None:
    raise OperationTimeout("phase10 row operation timed out")


def _set_alarm(seconds: int) -> None:
    if hasattr(signal, "SIGALRM"):
        signal.alarm(seconds)


def _clear_alarm() -> None:
    if hasattr(signal, "SIGALRM"):
        signal.alarm(0)

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


# Use the canonical is_dirty from hoaware.name_utils so this script gets all
# the same rules (leading_punctuation, leading_conjunction, project_code_prefix,
# generic_single_stem, …) — not just the subset that was forked here.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root
from hoaware.name_utils import is_dirty  # noqa: E402


# ---------------------------------------------------------------------------
# Live API helpers
# ---------------------------------------------------------------------------


def _live_admin_token() -> str | None:
    # Render env-vars fallback removed 2026-05-16 (Hetzner cutover).
    return os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")


def _fetch_summaries(base_url: str, state: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        # Retry on transient 5xx / connection errors. Render occasionally
        # 502s under load; without retries the rename pass crashes mid-run
        # and Phase 10 reports all-"no_candidates" which would silently
        # skip cleanup of newly imported entries.
        last_err = None
        payload = None
        for attempt in range(6):
            try:
                r = requests.get(
                    f"{base_url}/hoas/summary",
                    params={"state": state, "limit": 500, "offset": offset},
                    timeout=120,
                )
                if r.status_code == 200:
                    payload = r.json()
                    last_err = None
                    break
                last_err = f"http {r.status_code}: {r.text[:160]}"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(min(15 + 10 * attempt, 60))
        if last_err:
            raise RuntimeError(f"_fetch_summaries failed after retries: {last_err}")
        batch = payload.get("results") or []
        rows.extend(batch)
        if len(rows) >= int(payload.get("total") or 0) or not batch:
            return rows
        offset += len(batch)


def _fetch_doc_text(base_url: str, hoa: str, max_chars: int = 3500) -> str:
    quoted = requests.utils.quote(hoa, safe="")
    # Retry transient 5xx on the docs-list call. Empty result here means
    # phase10_close.py treats the entry as `skip_delete=True` and
    # preserves it — we'd rather pay 2 retries than lose a real HOA on a
    # 502 spike.
    docs = None
    for attempt in range(3):
        try:
            r = requests.get(f"{base_url}/hoas/{quoted}/documents", timeout=60)
            if r.status_code == 200:
                docs = r
                break
            if r.status_code in (404, 410):
                return ""  # genuinely no docs / renamed away
        except Exception:
            pass
        time.sleep(5 + 5 * attempt)
    if docs is None:
        return ""
    paths = [
        d.get("relative_path") or d.get("path")
        for d in (docs.json() or [])
        if d.get("relative_path") or d.get("path")
    ]
    if not paths:
        return ""
    rendered = None
    for attempt in range(3):
        try:
            r = requests.get(
                f"{base_url}/hoas/{quoted}/documents/searchable",
                params={"path": paths[0]}, timeout=120,
            )
            if r.status_code == 200:
                rendered = r
                break
            if r.status_code in (404, 410):
                return ""
        except Exception:
            pass
        time.sleep(5 + 5 * attempt)
    if rendered is None:
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
    "You normalize HOA / condominium / property-owners-association names "
    "AND validate that each entry is actually a homeowner / community "
    "association. Given a possibly-garbled current name plus the first "
    "chunk of the document's OCR text, return strict JSON.\n\n"
    "Rules:\n"
    "  is_hoa: true only when the document is a governing document "
    "(declaration, CC&Rs, bylaws, articles of incorporation, amendment, "
    "master deed, supplemental declaration, condominium declaration, "
    "cooperative bylaws, restrictive covenants) of a community that "
    "governs real property by recorded covenants — i.e. an HOA, condo "
    "association, cooperative, townhome association, property-owners "
    "association, master association, or homeowners-association equivalent. "
    "Set is_hoa=FALSE for: medical / professional / trade societies (e.g. "
    "'Dermatological Association'), water utilities / irrigation districts, "
    "municipal or state agencies (parks-and-recreation associations, "
    "redevelopment authorities), court filings / lawsuits / opinions, "
    "news / press / blog / marketing / real-estate listings, "
    "recording-stamp text fragments (e.g. 'Recorder of … County'), "
    "plat-page extracts, generic legal-text fragments, OCR garbage. "
    "An entity name containing the word 'Association' is NOT enough — "
    "the doc text must show the entity governs a residential community.\n\n"
    "  canonical_name: the canonical legal name of the association as it "
    "appears in the document body (e.g. 'Buckhead Homeowners Association', "
    "'Cumberland Harbour Association, Inc.'). Strip OCR fragments, page "
    "headers, all-caps shouting, and doc titles like 'BY-LAWS OF', "
    "'DECLARATION OF', 'ARTICLES OF INCORPORATION OF'. Do not invent a "
    "name from a street address or a city. Return null when the document "
    "does not clearly state an HOA name OR when is_hoa=false.\n\n"
    "  city: the city / town where the community is located, as stated "
    "in the document. Null if not present.\n\n"
    "  state: the two-letter US state code where the community is "
    "located, as stated in the document (e.g. 'GA', 'NV', 'AR'). Null if "
    "not present. IMPORTANT: this may differ from the state the entry is "
    "currently filed under — return what the document actually says. "
    "Cross-state mis-attribution (banked HOA whose docs are from another "
    "state) is one of the things this validation pass detects.\n\n"
    "  county: the county or parish where the community is located, as "
    "stated in the document. Null if not present.\n\n"
    "  confidence: 0–1 score of overall judgment.\n\n"
    "  reason: short string explaining the decision."
)


def _prompt(name: str, text: str) -> str:
    return (
        f"current_name: {name!r}\n\n"
        f"document_excerpt:\n{text or '(none)'}\n\n"
        "Return strict JSON: {\"is_hoa\": <bool>, "
        "\"canonical_name\": <string or null>, "
        "\"city\": <string or null>, "
        "\"state\": <2-letter string or null>, "
        "\"county\": <string or null>, "
        "\"confidence\": <0-1>, "
        "\"reason\": <short string>}. "
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
    p.add_argument(
        "--no-dirty-filter", action="store_true",
        help="Run the LLM rename pass over every live HOA, not just is_dirty()"
        " hits. Use after a state run where bank-stage name extraction was weak"
        " (Tier 0 keyword-Serper without SoS leads, etc.)."
    )
    args = p.parse_args()

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _raise_timeout)

    client = _llm_client()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    summaries = _fetch_summaries(args.base_url, args.state.upper())
    dirty = []
    for r in summaries:
        if args.no_dirty_filter:
            dirty.append((r, "no_dirty_filter"))
            continue
        ok, why = is_dirty(r.get("hoa") or "")
        if ok:
            dirty.append((r, why))
    if args.limit:
        dirty = dirty[: args.limit]
    print(f"HOAs to process: {len(dirty)} (no_dirty_filter={args.no_dirty_filter})", file=sys.stderr)

    decisions: list[dict[str, Any]] = []
    processed_keys: set[tuple[int | None, str]] = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                decision = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(decision, dict):
                continue
            decisions.append(decision)
            processed_keys.add((decision.get("hoa_id"), decision.get("old_name") or ""))

    seen_names: set[str] = {(r.get("hoa") or "") for r in summaries}
    propose_renames: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for decision in decisions:
        old = decision.get("old_name") or ""
        canonical = decision.get("canonical_name")
        try:
            confidence = float(decision.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if (
            decision.get("is_hoa") is not False
            and _looks_canonical(canonical)
            and confidence >= args.min_confidence
            and _normalize_for_compare(canonical) != _normalize_for_compare(old)
        ):
            propose_renames.append(
                {"hoa_id": decision.get("hoa_id"), "new_name": canonical.strip()}
            )
        else:
            skipped.append(decision)

    for i, (row, why) in enumerate(dirty, 1):
        old = row.get("hoa") or ""
        key = (row.get("hoa_id"), old)
        if key in processed_keys:
            if i % 10 == 0:
                print(f"  processed {i}/{len(dirty)}", file=sys.stderr)
            continue
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
                "is_hoa": True,
                "canonical_name": stripped,
                "city": None,
                "state": None,
                "county": None,
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
        try:
            _set_alarm(int(os.environ.get("PHASE10_DOC_FETCH_TIMEOUT_SECONDS", "90")))
            text = _fetch_doc_text(args.base_url, old, max_chars=args.max_text_chars)
        except OperationTimeout:
            decision = {
                "hoa_id": row.get("hoa_id"),
                "old_name": old,
                "dirty_reason": why,
                "is_hoa": True,
                "canonical_name": None,
                "city": None, "state": None, "county": None,
                "confidence": 0.0,
                "llm_reason": "skipped:doc_fetch_timeout",
                "doc_count": row.get("doc_count"),
                "chunk_count": row.get("chunk_count"),
                "skip_delete": True,
            }
            decisions.append(decision)
            processed_keys.add(key)
            skipped.append(decision)
            if i % 10 == 0:
                print(f"  processed {i}/{len(dirty)}", file=sys.stderr)
                out_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
            time.sleep(args.sleep_s)
            continue
        finally:
            _clear_alarm()
        # If we couldn't fetch enough text to validate, skip the LLM entirely
        # and emit a decision that preserves the current entry. The OCR
        # validation pass should never delete an entry when we have no text
        # to base the decision on.
        if len(text or "") < 500:
            decision = {
                "hoa_id": row.get("hoa_id"),
                "old_name": old,
                "dirty_reason": why,
                "is_hoa": True,
                "canonical_name": None,
                "city": None, "state": None, "county": None,
                "confidence": 0.0,
                "llm_reason": "skipped:empty_doc_text",
                "doc_count": row.get("doc_count"),
                "chunk_count": row.get("chunk_count"),
                "skip_delete": True,  # phase10_close honors this guard
            }
            decisions.append(decision)
            skipped.append(decision)
            if i % 10 == 0:
                print(f"  processed {i}/{len(dirty)}", file=sys.stderr)
                out_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
            time.sleep(args.sleep_s)
            continue
        try:
            _set_alarm(int(os.environ.get("PHASE10_LLM_TIMEOUT_SECONDS", "75")))
            ans = _ask_llm(client, args.model, old, text)
            if ans is None or ans.get("_error"):
                ans = _ask_llm(client, args.fallback_model, old, text) or {}
        except OperationTimeout:
            ans = {"_error": "llm_timeout"}
        finally:
            _clear_alarm()
        canonical = (ans or {}).get("canonical_name")
        confidence = float((ans or {}).get("confidence") or 0)
        reason = (ans or {}).get("reason") or (ans or {}).get("_error") or ""
        # is_hoa: default to True for safety when the LLM doesn't explicitly
        # set it (older prompt schema or response missing the field). Only
        # treat as False when the LLM explicitly returns False.
        is_hoa_raw = (ans or {}).get("is_hoa")
        is_hoa = False if is_hoa_raw is False else True
        # Override is_hoa back to True when the LLM's reason indicates it
        # was uncertain due to missing/insufficient text (rather than a
        # confident "this is not an HOA"). DeepSeek-flash + Kimi sometimes
        # return is_hoa=false with a "no document excerpt" canned response
        # even when the doc text was supplied — defensive guard prevents
        # mass deletions from those false negatives.
        if not is_hoa:
            uncertainty = re.search(
                r"\b(no\s+document|no\s+excerpt|cannot\s+verify|unable\s+to|"
                r"insufficient|not\s+enough\s+(?:text|info|context|content)|"
                r"text\s+(?:is|was)\s+(?:empty|missing|not\s+provided))\b",
                reason or "", re.I,
            )
            if uncertainty:
                is_hoa = True
        # Normalize state to upper 2-letter, drop empty / non-2-letter.
        llm_state_raw = (ans or {}).get("state")
        if isinstance(llm_state_raw, str):
            s = llm_state_raw.strip().upper()
            llm_state = s if len(s) == 2 and s.isalpha() else None
        else:
            llm_state = None
        llm_city = (ans or {}).get("city")
        if isinstance(llm_city, str):
            llm_city = llm_city.strip() or None
        else:
            llm_city = None
        llm_county = (ans or {}).get("county")
        if isinstance(llm_county, str):
            llm_county = llm_county.strip() or None
        else:
            llm_county = None
        decision = {
            "hoa_id": row.get("hoa_id"),
            "old_name": old,
            "dirty_reason": why,
            "is_hoa": is_hoa,
            "canonical_name": canonical,
            "city": llm_city,
            "state": llm_state,
            "county": llm_county,
            "confidence": confidence,
            "llm_reason": reason,
            "doc_count": row.get("doc_count"),
            "chunk_count": row.get("chunk_count"),
        }
        decisions.append(decision)
        # Only propose a rename for entries the LLM confirmed as HOAs.
        # is_hoa=False entries get hard-deleted by phase10_close.py via
        # parse_not_hoa_ids; renaming them would just hide the problem.
        if (
            is_hoa
            and _looks_canonical(canonical)
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
            payload = None
            last_err = None
            for attempt in range(6):
                try:
                    r = requests.post(
                        f"{args.base_url}/admin/rename-hoa",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"renames": chunk},
                        timeout=180,
                    )
                    if r.status_code == 200:
                        payload = r.json()
                        last_err = None
                        break
                    last_err = f"http {r.status_code}: {r.text[:160]}"
                except Exception as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                time.sleep(20 + 10 * attempt)
            if last_err:
                print(f"rename chunk {start//100 + 1} failed after retries: {last_err}", file=sys.stderr)
                errs_total += len(chunk)
                continue
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
