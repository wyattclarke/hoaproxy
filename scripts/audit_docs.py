#!/usr/bin/env python3
"""Audit scraped HOA documents before upload.

For each HOA in the ingest queue (done + pending), classifies every PDF:
  1. Category (ccr, bylaws, tax, court, membership_list, etc.)
  2. Digital vs scanned (can pdfminer extract text?)
  3. PII risk

Outputs a JSON report and summary stats.

Usage:
    python scripts/audit_docs.py                        # audit all
    python scripts/audit_docs.py --limit 200            # audit first 200 HOAs
    python scripts/audit_docs.py --source co_google_ccr_scrape  # CO only
    python scripts/audit_docs.py --no-vision            # skip Haiku calls (regex only)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / "settings.env")
os.environ.setdefault("HOA_DB_PATH", str(ROOT / "data" / "hoa_index.db"))

from hoaware.doc_classifier import classify_pdf, classify_from_text, VALID_CATEGORIES, REJECT_PII
from hoaware.pii_filter import scan_for_pii


def is_digital(pdf_path: Path) -> bool:
    """Check if pdfminer can extract meaningful text from page 1."""
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(str(pdf_path), page_numbers=[0], maxpages=1)
        return bool(text and len(text.strip()) > 50)
    except Exception:
        return False


def get_page_count(pdf_path: Path) -> int:
    """Get page count from a PDF."""
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return 0


def audit_entry(entry: dict, use_vision: bool = True) -> dict:
    """Audit one HOA's documents. Returns audit record."""
    name = entry["name"]
    files = [Path(p) for p in entry.get("files", []) if Path(p).exists()]

    doc_audits = []
    for pdf_path in files:
        digital = is_digital(pdf_path)

        if use_vision:
            classification = classify_pdf(pdf_path, name)
        else:
            # Text-only classification
            try:
                from pdfminer.high_level import extract_text
                text = extract_text(str(pdf_path), page_numbers=[0], maxpages=1)
            except Exception:
                text = ""
            result = classify_from_text(text or "", name) if text and text.strip() else None
            classification = result or {"category": "unknown", "confidence": 0.0, "method": "none"}
            classification["is_valid"] = classification["category"] in VALID_CATEGORIES
            classification["is_pii_risk"] = classification["category"] in REJECT_PII

        # Post-OCR PII scan on digital docs
        pii_result = None
        if digital:
            try:
                from pdfminer.high_level import extract_text
                full_text = extract_text(str(pdf_path), maxpages=5)
                pii_scan = scan_for_pii(full_text or "")
                if pii_scan.has_pii:
                    pii_result = {
                        "risk_level": pii_scan.risk_level,
                        "findings": pii_scan.findings,
                    }
            except Exception:
                pass

        pages = get_page_count(pdf_path)

        doc_audits.append({
            "file": str(pdf_path),
            "filename": pdf_path.name,
            "size_kb": round(pdf_path.stat().st_size / 1024, 1),
            "pages": pages,
            "is_digital": digital,
            "category": classification["category"],
            "confidence": classification["confidence"],
            "method": classification["method"],
            "is_valid": classification["is_valid"],
            "is_pii_risk": classification.get("is_pii_risk", False),
            "pii_scan": pii_result,
        })

    has_valid = any(d["is_valid"] for d in doc_audits)
    has_pii = any(d["is_pii_risk"] or d.get("pii_scan") for d in doc_audits)

    # Page counts for OCR cost estimation
    valid_scanned = [d for d in doc_audits if d["is_valid"] and not d["is_digital"]]
    valid_digital = [d for d in doc_audits if d["is_valid"] and d["is_digital"]]

    return {
        "name": name,
        "state": entry.get("state", ""),
        "source": entry.get("source", ""),
        "total_docs": len(doc_audits),
        "valid_docs": sum(1 for d in doc_audits if d["is_valid"]),
        "rejected_docs": sum(1 for d in doc_audits if not d["is_valid"]),
        "digital_docs": sum(1 for d in doc_audits if d["is_digital"]),
        "scanned_docs": sum(1 for d in doc_audits if not d["is_digital"]),
        "pii_risk_docs": sum(1 for d in doc_audits if d["is_pii_risk"] or d.get("pii_scan")),
        "total_pages": sum(d["pages"] for d in doc_audits),
        "valid_digital_pages": sum(d["pages"] for d in valid_digital),
        "valid_scanned_pages": sum(d["pages"] for d in valid_scanned),
        "has_valid": has_valid,
        "has_pii": has_pii,
        "recommendation": "reject" if not has_valid else ("review_pii" if has_pii else "upload"),
        "documents": doc_audits,
    }


def main():
    parser = argparse.ArgumentParser(description="Audit HOA documents before upload")
    parser.add_argument("--limit", type=int, default=0, help="Limit to N HOAs (0=all)")
    parser.add_argument("--source", default=None, help="Filter by source")
    parser.add_argument("--no-vision", action="store_true",
                        help="Skip Haiku vision calls (regex-only classification)")
    parser.add_argument("--output", default=str(ROOT / "data" / "doc_audit_report.json"),
                        help="Output report path")
    args = parser.parse_args()

    # Collect entries from done + pending + worker dirs
    queue_dir = ROOT / "data" / "ingest_queue"
    entries = []
    for subdir in ["done", "pending"]:
        d = queue_dir / subdir
        if d.exists():
            for f in sorted(d.glob("*.json")):
                entries.append(json.loads(f.read_text()))
    # Also check worker staging dirs
    for w_dir in sorted(queue_dir.glob(".worker_*")):
        for subdir in ["pending", "done"]:
            d = w_dir / subdir
            if d.exists():
                for f in sorted(d.glob("*.json")):
                    entries.append(json.loads(f.read_text()))

    if args.source:
        entries = [e for e in entries if e.get("source") == args.source]

    if args.limit > 0:
        entries = entries[:args.limit]

    print(f"Auditing {len(entries)} HOAs...", flush=True)
    use_vision = not args.no_vision

    results = []
    category_counts = Counter()
    total_docs = 0
    total_digital = 0
    total_valid = 0
    total_pii = 0
    total_pages = 0
    total_valid_digital_pages = 0
    total_valid_scanned_pages = 0

    for i, entry in enumerate(entries):
        audit = audit_entry(entry, use_vision=use_vision)
        results.append(audit)

        total_docs += audit["total_docs"]
        total_digital += audit["digital_docs"]
        total_valid += audit["valid_docs"]
        total_pii += audit["pii_risk_docs"]
        total_pages += audit["total_pages"]
        total_valid_digital_pages += audit["valid_digital_pages"]
        total_valid_scanned_pages += audit["valid_scanned_pages"]
        for doc in audit["documents"]:
            category_counts[doc["category"]] += 1

        rec = audit["recommendation"]
        print(f"  [{i+1}/{len(entries)}] {audit['name'][:45]:45s}  "
              f"docs={audit['total_docs']}  valid={audit['valid_docs']}  "
              f"digital={audit['digital_docs']}  pii={audit['pii_risk_docs']}  "
              f"scanned_pg={audit['valid_scanned_pages']}  "
              f"→ {rec}", flush=True)

        # Periodic save
        if (i + 1) % 50 == 0:
            _save_report(results, category_counts, total_docs, total_digital,
                         total_valid, total_pii, total_pages,
                         total_valid_digital_pages, total_valid_scanned_pages,
                         args.output)

    _save_report(results, category_counts, total_docs, total_digital,
                 total_valid, total_pii, total_pages,
                 total_valid_digital_pages, total_valid_scanned_pages,
                 args.output)

    # Print summary
    print(f"\n{'='*70}")
    print(f"AUDIT SUMMARY — {len(results)} HOAs, {total_docs} documents, {total_pages:,} pages")
    print(f"{'='*70}")
    print(f"\n  Documents:")
    print(f"    Digital (no OCR needed):  {total_digital:5d}  ({total_digital*100/max(total_docs,1):.1f}%)")
    print(f"    Scanned (needs OCR):      {total_docs-total_digital:5d}  ({(total_docs-total_digital)*100/max(total_docs,1):.1f}%)")
    print(f"    Valid for upload:         {total_valid:5d}  ({total_valid*100/max(total_docs,1):.1f}%)")
    print(f"    PII risk:                 {total_pii:5d}  ({total_pii*100/max(total_docs,1):.1f}%)")
    print(f"\n  Pages (valid docs only):")
    print(f"    Digital (free to ingest): {total_valid_digital_pages:7,}")
    print(f"    Scanned (needs OCR):      {total_valid_scanned_pages:7,}")
    docai_cost = total_valid_scanned_pages * 1.5 / 1000
    print(f"    Document AI cost for scanned: ${docai_cost:,.2f}")
    print(f"\nCategories:")
    for cat, count in category_counts.most_common():
        print(f"  {cat:25s} {count:5d}")
    print(f"\nRecommendations:")
    recs = Counter(r["recommendation"] for r in results)
    for rec, count in recs.most_common():
        print(f"  {rec:20s} {count:5d}")

    # Extrapolation to full queue
    audited_hoas = len(results)
    total_queue = len(list((ROOT / "data" / "ingest_queue" / "done").glob("*.json")))
    for w_dir in (ROOT / "data" / "ingest_queue").glob(".worker_*"):
        for sub in ["pending", "done"]:
            d = w_dir / sub
            if d.exists():
                total_queue += len(list(d.glob("*.json")))
    total_queue += len(list((ROOT / "data" / "ingest_queue" / "pending").glob("*.json")))

    if audited_hoas > 0 and total_queue > audited_hoas:
        ratio = total_queue / audited_hoas
        est_scanned_pages = int(total_valid_scanned_pages * ratio)
        est_docai = est_scanned_pages * 1.5 / 1000
        print(f"\n  Extrapolation to full queue ({total_queue:,} HOAs):")
        print(f"    Est. scanned pages needing OCR: ~{est_scanned_pages:,}")
        print(f"    Est. Document AI cost:          ~${est_docai:,.2f}")

    print(f"\nReport saved to: {args.output}")


def _save_report(results, category_counts, total_docs, total_digital,
                 total_valid, total_pii, total_pages,
                 total_valid_digital_pages, total_valid_scanned_pages,
                 output_path):
    report = {
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "total_hoas": len(results),
        "total_docs": total_docs,
        "total_digital": total_digital,
        "total_valid": total_valid,
        "total_pii_risk": total_pii,
        "total_pages": total_pages,
        "valid_digital_pages": total_valid_digital_pages,
        "valid_scanned_pages": total_valid_scanned_pages,
        "est_docai_cost_scanned": round(total_valid_scanned_pages * 1.5 / 1000, 2),
        "category_counts": dict(category_counts.most_common()),
        "results": results,
    }
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)


if __name__ == "__main__":
    main()
