#!/usr/bin/env python3
"""Deep name repair for WY: re-extract HOA names from full document text,
then either rename (if a real subdivision/HOA name surfaces) or delete (if
the doc is genuinely not an HOA governing instrument).

Improvements over `clean_dirty_hoa_names.py --no-dirty-filter`:

  1. Pulls FULL document text (not just first 3500 chars). Bank-stage name
     extraction grabs whatever appears at top of page 1 — typically the
     recorder stamp ("$72.00 PK AMENDED COVENANTS — EDA SCHUNK THOMPSON,
     SHERIDAN COUNTY CLERK"). The actual HOA / subdivision name is in the
     declaration body, often 1-3 KB deep.

  2. Builds an LLM context window that includes (a) the first 4000 chars
     and (b) 1500-char windows around every mention of
     `Association | Declaration | Covenants | Subdivision`. This catches
     names buried in recital paragraphs.

  3. LLM prompt asks for both a canonical name AND an `is_hoa` boolean.
     When the document body confirms it's a recorded declaration of
     covenants for a real community, propose a name even when the bank
     name is garbage. When the document is a city ordinance / planning
     memo / random gov filing, set `is_hoa=false` so the runner can
     DELETE the live entry via /admin/delete-hoa.

  4. Applies high-confidence renames AND deletes in one pass.

Run mode: --dry-run by default; --apply to actually mutate.
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
load_dotenv(ROOT / "settings.env", override=False)

BASE_URL = "https://hoaproxy.org"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
# OpenRouter API key is gateway-restricted to DEFAULT_MODEL. The historical
# Kimi K2 fallback is no longer permitted, so retries re-hit the primary.
FALLBACK_MODEL = DEFAULT_MODEL

SYSTEM = (
    "You normalize HOA / condominium / property-owners-association names. "
    "Given the live HOA's current (often garbled) name plus the FULL OCR text "
    "of its governing document, do two things: "
    "(1) decide whether the document is in fact an HOA / condo / "
    "subdivision-association governing instrument (declaration of covenants, "
    "CC&Rs, master deed, articles of incorporation of an HOA, bylaws of an "
    "association). It IS one if it recites covenants/restrictions running with "
    "the land for an identified subdivision or community, even if no formal "
    "association is named. It is NOT one if it is a town/city ordinance, a "
    "county zoning resolution, a court packet, a real-estate marketing PDF, "
    "an exam handbook, a meeting agenda, or a planning memo. "
    "(2) Extract the canonical legal name to use as the live HOA name. "
    "Prefer in this order: "
    "  (a) the formally-named association ('Buckhead Homeowners Association'); "
    "  (b) the subdivision/community name with the most natural HOA suffix "
    "      ('Skyview West Subdivision', 'Indian Springs Ranch'); "
    "  (c) the project name as it appears in the document title "
    "      ('AMENDED DECLARATION OF PROTECTIVE COVENANTS FOR THE SKYVIEW WEST "
    "      SUBDIVISION' → 'Skyview West Subdivision'). "
    "Strip recorder stamps ('$72.00 PK AMENDED COVENANTS, EDA SCHUNK …'), "
    "page headers, all-caps shouting, and document-title fragments like "
    "'BY-LAWS OF', 'DECLARATION OF', 'ARTICLES OF INCORPORATION OF'. "
    "Do not invent a name from a street address. If the document is an HOA "
    "governing instrument but you cannot find a community name, return "
    "is_hoa=true and canonical_name=null."
)


def _prompt(name: str, text: str) -> str:
    return (
        f"current_name: {name!r}\n\n"
        f"document_excerpt:\n{text or '(none)'}\n\n"
        "Return strict JSON: {\"is_hoa\": <true|false>, "
        "\"canonical_name\": <string or null>, "
        "\"confidence\": <0-1>, \"reason\": <short string>}. "
        "Do not include any text outside the JSON object."
    )


def _llm_client() -> OpenAI:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY required")
    return OpenAI(api_key=key, base_url=OPENROUTER_BASE_URL, max_retries=0)


def _ask_llm(client: OpenAI, model: str, name: str, text: str) -> dict[str, Any] | None:
    try:
        resp = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": _prompt(name, text)},
            ],
            temperature=0.0,
            max_tokens=400,
            timeout=60,
        )
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}
    content = (resp.choices[0].message.content or "").strip()
    try:
        return json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return {"_error": "json_parse"}


def _fetch_full_doc_text(base_url: str, hoa: str, max_chars: int = 80000) -> str:
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
        timeout=180,
    )
    if not rendered.ok:
        return ""
    pre = re.findall(r"<pre>(.*?)</pre>", rendered.text, flags=re.S | re.I)
    text = "\n".join(html.unescape(re.sub(r"<[^>]+>", " ", part)) for part in pre)
    return text[:max_chars]


def _build_context_window(text: str, head_chars: int = 4000, win_radius: int = 750,
                          max_total: int = 9000) -> str:
    """Concatenate the first head_chars plus 750-char windows around each
    interesting keyword hit. Dedupe overlapping windows. Cap at max_total."""
    if len(text) <= max_total:
        return text
    head = text[:head_chars]
    keywords = re.compile(
        r"\b(association|declaration|covenants?|subdivision|condominium|"
        r"homeowners?|home owners?|owners'?\s*association|"
        r"plat|filing|phase|estates|ranch|village|commons)\b",
        re.I,
    )
    spans: list[tuple[int, int]] = []
    for m in keywords.finditer(text[head_chars:], re.I):
        s = head_chars + max(0, m.start() - win_radius)
        e = head_chars + min(len(text) - head_chars, m.end() + win_radius)
        spans.append((s, e))
    # Merge overlapping spans
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    pieces = [head]
    used = head_chars
    for s, e in merged:
        snippet = text[s:e]
        if used + len(snippet) + 5 > max_total:
            snippet = snippet[: max(0, max_total - used - 5)]
        pieces.append(f"\n…\n{snippet}")
        used += len(snippet) + 5
        if used >= max_total:
            break
    return "".join(pieces)


def _live_admin_token() -> str | None:
    if os.environ.get("HOAPROXY_ADMIN_BEARER"):
        return os.environ["HOAPROXY_ADMIN_BEARER"]
    api = os.environ.get("RENDER_API_KEY")
    sid = os.environ.get("RENDER_SERVICE_ID")
    if api and sid:
        try:
            r = requests.get(
                f"https://api.render.com/v1/services/{sid}/env-vars",
                headers={"Authorization": f"Bearer {api}"}, timeout=30,
            )
            for env in r.json():
                e = env.get("envVar", env)
                if e.get("key") == "JWT_SECRET" and e.get("value"):
                    return e["value"]
        except Exception:
            pass
    return os.environ.get("JWT_SECRET")


def _post_with_retries(url: str, payload: dict, token: str, retries: int = 6,
                       backoff_s: int = 20) -> requests.Response:
    last = None
    for _ in range(retries):
        last = requests.post(url, headers={"Authorization": f"Bearer {token}"},
                             json=payload, timeout=120)
        if last.status_code == 200:
            return last
        if last.status_code in (500, 502, 503):
            time.sleep(backoff_s)
            continue
        return last
    return last  # type: ignore[return-value]


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", default="WY")
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--out", default="state_scrapers/wy/results/wy_20260507_225444_claude/deep_repair.jsonl")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--min-confidence", type=float, default=0.7)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--fallback-model", default=FALLBACK_MODEL)
    p.add_argument("--include-tagged", action="store_true",
                   help="Re-process [non-HOA]-tagged entries too.")
    p.add_argument("--name-suffix-strip", default="[non-HOA] ",
                   help="Strip this prefix from current names before sending to LLM.")
    args = p.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    client = _llm_client()
    token = _live_admin_token()
    if args.apply and not token:
        print("no admin token", file=sys.stderr); return 1

    summary = requests.get(f"{args.base_url}/hoas/summary",
                           params={"state": args.state, "limit": 2000},
                           timeout=60).json()
    results = summary.get("results") or []
    print(f"live {args.state} HOAs: {len(results)}", file=sys.stderr)

    # Decide which to process.
    targets: list[dict] = []
    for r in results:
        hoa = r.get("hoa", "")
        # Process if name looks junky OR is tagged
        is_tagged = hoa.startswith("[non-HOA] ")
        # Heuristic for "looks bad" - keep simple, anything we'd be unsure about
        looks_bad = bool(re.search(
            r"^\$|^\d+\s|HOA$|^OF\s|^the\s|"
            r"^[A-Z]{2,}\s+[A-Z]+\s+[A-Z]+\s+HOA$|"
            r"chapter|^archive$|^conditions$|^restrictive|^get images|"
            r"^cc\s*rs?org$|^d\s+c\s+d|^get\s+images$|"
            r"untitled|manuscript|surveyor review|^lt[a-z]+\s+minor|"
            r"^town\s+of|^city\s+of|^board\s+of|^community\s+character|"
            r"^centennial\s+hills\s*\.\.\.|"
            r"^supplement\s+to|^update\s+to|^wyoming\s+(data|dot|real)|"
            r"^zfe|^subdivision\s+(plat|lot|regs)$|^final\s+plat|"
            r"^restrictive$|^restrictive\s+hoa$|"
            r"^granite\s+ridge\s+excerpts$|^solitude\s+cc\s+rs|"
            r"^spring\s+canyon\s+ranch\s+cc|^star\s+valley\s+ranch\s+association$|"
            r"^stoneridge.*plat\s+hoa$|^stone\s+ridge.*plat\s+hoa$|"
            r"^teton\s+county|^teton\s+village\s+association\s+isd|"
            r"^the\s+homeowners\s+association$|^title\s+mills|"
            r"^townhome\s+ccr|"
            r"^untitled|^scanned|^community\s+character|^joint\s+information|"
            r"^centennial\s+hills|^century\s+21|^flexmls|^govinfo|"
            r"^jackson\s+planning|^lander\s+subdivision|^leda|^ltn|"
            r"^ltnwmp|^sd\s+app|^chapter|^cc&rs|^community\s+association|"
            r"^for\s+affordable|^final\s+planned|^dirt\s+road|"
            r"^campbell\s+county|^city\s+of\s+cody|^albany\s+county|"
            r"^approval\s+from|^archive$|^annexation|^afton\s+airpark$|"
            r"^cloud\s+peak|^centennial\s+hills",
            hoa, re.I,
        )) or len(hoa) < 6 or "HOA HOA" in hoa
        if is_tagged or looks_bad:
            targets.append(r)

    if not args.include_tagged:
        # If no tagged, only process the non-tagged "looks bad" set
        # (tagged were already classified as non-HOA earlier; user wants reconsider)
        pass

    print(f"will examine: {len(targets)}", file=sys.stderr)

    decisions: list[dict] = []
    rename_count = delete_count = skip_count = 0

    for i, r in enumerate(targets, 1):
        hoa = r.get("hoa", "")
        # Strip the [non-HOA] tag for the LLM context
        bare_name = hoa
        if hoa.startswith(args.name_suffix_strip):
            bare_name = hoa[len(args.name_suffix_strip):]

        text = _fetch_full_doc_text(args.base_url, hoa, max_chars=80000)
        ctx = _build_context_window(text)
        ans = _ask_llm(client, args.model, bare_name, ctx)
        if not ans or ans.get("_error"):
            ans = _ask_llm(client, args.fallback_model, bare_name, ctx) or {}

        is_hoa = bool((ans or {}).get("is_hoa"))
        canonical = (ans or {}).get("canonical_name")
        confidence = float((ans or {}).get("confidence") or 0)
        reason = (ans or {}).get("reason") or (ans or {}).get("_error") or ""

        action = "skip"
        new_name = None
        if not is_hoa and confidence >= args.min_confidence:
            action = "delete"
            delete_count += 1
        elif is_hoa and canonical and confidence >= args.min_confidence:
            cn = canonical.strip()
            if _normalize(cn) != _normalize(hoa) and len(cn) <= 180:
                action = "rename"
                new_name = cn
                rename_count += 1
            else:
                action = "skip"
                skip_count += 1
        else:
            action = "skip"
            skip_count += 1

        decision = {
            "hoa_id": r.get("hoa_id"),
            "old_name": hoa,
            "is_hoa": is_hoa,
            "canonical_name": canonical,
            "confidence": confidence,
            "reason": reason,
            "action": action,
            "new_name": new_name,
            "doc_count": r.get("doc_count"),
            "chunk_count": r.get("chunk_count"),
        }
        decisions.append(decision)

        if i % 5 == 0:
            print(f"  examined {i}/{len(targets)}", file=sys.stderr)
            out_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
        time.sleep(0.2)

    out_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
    print(json.dumps({
        "scanned": len(decisions),
        "rename": rename_count,
        "delete": delete_count,
        "skip": skip_count,
    }, indent=2))

    if not args.apply:
        return 0

    # Apply: deletes first, then renames.
    deletes = [d for d in decisions if d["action"] == "delete"]
    renames = [d for d in decisions if d["action"] == "rename"]
    print(f"\napplying: {len(deletes)} deletes + {len(renames)} renames", file=sys.stderr)

    if deletes:
        delete_payload = {"hoa_ids": [d["hoa_id"] for d in deletes]}
        r = _post_with_retries(f"{args.base_url}/admin/delete-hoa", delete_payload, token)
        print(f"delete: status={r.status_code} body={r.text[:300]}", file=sys.stderr)

    applied_rename = errs = 0
    for d in renames:
        payload = {"hoa_id": d["hoa_id"], "new_name": d["new_name"]}
        rr = _post_with_retries(f"{args.base_url}/admin/rename-hoa", payload, token)
        if rr.status_code == 200:
            applied_rename += int(rr.json().get("renamed") or 0) + int(rr.json().get("merged") or 0)
        else:
            errs += 1
            print(f"  rename ERR id={d['hoa_id']}: {rr.status_code} {rr.text[:120]}", file=sys.stderr)
        time.sleep(0.4)

    print(f"applied renames: {applied_rename} (errors: {errs})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
