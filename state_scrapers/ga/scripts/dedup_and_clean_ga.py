"""Comprehensive GA HOA cleanup: dedup near-duplicates + repair remaining
dirty names.

Pulls every live GA HOA, groups by a suffix-stripped signature, and asks
DeepSeek to decide for each group:

  - same HOA: which entry is canonical? merge the rest.
  - different HOAs: leave alone (e.g. signature 'village' across cities).

Then re-runs the dirty-name cleanup using a more permissive prompt that
allows inferring the HOA name from the named subdivision when the doc
text doesn't say "Homeowners Association" verbatim.

Posts decisions to /admin/rename-hoa in chunks of 50.

  python state_scrapers/ga/scripts/dedup_and_clean_ga.py            # dry run
  python state_scrapers/ga/scripts/dedup_and_clean_ga.py --apply
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

# Reuse the existing cleanup utilities.
sys.path.insert(0, str(ROOT / "state_scrapers" / "ga" / "scripts"))
from clean_dirty_hoa_names import (  # noqa: E402
    is_dirty,
    _try_strip_prefix,
    _looks_canonical,
    _live_admin_token,
    _fetch_summaries,
    _fetch_doc_text,
    _llm_client,
    DEFAULT_MODEL,
    FALLBACK_MODEL,
)

BASE_URL = "https://hoaproxy.org"

SUFFIX_TOKENS = {
    "homeowners", "homeowner", "association", "associations", "assn",
    "hoa", "poa", "property", "owners", "owner", "community",
    "condominium", "condo", "condos", "condominiums",
    "townhome", "townhomes", "townhouse", "townhouses", "villas", "villa",
    "estates", "homes", "home", "houses", "house",
    "neighborhood", "subdivision", "sub", "ph", "phase",
    "inc", "incorporated", "co", "corp", "corporation", "ltd", "llc",
    "the", "at", "of", "and", "a", "in", "on", "by",
    "georgia", "ga",
    "s",  # singular/plural form-fold
}


def signature(name: str) -> str:
    n = (name or "").lower()
    n = re.sub(r"[^\w\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    toks = [t for t in n.split() if t and t not in SUFFIX_TOKENS]
    return " ".join(toks)


# --- LLM prompts ---


DEDUP_SYSTEM = (
    "You decide whether multiple HOA / condominium / property-owners-"
    "association entries on a website are the same legal association. "
    "Two entries are the same when their proper-name root matches and "
    "neither is clearly a different community in a different town. "
    "Use city, document count, chunk count, and the names themselves. "
    "When they're the same, return the most-canonical name (prefer "
    "fully-spelled-out 'Homeowners Association' over 'HOA', prefer "
    "the spelling closest to a legal-entity record, and avoid noisy "
    "all-caps when an equivalent mixed-case version exists)."
)


def dedup_prompt(group: list[dict]) -> str:
    rows = "\n".join(
        f"  id={g['hoa_id']} city={g.get('city') or '-'} docs={g.get('doc_count')} chunks={g.get('chunk_count')} name={g['hoa']!r}"
        for g in group
    )
    return (
        f"Entries with shared name root:\n{rows}\n\n"
        "Return strict JSON:\n"
        "  {\"same_hoa\": <true|false>,\n"
        "   \"canonical_name\": <string or null>,\n"
        "   \"keep_id\": <int or null>,\n"
        "   \"reason\": <short string>}\n\n"
        "If same_hoa is false (entries are distinct HOAs that happen "
        "to share a root), keep_id and canonical_name should be null. "
        "Otherwise pick the entry whose name is closest to canonical "
        "(or pick the largest by chunks if tied) as keep_id, and set "
        "canonical_name to its preferred form. Return JSON only."
    )


PERMISSIVE_SYSTEM = (
    "You normalize HOA names. Given a possibly-garbled current name "
    "plus the first chunk of the HOA's governing-document OCR text, "
    "return the canonical legal name of the association. Prefer the "
    "exact phrase from the document body. If the document names a "
    "subdivision but never spells out the legal entity, you may infer "
    "'<Subdivision Name> Homeowners Association' as long as the "
    "subdivision name is unambiguous in the text. Return null only "
    "when no specific subdivision or community is named at all. "
    "Strip OCR fragments, page headers, all-caps shouting, and "
    "document titles like 'BY-LAWS OF', 'DECLARATION OF', "
    "'EXHIBIT A', 'AMENDMENT', or 'AMENDED AND RESTATED'."
)


def name_prompt(name: str, text: str) -> str:
    return (
        f"current_name: {name!r}\n\n"
        f"document_excerpt:\n{text or '(none)'}\n\n"
        "Return strict JSON: {\"canonical_name\": <string or null>, "
        "\"confidence\": <0-1>, \"reason\": <short string>}. "
        "Do not include any text outside the JSON object."
    )


def _llm_json(client: OpenAI, model: str, system: str, user: str) -> dict | None:
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
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


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# --- Driver ---


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", default="GA")
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--out", default="state_scrapers/ga/results/dedup_and_clean.jsonl")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--fallback-model", default=FALLBACK_MODEL)
    p.add_argument("--max-text-chars", type=int, default=3500)
    p.add_argument("--min-confidence", type=float, default=0.7)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--sleep-s", type=float, default=0.05)
    p.add_argument("--skip-name-cleanup", action="store_true")
    p.add_argument("--skip-dedup", action="store_true")
    args = p.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    client = _llm_client()
    summaries = _fetch_summaries(args.base_url, args.state.upper())
    print(f"live HOAs: {len(summaries)}", file=sys.stderr)

    decisions: list[dict] = []
    renames: list[dict] = []  # {hoa_id, new_name} for /admin/rename-hoa

    # ---- 1. Dedup pass ----
    if not args.skip_dedup:
        groups = defaultdict(list)
        for r in summaries:
            sig = signature(r.get("hoa") or "")
            if sig and len(sig) >= 4:  # very-short signatures are too generic
                groups[sig].append(r)
        dedup_groups = [(sig, gs) for sig, gs in groups.items() if len(gs) > 1]
        print(f"dedup groups (size ≥ 2, signature ≥ 4 chars): {len(dedup_groups)}", file=sys.stderr)

        for i, (sig, group) in enumerate(dedup_groups, 1):
            ans = _llm_json(client, args.model, DEDUP_SYSTEM, dedup_prompt(group))
            if ans is None or ans.get("_error"):
                ans = _llm_json(client, args.fallback_model, DEDUP_SYSTEM, dedup_prompt(group)) or {}
            same = bool((ans or {}).get("same_hoa"))
            keep_id = (ans or {}).get("keep_id")
            canonical = (ans or {}).get("canonical_name")
            decisions.append({
                "kind": "dedup",
                "signature": sig,
                "members": [{"id": g["hoa_id"], "name": g["hoa"]} for g in group],
                "same_hoa": same,
                "keep_id": keep_id,
                "canonical_name": canonical,
                "reason": (ans or {}).get("reason"),
            })
            if same and keep_id and canonical and _looks_canonical(canonical):
                # rename the keep entry to the canonical name (no-op if already)
                # then merge each non-keep entry into that canonical name
                renames.append({"hoa_id": int(keep_id), "new_name": canonical.strip()})
                for g in group:
                    if int(g["hoa_id"]) != int(keep_id):
                        renames.append({"hoa_id": int(g["hoa_id"]), "new_name": canonical.strip()})
            if i % 10 == 0:
                print(f"  dedup {i}/{len(dedup_groups)}", file=sys.stderr)
                out_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
            time.sleep(args.sleep_s)

    # ---- 2. Name cleanup pass ----
    if not args.skip_name_cleanup:
        # rebuild "currently dirty" list, excluding entries already covered by a dedup rename
        already_planned = {r["hoa_id"] for r in renames}
        dirty = [r for r in summaries if is_dirty(r.get("hoa") or "")[0] and r["hoa_id"] not in already_planned]
        print(f"dirty names to review: {len(dirty)}", file=sys.stderr)

        for i, row in enumerate(dirty, 1):
            old = row.get("hoa") or ""
            stripped = _try_strip_prefix(old)
            if stripped and _looks_canonical(stripped) and stripped != old:
                decisions.append({
                    "kind": "name",
                    "hoa_id": row["hoa_id"],
                    "old_name": old,
                    "canonical_name": stripped,
                    "method": "deterministic_prefix_strip",
                    "confidence": 0.95,
                })
                renames.append({"hoa_id": row["hoa_id"], "new_name": stripped})
                continue

            text = _fetch_doc_text(args.base_url, old, max_chars=args.max_text_chars)
            ans = _llm_json(client, args.model, PERMISSIVE_SYSTEM, name_prompt(old, text))
            if ans is None or ans.get("_error"):
                ans = _llm_json(client, args.fallback_model, PERMISSIVE_SYSTEM, name_prompt(old, text)) or {}
            canonical = (ans or {}).get("canonical_name")
            confidence = float((ans or {}).get("confidence") or 0)
            decision = {
                "kind": "name",
                "hoa_id": row["hoa_id"],
                "old_name": old,
                "canonical_name": canonical,
                "method": "llm_permissive",
                "confidence": confidence,
                "reason": (ans or {}).get("reason") or (ans or {}).get("_error"),
            }
            decisions.append(decision)
            if (
                _looks_canonical(canonical)
                and confidence >= args.min_confidence
                and _normalize(canonical) != _normalize(old)
            ):
                renames.append({"hoa_id": row["hoa_id"], "new_name": canonical.strip()})
            if i % 10 == 0:
                print(f"  name {i}/{len(dirty)}", file=sys.stderr)
                out_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
            time.sleep(args.sleep_s)

    out_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
    print(f"wrote {out_path}", file=sys.stderr)
    print(json.dumps({
        "live": len(summaries),
        "decisions": len(decisions),
        "proposed_renames_or_merges": len(renames),
    }, sort_keys=True))

    if not args.apply or not renames:
        return 0

    token = _live_admin_token()
    if not token:
        print("no admin token; cannot apply", file=sys.stderr)
        return 1

    headers = {"Authorization": f"Bearer {token}"}
    totals = {"renamed": 0, "merged": 0, "errors": 0, "noop": 0}
    for start in range(0, len(renames), 50):
        chunk = renames[start : start + 50]
        r = requests.post(
            f"{args.base_url}/admin/rename-hoa",
            headers=headers,
            json={"renames": chunk},
            timeout=180,
        )
        r.raise_for_status()
        payload = r.json()
        for k in totals:
            totals[k] += int(payload.get(k) or 0)
        print(
            f"applied chunk {start//50 + 1}: "
            f"renamed={payload.get('renamed')} merged={payload.get('merged')} "
            f"noop={payload.get('noop')} errors={payload.get('errors')}",
            file=sys.stderr,
        )
    print(json.dumps({"applied": True, **totals}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
