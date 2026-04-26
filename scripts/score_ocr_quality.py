#!/usr/bin/env python3
"""Score OCR quality of existing chunks; flag candidates for DocAI re-OCR.

Heuristic: real-word ratio. We compute, per document, the fraction of
whitespace-separated tokens that look like real English words (alphabetic,
length 2-20, all-lowercase form). Garbled tesseract output (the Preston Point
declaration we sampled is exemplar) scores well below 60%.

Usage:
    # Default: print top-100 candidates for re-OCR
    python scripts/score_ocr_quality.py

    # Filter by HOA category and show all under threshold
    python scripts/score_ocr_quality.py --category ccr --threshold 0.6

    # Output JSON manifest for the re-OCR script
    python scripts/score_ocr_quality.py --output data/reocr_candidates.json --limit 0
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hoaware import db
from hoaware.config import load_settings


# Cheap "looks like a real word" check. Conservative: anything with non-alpha
# chars, ALL-CAPS runs, or odd length fails. Won't be perfect but it doesn't
# need to be — we just need to spot tesseract garbage like "fRES~HTEO" and
# "wo~J::Jt..lJ".
_WORD_RE = re.compile(r"^[a-z][a-z'-]{1,18}[a-z]$|^[a-z]$")


def _score_text(text: str) -> tuple[int, int]:
    """Returns (real_word_count, total_token_count)."""
    if not text:
        return 0, 0
    tokens = text.split()
    real = 0
    for tok in tokens:
        # Strip surrounding punctuation
        stripped = tok.strip(".,;:()[]{}\"'!?")
        if not stripped:
            continue
        # Lowercase test (we accept Title-case via lowercasing)
        if _WORD_RE.match(stripped.lower()):
            real += 1
    return real, len(tokens)


def main() -> int:
    ap = argparse.ArgumentParser(description="Score OCR quality across the corpus")
    ap.add_argument("--threshold", type=float, default=0.60,
                    help="Real-word ratio threshold below which a doc is a re-OCR candidate")
    ap.add_argument("--category", default=None,
                    help="Filter by documents.category (e.g. ccr, bylaws)")
    ap.add_argument("--limit", type=int, default=100,
                    help="Show top N (sorted by worst quality first); 0 = all")
    ap.add_argument("--min-tokens", type=int, default=200,
                    help="Skip very short docs to avoid noise")
    ap.add_argument("--output", type=Path, default=None,
                    help="Optional JSON output for re-OCR pipeline")
    args = ap.parse_args()

    settings = load_settings()
    where = ["d.hidden_reason IS NULL"]
    params: list = []
    if args.category:
        where.append("d.category = ?")
        params.append(args.category)

    sql = f"""
        SELECT d.id AS doc_id, h.name AS hoa, d.relative_path, d.category,
               d.text_extractable, d.page_count, d.bytes,
               GROUP_CONCAT(c.text, ' ') AS combined_text
        FROM documents d
        JOIN hoas h ON h.id = d.hoa_id
        LEFT JOIN chunks c ON c.document_id = d.id
        WHERE {' AND '.join(where)}
        GROUP BY d.id
    """

    candidates: list[dict] = []
    with db.get_connection(settings.db_path) as conn:
        for row in conn.execute(sql, params).fetchall():
            real, total = _score_text(row["combined_text"] or "")
            if total < args.min_tokens:
                continue
            ratio = real / total if total else 0.0
            if ratio < args.threshold:
                candidates.append({
                    "doc_id": int(row["doc_id"]),
                    "hoa": str(row["hoa"]),
                    "relative_path": str(row["relative_path"]),
                    "category": row["category"],
                    "text_extractable": row["text_extractable"],
                    "page_count": int(row["page_count"]) if row["page_count"] is not None else None,
                    "bytes": int(row["bytes"]),
                    "real_word_ratio": round(ratio, 3),
                    "real_words": real,
                    "total_tokens": total,
                })

    candidates.sort(key=lambda c: c["real_word_ratio"])
    if args.limit > 0:
        candidates = candidates[: args.limit]

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(candidates, indent=2))
        print(f"Wrote {len(candidates)} candidates to {args.output}")
    else:
        print(f"{'ratio':<6}  {'pages':<5}  {'cat':<10}  hoa / file")
        for c in candidates:
            print(f"{c['real_word_ratio']:<6}  {c['page_count'] or '?':<5}  "
                  f"{(c['category'] or '?'):<10}  {c['hoa']} / {c['relative_path'].rsplit('/', 1)[-1]}")
        print(f"\n{len(candidates)} doc(s) under threshold {args.threshold}")

    # Total page count if we re-OCR everything in the manifest
    total_pages = sum((c.get("page_count") or 0) for c in candidates)
    est_cost = round(total_pages * 0.0015, 2)
    print(f"Total pages if re-OCR'd: {total_pages:,}  →  est ${est_cost:.2f} via DocAI")

    return 0


if __name__ == "__main__":
    sys.exit(main())
