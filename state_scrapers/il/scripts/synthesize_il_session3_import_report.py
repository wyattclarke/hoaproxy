"""Synthesize a session-3 live_import_report.json from the IL bank.

The session-3 prepare phase didn't produce a live_import_report.json (its
ledger is at the prepare layer, not the import layer). Phase-10 cleanup
needs name→bank-prefix mappings for new IL HOAs (Wave B round-2, Wave C,
owned-domain mining). This walks every `manifest.json` under
`gs://hoaproxy-bank/v1/IL/` and emits a report shaped like the live
auto-importer's responses, so `dedup_and_clean_il_downstate.py
--name-to-prefix` consumes it without modification.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from google.cloud import storage


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bucket", default="hoaproxy-bank")
    p.add_argument("--state", default="IL")
    p.add_argument("--out", required=True, help="Output live_import_report.json path")
    p.add_argument("--workers", type=int, default=24)
    args = p.parse_args()

    client = storage.Client()
    bucket = client.bucket(args.bucket)
    prefix = f"v1/{args.state}/"

    blobs = list(bucket.list_blobs(prefix=prefix))
    manifest_blobs = [b for b in blobs if b.name.endswith("/manifest.json")]
    print(f"manifests under {prefix}: {len(manifest_blobs)}", file=sys.stderr)

    def fetch(blob) -> dict | None:
        try:
            payload = json.loads(blob.download_as_bytes())
        except Exception:
            return None
        name = payload.get("name") or ""
        if not name:
            return None
        # Blob name: v1/IL/{county}/{slug}/manifest.json
        # Prefix: v1/IL/{county}/{slug}
        prefix = blob.name[: -len("/manifest.json")]
        return {"hoa": name, "prefix": prefix}

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(fetch, b) for b in manifest_blobs]
        for i, f in enumerate(as_completed(futures), 1):
            r = f.result()
            if r:
                rows.append(r)
            if i % 500 == 0:
                print(f"  fetched {i}/{len(manifest_blobs)}", file=sys.stderr)

    print(f"valid name->prefix entries: {len(rows)}", file=sys.stderr)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"responses": [{"body": {"results": rows}}]}, indent=2))
    print(f"wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
