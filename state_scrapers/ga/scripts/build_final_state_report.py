"""Compose the final GA state report once import + cleanup are done."""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import requests
from google.cloud import storage

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def _count_bank_manifests(bucket: storage.Bucket, prefix: str) -> int:
    n = 0
    for blob in bucket.list_blobs(prefix=prefix):
        if blob.name.endswith("/manifest.json"):
            n += 1
    return n


def _scan_prepared(bucket: storage.Bucket, prefix: str) -> dict:
    by_status: Counter[str] = Counter()
    n_bundles = 0
    n_docs = 0
    for blob in bucket.list_blobs(prefix=prefix):
        if blob.name.endswith("/status.json"):
            try:
                data = json.loads(blob.download_as_bytes())
            except Exception:
                continue
            by_status[data.get("status") or "?"] += 1
        elif blob.name.endswith("/bundle.json"):
            n_bundles += 1
            try:
                payload = json.loads(blob.download_as_bytes())
                n_docs += len(payload.get("documents") or [])
            except Exception:
                pass
    return {"bundles": n_bundles, "documents": n_docs, "by_status": dict(by_status)}


def _scan_live(base_url: str, state: str) -> dict:
    rows: list[dict] = []
    offset = 0
    while True:
        r = requests.get(
            f"{base_url}/hoas/summary",
            params={"state": state, "limit": 500, "offset": offset},
            timeout=120,
        )
        r.raise_for_status()
        payload = r.json()
        batch = payload.get("results") or []
        rows.extend(batch)
        if len(rows) >= int(payload.get("total") or 0) or not batch:
            break
        offset += len(batch)
    map_points = requests.get(
        f"{base_url}/hoas/map-points", params={"state": state}, timeout=60
    ).json()
    quality_counter: Counter[str] = Counter()
    docs_total = 0
    chunks_total = 0
    zero_chunk_hoas: list[str] = []
    for row in rows:
        docs_total += int(row.get("doc_count") or 0)
        chunk_count = int(row.get("chunk_count") or 0)
        chunks_total += chunk_count
        if chunk_count == 0:
            zero_chunk_hoas.append(row.get("hoa") or "?")
        if row.get("boundary_geojson"):
            quality_counter["polygon_or_address"] += 1
        elif row.get("latitude") is not None:
            quality_counter["point"] += 1
        else:
            quality_counter["unmapped"] += 1
    return {
        "live_profiles": len(rows),
        "live_documents": docs_total,
        "live_chunks": chunks_total,
        "map_points": len(map_points),
        "map_rate": round(len(map_points) / max(1, len(rows)), 4),
        "by_quality": dict(quality_counter),
        "zero_chunk_count": len(zero_chunk_hoas),
        "zero_chunk_sample": zero_chunk_hoas[:10],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", default="GA")
    p.add_argument("--base-url", default="https://hoaproxy.org")
    p.add_argument("--bank-bucket", default="hoaproxy-bank")
    p.add_argument("--prepared-bucket", default="hoaproxy-ingest-ready")
    p.add_argument("--out", default="state_scrapers/ga/results/final_state_report.json")
    args = p.parse_args()

    state = args.state.upper()
    client = storage.Client()
    bank = client.bucket(args.bank_bucket)
    prepared = client.bucket(args.prepared_bucket)

    print("counting bank...")
    bank_n = _count_bank_manifests(bank, f"v1/{state}/")
    print(f"  bank manifests: {bank_n}")

    print("scanning prepared...")
    prepared_summary = _scan_prepared(prepared, f"v1/{state}/")
    print(f"  prepared bundles: {prepared_summary['bundles']}")
    print(f"  prepared documents: {prepared_summary['documents']}")
    print(f"  by status: {prepared_summary['by_status']}")

    print("scanning live...")
    live_summary = _scan_live(args.base_url, state)
    print(f"  live HOAs: {live_summary['live_profiles']}")
    print(f"  live documents: {live_summary['live_documents']}")
    print(f"  live chunks: {live_summary['live_chunks']}")
    print(f"  map_points: {live_summary['map_points']}")
    print(f"  map_rate: {live_summary['map_rate']}")

    payload = {
        "state": state,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "raw_bank_manifests": bank_n,
        "prepared": prepared_summary,
        "live": live_summary,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
