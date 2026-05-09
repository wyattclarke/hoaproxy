#!/usr/bin/env python3
"""OCR-state cross-validation for Chicagoland live HOAs.

For each currently-live IL HOA whose bank prefix maps to Chicagoland scope
(cook/dupage/lake/will/kane/mchenry/kendall/il), fetch page-1 OCR text via
the public /admin/hoa-doc-text endpoint, count US-state-name mentions, and
mark for hard-delete if a non-IL state dominates by more than 2:1.

Catches the misroute pattern from round 2 (e.g. "Hoa Ai Lam" → "Disney's
BoardWalk Villas Condominium Association" → docs are about FL, not IL).

Usage:
  .venv/bin/python state_scrapers/il/scripts/ocr_state_validate_chicagoland.py            # dry run
  .venv/bin/python state_scrapers/il/scripts/ocr_state_validate_chicagoland.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)

sys.path.insert(0, str(ROOT / "state_scrapers" / "ga" / "scripts"))
from clean_dirty_hoa_names import (  # noqa: E402
    _fetch_summaries,
    _live_admin_token,
)

sys.path.insert(0, str(ROOT / "state_scrapers" / "il" / "scripts"))
from dedup_and_clean_il_chicagoland import (  # noqa: E402
    _is_chicagoland_eligible,
    _name_to_prefix_map,
)

BASE_URL = "https://hoaproxy.org"

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

# Match full state name OR comma-separated abbreviation (", FL")
STATE_NAME_RES = {
    abbr: re.compile(
        rf"\b({re.escape(name)}|,\s*{abbr}\b|\b{abbr}\s+\d{{5}})",
        re.I,
    )
    for abbr, name in US_STATES.items()
}


def count_state_mentions(text: str) -> dict[str, int]:
    """Return {state_abbr: count} for state references found in text."""
    counts: dict[str, int] = {}
    for abbr, regex in STATE_NAME_RES.items():
        n = len(regex.findall(text))
        if n:
            counts[abbr] = n
    return counts


def fetch_first_doc_text(base_url: str, hoa_name: str, max_chars: int = 6000) -> str:
    """Fetch page-1 / first-N-chars of the HOA's first doc text via the public docs endpoint."""
    try:
        r = requests.get(
            f"{base_url}/hoas/{requests.utils.quote(hoa_name, safe='')}/documents",
            timeout=30,
        )
        if r.status_code != 200:
            return ""
        docs = r.json() if r.headers.get("content-type", "").startswith("application/json") else []
        if not docs:
            return ""
        # Try /hoas/{name}/text or /hoas/{name}/documents/{doc_id}/text
        first = docs[0] if isinstance(docs, list) else (docs.get("documents") or [{}])[0]
        doc_id = first.get("doc_id") or first.get("id")
        if not doc_id:
            return ""
        # Try a couple of common text endpoints
        for url in (
            f"{base_url}/hoas/{requests.utils.quote(hoa_name, safe='')}/documents/{doc_id}/text",
            f"{base_url}/admin/doc/{doc_id}/text",
        ):
            try:
                r2 = requests.get(url, timeout=30)
                if r2.status_code == 200:
                    return r2.text[:max_chars]
            except Exception:
                continue
    except Exception:
        return ""
    return ""


def fetch_doc_text_via_chunks(base_url: str, hoa_name: str, max_chars: int = 6000) -> str:
    """Fallback: pull HOA chunks from /search and concatenate (the chunks contain doc text)."""
    try:
        r = requests.get(
            f"{base_url}/search",
            params={"q": "covenants declaration association", "hoa": hoa_name, "limit": 5},
            timeout=30,
        )
        if r.status_code != 200:
            return ""
        body = r.json()
        results = body.get("results") or []
        out: list[str] = []
        for h in results:
            t = h.get("text") or h.get("chunk") or ""
            if t:
                out.append(t)
                if sum(len(x) for x in out) >= max_chars:
                    break
        return " ".join(out)[:max_chars]
    except Exception:
        return ""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--name-to-prefix", action="append", default=None,
                   help="live_import_report.json paths (may repeat)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--out-dir",
                   default="state_scrapers/il/results/cleanup_chicagoland_ocrstate")
    p.add_argument("--sleep-s", type=float, default=0.5)
    p.add_argument("--ratio-threshold", type=float, default=2.0,
                   help="Non-IL state dominance ratio above which we hard-delete")
    p.add_argument("--min-non-il-mentions", type=int, default=3,
                   help="Minimum non-IL state mentions to consider deletion")
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = out_dir / "ocr_state_decisions.jsonl"

    paths = [Path(p) for p in (args.name_to_prefix or [
        "state_scrapers/il/results/il_20260508_114942_claude_phase2/live_import_report.json",
        "state_scrapers/il/results/il_chicagoland_20260509_061922_claude_phase2/live_import_report.json",
        "state_scrapers/il/results/il_chicagoland_assessor_20260509_145512_claude_phase2/live_import_report.json",
    ])]
    name_to_prefix = _name_to_prefix_map(paths)
    summaries = _fetch_summaries(args.base_url, "IL")

    eligible = [r for r in summaries if _is_chicagoland_eligible(r.get("hoa") or "", name_to_prefix)[0]]
    print(f"IL live total: {len(summaries)}", file=sys.stderr)
    print(f"Chicagoland eligible: {len(eligible)}", file=sys.stderr)

    decisions: list[dict[str, Any]] = []
    delete_ids: set[int] = set()

    for i, row in enumerate(eligible, 1):
        name = row.get("hoa") or ""
        # Try /search with hoa filter (most reliable, returns chunks)
        text = fetch_doc_text_via_chunks(args.base_url, name) or fetch_first_doc_text(args.base_url, name)
        if not text:
            decisions.append({
                "hoa_id": row["hoa_id"], "name": name,
                "decision": "no_text", "counts": {},
            })
            continue

        counts = count_state_mentions(text)
        il_count = counts.get("IL", 0)
        non_il = {k: v for k, v in counts.items() if k != "IL"}
        top_non_il = max(non_il.items(), key=lambda kv: kv[1]) if non_il else (None, 0)

        decision = "keep"
        reason = ""
        if top_non_il[0] and top_non_il[1] >= args.min_non_il_mentions:
            ratio = (top_non_il[1] / il_count) if il_count else float("inf")
            if ratio >= args.ratio_threshold:
                decision = "delete"
                reason = f"top_non_il={top_non_il[0]}({top_non_il[1]}) il={il_count} ratio={ratio:.1f}"
                delete_ids.add(int(row["hoa_id"]))

        decisions.append({
            "hoa_id": row["hoa_id"], "name": name,
            "decision": decision, "reason": reason,
            "counts": counts,
        })
        if i % 20 == 0:
            print(f"  scanned {i}/{len(eligible)} (deletes so far: {len(delete_ids)})", file=sys.stderr)
            decisions_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
        time.sleep(args.sleep_s)

    decisions_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
    print(json.dumps({
        "eligible": len(eligible),
        "deletions": len(delete_ids),
        "decisions_path": str(decisions_path),
    }, sort_keys=True))

    if not args.apply:
        print("dry-run; pass --apply to hard-delete", file=sys.stderr)
        return 0

    if not delete_ids:
        return 0

    token = _live_admin_token()
    if not token:
        print("no admin token", file=sys.stderr)
        return 1
    headers = {"Authorization": f"Bearer {token}"}
    ids = sorted(delete_ids)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        r = requests.post(
            f"{args.base_url}/admin/delete-hoa",
            headers=headers,
            json={"hoa_ids": chunk, "dry_run": False},
            timeout=300,
        )
        r.raise_for_status()
        body = r.json()
        print(f"delete chunk {i//50 + 1}: deleted={body.get('deleted')}, errors={body.get('errors')}", file=sys.stderr)
        time.sleep(1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
