"""Verify GA HOAs are live on hoaproxy.org and emit a final state report."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

BASE = "https://hoaproxy.org"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", default="GA")
    p.add_argument("--out", default="state_scrapers/ga/results/final_state_report.json")
    p.add_argument("--sample", type=int, default=10)
    args = p.parse_args()

    state = args.state.upper()

    s = requests.Session()
    summary = s.get(f"{BASE}/hoas/summary", params={"state": state, "limit": 1, "offset": 0}, timeout=60).json()
    total = int(summary.get("total") or 0)
    print(f"/hoas/summary?state={state}: total={total}")

    map_points = s.get(f"{BASE}/hoas/map-points", params={"state": state}, timeout=60).json()
    n_mapped = len(map_points)
    print(f"/hoas/map-points?state={state}: returned={n_mapped}")

    # Pull a representative page to inspect chunk_count distribution
    sample = s.get(f"{BASE}/hoas/summary", params={"state": state, "limit": 500, "offset": 0}, timeout=120).json()
    results = sample.get("results") or []
    chunk_counts = [int(r.get("chunk_count") or 0) for r in results]
    zero_chunks = [r for r in results if not int(r.get("chunk_count") or 0)]
    nonzero_chunks = [c for c in chunk_counts if c]
    avg_chunks = (sum(nonzero_chunks) / len(nonzero_chunks)) if nonzero_chunks else 0
    print(
        f"chunk_count over first {len(results)} HOAs: "
        f"zero={len(zero_chunks)} nonzero={len(nonzero_chunks)} avg_nonzero={avg_chunks:.1f}"
    )

    # Sample HOAs lacking chunks (suggests ingestion gaps or only-rejected docs)
    print(f"sample (up to {args.sample}) HOAs with chunk_count=0:")
    for row in zero_chunks[: args.sample]:
        print(f"  {row.get('name')} :: docs={row.get('doc_count')} mapped={bool(row.get('latitude'))}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "state": state,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "total_hoas": total,
                "map_points": n_mapped,
                "page_size": len(results),
                "chunk_summary": {
                    "zero": len(zero_chunks),
                    "nonzero": len(nonzero_chunks),
                    "avg_nonzero": round(avg_chunks, 2),
                },
                "sample_zero_chunk": [r.get("name") for r in zero_chunks[: args.sample]],
            },
            indent=2,
            sort_keys=True,
        )
    )
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
