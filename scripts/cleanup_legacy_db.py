#!/usr/bin/env python3
"""One-shot cleanup of legacy state in the production DB.

PR-5 of the agent-paradigm cleanup. Run once after PR-1..4 are deployed.

Cleanups:
  1. Remove bulk-importer accounts created by deleted scripts
     (display_name in {Bulk Importer, Alexandria Bulk Importer, ...},
      email patterns ingest-*@example.com, bulk-importer-*@*.local)
     plus their sessions.
  2. Drop documents whose PDF file is missing on disk (orphans from
     interrupted bulk runs).
  3. Normalize hoa_locations.source enum to {agent, public_contributor,
     legacy_bulk_import}. Legacy values map:
       manual                          -> agent
       anonymous_upload                -> public_contributor
       alexandria_va_common_ownership  -> legacy_bulk_import
       trec_texas, breckenridge_*, ... -> legacy_bulk_import
       NULL/unknown                    -> legacy_bulk_import

Usage:
    python scripts/cleanup_legacy_db.py --dry-run     # report only
    python scripts/cleanup_legacy_db.py               # apply

Run against production by setting HOA_DB_PATH to a local copy of the prod DB,
or by SSH'ing into Render and running there.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hoaware import db
from hoaware.config import load_settings


_LEGACY_SOURCE_MAP = {
    "manual": "agent",
    "anonymous_upload": "public_contributor",
}
_KEEP_SOURCES = {"agent", "public_contributor", "legacy_bulk_import"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Cleanup legacy production DB state")
    ap.add_argument("--dry-run", action="store_true", help="Report only, no writes")
    args = ap.parse_args()

    settings = load_settings()
    print(f"Target DB: {settings.db_path}", flush=True)
    print(f"Docs root: {settings.docs_root}", flush=True)
    print(f"Mode:      {'DRY-RUN' if args.dry_run else 'APPLY'}", flush=True)
    print()

    with db.get_connection(settings.db_path) as conn:
        # ---------- 1. Bulk-importer accounts ----------
        bulk_users = conn.execute(
            """
            SELECT id, email, display_name FROM users
            WHERE display_name IN ('Bulk Importer','Alexandria Bulk Importer',
                                    'Nearby Cary HOA Importer','Agent Test')
               OR email LIKE 'ingest-%@example.com'
               OR email LIKE 'bulk-importer-%@%'
               OR email LIKE '%@hoaproxy-bulk.local'
            """
        ).fetchall()
        print(f"  Bulk-importer accounts to remove: {len(bulk_users)}")
        for u in bulk_users[:5]:
            print(f"    - id={u['id']}  email={u['email']}  name={u['display_name']}")
        if len(bulk_users) > 5:
            print(f"    ... and {len(bulk_users) - 5} more")

        if not args.dry_run and bulk_users:
            ids = [int(u["id"]) for u in bulk_users]
            placeholders = ",".join(["?"] * len(ids))
            conn.execute(f"DELETE FROM sessions WHERE user_id IN ({placeholders})", ids)
            conn.execute(f"DELETE FROM users WHERE id IN ({placeholders})", ids)

        # ---------- 2. Orphan documents (missing files on disk) ----------
        all_docs = conn.execute(
            """
            SELECT d.id, h.name AS hoa, d.relative_path
            FROM documents d JOIN hoas h ON h.id = d.hoa_id
            """
        ).fetchall()
        orphans = []
        for r in all_docs:
            path = settings.docs_root / r["relative_path"]
            if not path.exists():
                orphans.append((int(r["id"]), str(r["hoa"]), str(r["relative_path"])))
        print(f"\n  Orphan documents (missing on disk): {len(orphans)}")
        for oid, hoa, rel in orphans[:5]:
            print(f"    - {hoa} / {rel}")
        if len(orphans) > 5:
            print(f"    ... and {len(orphans) - 5} more")

        if not args.dry_run and orphans:
            ids = [oid for oid, _, _ in orphans]
            placeholders = ",".join(["?"] * len(ids))
            # chunks ON DELETE CASCADE handles the chunks/embeddings
            conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", ids)

        # ---------- 3. Normalize hoa_locations.source ----------
        source_rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM hoa_locations GROUP BY source"
        ).fetchall()
        print(f"\n  Source distribution before normalization:")
        for r in source_rows:
            print(f"    - {r['source'] or '(NULL)'}: {r['n']:,}")

        rename_map: dict[str, str] = {}
        for r in source_rows:
            src = r["source"]
            if src is None or src == "":
                rename_map[src] = "legacy_bulk_import"
            elif src in _KEEP_SOURCES:
                continue  # already canonical
            elif src in _LEGACY_SOURCE_MAP:
                rename_map[src] = _LEGACY_SOURCE_MAP[src]
            else:
                # Anything else is from a per-corpus uploader: lump into legacy
                rename_map[src] = "legacy_bulk_import"

        print(f"\n  Renames to apply: {len(rename_map)}")
        for old, new in rename_map.items():
            print(f"    - {old or '(NULL)'} -> {new}")

        if not args.dry_run:
            for old, new in rename_map.items():
                if old is None:
                    conn.execute(
                        "UPDATE hoa_locations SET source = ? WHERE source IS NULL",
                        (new,),
                    )
                else:
                    conn.execute(
                        "UPDATE hoa_locations SET source = ? WHERE source = ?",
                        (new, old),
                    )

        if not args.dry_run:
            conn.commit()
            print("\n  Committed.")
        else:
            print("\n  DRY-RUN — no writes made.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
