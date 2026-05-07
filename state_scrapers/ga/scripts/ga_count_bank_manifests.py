"""Count raw GA bank manifests for the final state report."""
from __future__ import annotations

import json
import sys
from collections import Counter

from google.cloud import storage


def main() -> int:
    c = storage.Client()
    b = c.bucket("hoaproxy-bank")
    by_county: Counter[str] = Counter()
    docs_total = 0
    for blob in b.list_blobs(prefix="v1/GA/"):
        if not blob.name.endswith("/manifest.json"):
            continue
        try:
            payload = json.loads(blob.download_as_bytes())
        except Exception:
            continue
        parts = blob.name.split("/")
        cty = parts[2] if len(parts) > 2 else "?"
        by_county[cty] += 1
        docs = payload.get("documents") or []
        docs_total += len(docs)
    print(f"raw GA manifests: {sum(by_county.values())}")
    print(f"raw GA documents (manifest entries): {docs_total}")
    print(f"counties represented: {len(by_county)}")
    print("--- top 15 by manifest count ---")
    for c, n in by_county.most_common(15):
        print(f"  {c}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
