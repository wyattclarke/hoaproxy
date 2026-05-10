#!/usr/bin/env python3
"""Re-grade the 17 verdict==error entries in ca_grades.json.

All 17 failed during the original sibling-session audit with HTTP 502
or ReadTimeout from /hoas/{name}/documents — Render was overloaded.
Re-runs the grader (DeepSeek primary, Claude-Haiku fallback) on just
those entries and writes the updated verdicts back to ca_grades.json.

Usage:
    .venv/bin/python state_scrapers/ca/scripts/regrade_ca_errors.py
    .venv/bin/python state_scrapers/ca/scripts/regrade_ca_errors.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GRADES_PATH = ROOT / "state_scrapers" / "ca" / "results" / "audit_2026_05_09" / "ca_grades.json"

sys.path.insert(0, str(ROOT))
from scripts.audit.grade_hoa_text_quality import (  # noqa: E402
    grade_hoa,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "settings.env")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--sample-chars", type=int, default=2500)
    args = ap.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY missing", file=sys.stderr)
        return 1

    data = json.loads(GRADES_PATH.read_text())
    results = data.get("results", [])
    err_idxs = [i for i, r in enumerate(results) if r.get("verdict") == "error"]
    print(f"[regrade] {len(err_idxs)} entries to re-grade")

    if args.dry_run:
        for i in err_idxs[:20]:
            r = results[i]
            print(f"  would re-grade id={r.get('hoa_id'):>6} {r.get('hoa')}")
        return 0

    new_verdicts: Counter = Counter()
    for n, i in enumerate(err_idxs, 1):
        old = results[i]
        # grade_hoa() expects an "hoa" dict shaped like /hoas/summary results.
        # Rebuild from the audit entry — name + state + doc_count is enough.
        hoa_payload = {
            "hoa": old.get("hoa"),
            "hoa_id": old.get("hoa_id"),
            "city": old.get("city"),
            "state": old.get("state") or "CA",
            "doc_count": old.get("doc_count") or 1,
            "chunk_count": old.get("chunk_count") or 0,
        }
        try:
            new = grade_hoa(
                hoa_payload,
                base_url=DEFAULT_BASE_URL,
                api_key=api_key,
                model=args.model,
                sample_chars=args.sample_chars,
            )
        except Exception as e:
            new = {
                **old,
                "verdict": "error",
                "category": "regrade_exception",
                "reason": f"{type(e).__name__}: {e}",
            }
        # Preserve hoa_id + name; overwrite verdict/category/reason
        new["hoa_id"] = old.get("hoa_id")
        new["hoa"] = old.get("hoa")
        new["state"] = old.get("state") or "CA"
        results[i] = new
        v = new.get("verdict") or "error"
        new_verdicts[v] += 1
        print(f"  [{n}/{len(err_idxs)}] id={new.get('hoa_id'):>6} {new.get('hoa')[:50]:<50} -> {v} ({new.get('category')})")

    # Refresh top-level verdict_counts
    verdict_counts = Counter(r.get("verdict") for r in results)
    data["verdict_counts"] = dict(verdict_counts)
    data["total_graded"] = sum(verdict_counts.values())
    GRADES_PATH.write_text(json.dumps(data, indent=2))

    print()
    print(f"[regrade] re-graded results: {dict(new_verdicts)}")
    print(f"[regrade] new verdict totals: {dict(verdict_counts)}")
    print(f"[regrade] written to {GRADES_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
