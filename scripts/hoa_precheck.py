#!/usr/bin/env python3
"""Per-PDF precheck for the HOA agent paradigm.

Inspects a PDF (local file or remote URL) and prints a JSON record an LLM
agent can use to decide whether to upload, what category to tag it as, and
whether DocAI will be needed for OCR.

Usage:
    hoa_precheck declaration.pdf [--hoa "Park Village"]
    hoa_precheck --url https://example.com/ccr.pdf [--hoa "Park Village"]
    hoa_precheck declaration.pdf --json   # JSON output (default)
    hoa_precheck declaration.pdf --human  # human-readable summary

Exit codes:
    0 — looks like a valid governing doc, safe to upload
    1 — PII risk or rejected category — DO NOT upload
    2 — uncertain category, agent should review before uploading
    3 — error reading the PDF
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hoaware.cost_tracker import COST_DOCAI_PER_PAGE
from hoaware.doc_classifier import (
    REJECT_PII,
    REJECT_JUNK,
    VALID_CATEGORIES,
    classify_from_filename,
    classify_from_text,
)


def _load_bytes(path_or_url: str) -> tuple[bytes, str]:
    """Returns (bytes, source_label). source_label is the URL or local path."""
    if path_or_url.startswith(("http://", "https://")):
        import requests

        resp = requests.get(path_or_url, timeout=60, stream=True)
        resp.raise_for_status()
        buf = io.BytesIO()
        limit = 25 * 1024 * 1024
        total = 0
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise RuntimeError(f"PDF exceeds 25 MB limit (got {total} bytes)")
            buf.write(chunk)
        return buf.getvalue(), path_or_url

    p = Path(path_or_url)
    if not p.exists():
        raise FileNotFoundError(p)
    return p.read_bytes(), str(p.resolve())


def precheck(path_or_url: str, hoa_name: str = "") -> dict:
    pdf_bytes, source = _load_bytes(path_or_url)
    sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    file_size = len(pdf_bytes)
    filename = source.rsplit("/", 1)[-1]

    page_count: int | None = None
    text_extractable: bool | None = None
    suggested_category: str | None = None
    method: str | None = None
    confidence: float | None = None

    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
        if page_count:
            first_text = reader.pages[0].extract_text() or ""
            text_extractable = len(first_text.strip()) >= 50

            if text_extractable:
                # Classify from text (free, no API call)
                full_text_parts: list[str] = []
                for i in range(min(5, page_count)):
                    try:
                        full_text_parts.append(reader.pages[i].extract_text() or "")
                    except Exception:
                        pass
                full_text = "\n".join(full_text_parts)
                clf = classify_from_text(full_text, hoa_name)
                if clf:
                    suggested_category = clf["category"]
                    method = clf["method"]
                    confidence = clf["confidence"]
    except Exception as exc:
        return {
            "ok": False,
            "error": f"PyPDF inspection failed: {exc}",
            "source": source,
            "sha256": sha256,
            "file_size_bytes": file_size,
        }

    # Filename fallback
    if not suggested_category:
        clf = classify_from_filename(filename)
        if clf:
            suggested_category = clf["category"]
            method = clf["method"]
            confidence = clf["confidence"]

    is_valid = suggested_category in VALID_CATEGORIES
    is_pii = suggested_category in REJECT_PII
    is_junk = suggested_category in REJECT_JUNK
    est_pages = page_count or 0 if text_extractable is False else 0
    est_cost = round(est_pages * COST_DOCAI_PER_PAGE, 6)

    if is_pii or is_junk:
        recommendation = "reject"
    elif is_valid:
        recommendation = "upload"
    else:
        recommendation = "review"

    return {
        "ok": True,
        "source": source,
        "filename": filename,
        "sha256": sha256,
        "file_size_bytes": file_size,
        "page_count": page_count,
        "text_extractable": text_extractable,
        "suggested_category": suggested_category,
        "classification_method": method,
        "classification_confidence": confidence,
        "is_valid_governing_doc": is_valid,
        "is_pii_risk": is_pii,
        "is_junk": is_junk,
        "est_docai_pages": est_pages,
        "est_docai_cost_usd": est_cost,
        "recommendation": recommendation,
    }


def _exit_code(result: dict) -> int:
    if not result.get("ok"):
        return 3
    rec = result.get("recommendation")
    if rec == "upload":
        return 0
    if rec == "reject":
        return 1
    return 2


def _print_human(r: dict) -> None:
    if not r.get("ok"):
        print(f"ERROR: {r.get('error')}", file=sys.stderr)
        return
    print(f"  file:        {r['filename']}")
    print(f"  sha256:      {r['sha256'][:16]}…")
    print(f"  size:        {r['file_size_bytes']:,} bytes  ({r['page_count']} pages)")
    print(f"  text:        {'extractable' if r['text_extractable'] else 'scanned (needs OCR)'}")
    print(f"  category:    {r.get('suggested_category')} "
          f"(via {r.get('classification_method')}, conf={r.get('classification_confidence')})")
    print(f"  est OCR:     {r['est_docai_pages']} pages → ${r['est_docai_cost_usd']:.4f}")
    print(f"  recommend:   {r['recommendation'].upper()}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Inspect a PDF for HOA-doc upload suitability")
    ap.add_argument("pdf", nargs="?", help="Local PDF path")
    ap.add_argument("--url", help="Remote PDF URL (alternative to positional path)")
    ap.add_argument("--hoa", default="", help="HOA name for context")
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="JSON output (default)")
    fmt.add_argument("--human", action="store_true", help="Human-readable summary")
    args = ap.parse_args()

    target = args.url or args.pdf
    if not target:
        ap.error("provide a PDF path or --url")

    try:
        result = precheck(target, hoa_name=args.hoa)
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "source": target}

    if args.human:
        _print_human(result)
    else:
        print(json.dumps(result, indent=2))

    return _exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
