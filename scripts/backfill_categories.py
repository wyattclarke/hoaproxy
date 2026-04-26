#!/usr/bin/env python3
"""Backfill documents.category / text_extractable from the historical audit report.

PR-2 of the agent-paradigm cleanup.

Reads `data/doc_audit_report.json` and updates rows in the documents table:
  - documents.category          ← audit's classifier verdict
  - documents.text_extractable  ← audit's is_digital
  - documents.hidden_reason     ← set when category is junk/PII (PR-4 will use this
                                   to filter from search; here we only mark)

Matching strategy:
  audit "name" → hoas.name (exact)
  audit "filename" → documents.relative_path basename (case-insensitive)

Usage:
    # Local DB
    python scripts/backfill_categories.py

    # Production DB (after `gcloud storage cp gs://hoaproxy-backups/db/...` to local)
    HOA_DB_PATH=/path/to/prod-copy.db python scripts/backfill_categories.py

    # Dry-run (just report counts)
    python scripts/backfill_categories.py --dry-run

By default, the script ONLY sets `category` and `text_extractable`. Pass
`--apply-hidden-reason` to also flag junk/PII documents for hiding (PR-4).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hoaware import db
from hoaware.config import load_settings
from hoaware.doc_classifier import REJECT_JUNK, REJECT_PII

DEFAULT_AUDIT = ROOT / "data" / "doc_audit_report.json"


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill documents.category from audit report")
    ap.add_argument("--audit", type=Path, default=DEFAULT_AUDIT,
                    help=f"Audit JSON path (default: {DEFAULT_AUDIT})")
    ap.add_argument("--dry-run", action="store_true", help="Report only, no writes")
    ap.add_argument("--apply-hidden-reason", action="store_true",
                    help="Also set hidden_reason for junk/PII categories")
    args = ap.parse_args()

    if not args.audit.exists():
        print(f"ERROR: audit file not found: {args.audit}", file=sys.stderr)
        return 1

    print(f"Loading audit from {args.audit}", flush=True)
    audit = json.loads(args.audit.read_text())
    results = audit.get("results", [])
    print(f"Audit has {len(results)} HOAs / {audit.get('total_docs', 0)} documents", flush=True)

    settings = load_settings()
    print(f"Target DB: {settings.db_path}", flush=True)

    matched = 0
    not_found_hoa = 0
    not_found_doc = 0
    updates_by_category: Counter[str] = Counter()
    hidden_count = 0

    with db.get_connection(settings.db_path) as conn:
        # Build hoa_name → hoa_id map
        hoa_rows = conn.execute("SELECT id, name FROM hoas").fetchall()
        hoa_id_by_name = {row["name"]: int(row["id"]) for row in hoa_rows}
        hoa_id_by_lower = {row["name"].lower(): int(row["id"]) for row in hoa_rows}
        print(f"Production DB has {len(hoa_rows)} HOAs", flush=True)

        for entry in results:
            hoa_name = entry.get("name") or ""
            hoa_id = hoa_id_by_name.get(hoa_name) or hoa_id_by_lower.get(hoa_name.lower())
            if not hoa_id:
                not_found_hoa += 1
                continue

            # Build relative_path → document_id map for this HOA, indexed by basename
            doc_rows = conn.execute(
                "SELECT id, relative_path FROM documents WHERE hoa_id = ?",
                (hoa_id,),
            ).fetchall()
            by_basename: dict[str, int] = {}
            for row in doc_rows:
                rel = str(row["relative_path"])
                base = rel.rsplit("/", 1)[-1].lower()
                by_basename[base] = int(row["id"])

            for doc in entry.get("documents", []):
                fname = (doc.get("filename") or "").lower()
                if not fname:
                    continue
                doc_id = by_basename.get(fname)
                if not doc_id:
                    not_found_doc += 1
                    continue

                category = doc.get("category") or "unknown"
                te_val = 1 if doc.get("is_digital") else 0
                hidden = None
                if args.apply_hidden_reason:
                    if category in REJECT_PII:
                        hidden = f"pii:{category}"
                        hidden_count += 1
                    elif category in REJECT_JUNK:
                        hidden = f"junk:{category}"
                        hidden_count += 1

                if not args.dry_run:
                    if hidden:
                        conn.execute(
                            """UPDATE documents
                               SET category = ?, text_extractable = ?, hidden_reason = ?
                               WHERE id = ?""",
                            (category, te_val, hidden, doc_id),
                        )
                    else:
                        conn.execute(
                            """UPDATE documents
                               SET category = ?, text_extractable = ?
                               WHERE id = ?""",
                            (category, te_val, doc_id),
                        )
                matched += 1
                updates_by_category[category] += 1

        if not args.dry_run:
            conn.commit()

    print()
    print(f"{'DRY-RUN ' if args.dry_run else ''}Summary:")
    print(f"  Documents matched & updated:   {matched:,}")
    print(f"  HOA name not found in DB:      {not_found_hoa}")
    print(f"  Document filename not found:   {not_found_doc}")
    if args.apply_hidden_reason:
        print(f"  Marked hidden (junk/PII):      {hidden_count:,}")
    print(f"\n  Categories assigned:")
    for cat, n in updates_by_category.most_common():
        print(f"    {cat:20s} {n:6,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
