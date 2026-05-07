"""Count prepared GA bundles by location_quality."""
from __future__ import annotations

import json
import sys
from collections import Counter

from google.cloud import storage

BUCKET = "hoaproxy-ingest-ready"
PREFIX = "v1/GA/"


def main() -> int:
    client = storage.Client()
    bucket = client.bucket(BUCKET)
    by_quality: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    total = 0
    samples_unmapped: list[tuple[str, str]] = []
    for blob in bucket.list_blobs(prefix=PREFIX):
        if not blob.name.endswith("/bundle.json"):
            continue
        total += 1
        try:
            payload = json.loads(blob.download_as_bytes())
        except Exception:
            by_quality["error"] += 1
            continue
        geometry = payload.get("geometry") or {}
        loc = (geometry.get("location_quality") or "missing").strip().lower()
        has_geo = bool(geometry.get("boundary_geojson")) or (
            geometry.get("latitude") is not None and geometry.get("longitude") is not None
        )
        by_quality[loc if has_geo else "unmapped"] += 1
        if not has_geo and len(samples_unmapped) < 10:
            samples_unmapped.append((payload.get("hoa_name") or "?", blob.name))
        # status sidecar
        status_blob = bucket.blob(blob.name.removesuffix("/bundle.json") + "/status.json")
        try:
            status = json.loads(status_blob.download_as_bytes())
            by_status[status.get("status") or "?"] += 1
        except Exception:
            by_status["status_err"] += 1
    print(f"prepared GA bundles: {total}")
    print("by location_quality:")
    for q, c in by_quality.most_common():
        print(f"  {q}: {c} ({c / max(total,1):.0%})")
    print("by status:")
    for q, c in by_status.most_common():
        print(f"  {q}: {c}")
    print("samples lacking geocode:")
    for hoa, bn in samples_unmapped:
        print(f"  {hoa} :: gs://{BUCKET}/{bn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
