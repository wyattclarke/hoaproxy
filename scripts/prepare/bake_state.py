#!/usr/bin/env python3
"""Bulk-bake all v1 prepared bundles for a state into v2.

Enumerates ``gs://{bucket}/v1/{STATE}/...`` for bundle.json blobs and runs
``bake_bundle.bake_one_bundle`` over each in parallel. Idempotent: bundles
already baked (chunks sidecar exists + validates against the configured
model) are skipped.

Per-state ledger lands at
``state_scrapers/_orchestrator/bake_{date}/{state}_bake.jsonl``.

Cost guard: refuses to start without ``--max-budget-usd``. Empirical cost
is ~$0.0004/doc for ``text-embedding-3-small`` on typical CCR-sized docs;
default budget 50 USD covers ~125k docs.

Usage::

    set -a; source settings.env; set +a
    .venv/bin/python scripts/prepare/bake_state.py \\
        --state FL --workers 8 --max-budget-usd 20
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import storage as gcs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hoaware import prepared_ingest  # noqa: E402
from openai import OpenAI  # noqa: E402

from bake_bundle import bake_one_bundle  # noqa: E402  (sibling module)

DEFAULT_BUCKET = os.environ.get(
    "HOA_PREPARED_GCS_BUCKET", prepared_ingest.DEFAULT_PREPARED_BUCKET
)
DEFAULT_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
BUNDLE_RE = re.compile(r"^(v1/[^/]+/[^/]+/[^/]+/[^/]+)/bundle\.json$")


def list_bundle_prefixes(bucket: gcs.Bucket, state: str) -> list[str]:
    state_n = state.strip().upper()
    prefix = f"v1/{state_n}/"
    out: list[str] = []
    for blob in bucket.list_blobs(prefix=prefix):
        m = BUNDLE_RE.match(blob.name)
        if m:
            out.append(m.group(1))
    return out


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", required=True)
    ap.add_argument("--bucket", default=DEFAULT_BUCKET)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--chunk-char-limit", type=int,
                    default=int(os.environ.get("HOA_CHUNK_CHAR_LIMIT", "1800")))
    ap.add_argument("--chunk-overlap", type=int,
                    default=int(os.environ.get("HOA_CHUNK_OVERLAP", "200")))
    ap.add_argument("--max-budget-usd", type=float, required=True,
                    help="Refuse to start without an explicit OpenAI budget cap")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap number of bundles processed (0=all)")
    ap.add_argument("--force", action="store_true",
                    help="Re-bake even if sidecar exists")
    ap.add_argument("--run-id",
                    default=f"bake_{datetime.now(timezone.utc).strftime('%Y_%m_%d')}")
    args = ap.parse_args()

    state_n = args.state.strip().upper()
    client = gcs.Client()
    bucket = client.bucket(args.bucket)
    openai_client = OpenAI()

    print(f"Listing v1 bundles for {state_n} in gs://{args.bucket}/v1/{state_n}/ ...",
          file=sys.stderr)
    prefixes = list_bundle_prefixes(bucket, state_n)
    print(f"  found {len(prefixes)} bundles", file=sys.stderr)
    if args.limit:
        prefixes = prefixes[:args.limit]
        print(f"  --limit {args.limit} → processing {len(prefixes)}", file=sys.stderr)

    # Coarse cost estimate: assume avg 8 docs/bundle × $0.0004/doc; refuse
    # if estimated > budget.
    est_cost = len(prefixes) * 8 * 0.0004
    if est_cost > args.max_budget_usd:
        print(
            f"FATAL: estimated OpenAI cost ${est_cost:.2f} exceeds "
            f"--max-budget-usd ${args.max_budget_usd:.2f}. "
            f"Increase budget or use --limit.",
            file=sys.stderr,
        )
        return 2
    print(f"  estimated OpenAI cost ≤ ${est_cost:.2f} "
          f"(budget ${args.max_budget_usd:.2f})", file=sys.stderr)

    run_dir = ROOT / "state_scrapers" / "_orchestrator" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = run_dir / f"{state_n.lower()}_bake.jsonl"

    totals = {"ok": 0, "errors": 0, "baked": 0, "skipped": 0}
    t0 = time.monotonic()
    with ledger_path.open("a", encoding="utf-8") as ledger:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {
                ex.submit(
                    bake_one_bundle,
                    bucket=bucket,
                    prefix=prefix,
                    embedding_model=args.model,
                    chunk_char_limit=args.chunk_char_limit,
                    chunk_overlap=args.chunk_overlap,
                    openai_client=openai_client,
                    force=args.force,
                ): prefix
                for prefix in prefixes
            }
            for i, fut in enumerate(as_completed(futures), 1):
                prefix = futures[fut]
                try:
                    result = fut.result()
                    totals["ok"] += 1
                    totals["baked"] += int(result.get("baked", 0))
                    totals["skipped"] += int(result.get("skipped", 0))
                except Exception as e:
                    result = {"prefix": prefix, "status": "error",
                              "error": f"{type(e).__name__}: {e}"}
                    totals["errors"] += 1
                ledger.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    **result,
                }, ensure_ascii=False) + "\n")
                ledger.flush()
                if i % 25 == 0:
                    elapsed = time.monotonic() - t0
                    rate = i / elapsed if elapsed > 0 else 0
                    print(
                        f"  [{i}/{len(prefixes)}] ok={totals['ok']} "
                        f"errors={totals['errors']} baked={totals['baked']} "
                        f"skipped={totals['skipped']} ({rate:.1f}/s)",
                        file=sys.stderr,
                    )

    summary = {
        "state": state_n,
        "run_date": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.monotonic() - t0, 1),
        "bundles": len(prefixes),
        "totals": totals,
        "embedding_model": args.model,
        "ledger_path": str(ledger_path.relative_to(ROOT)),
    }
    (run_dir / f"{state_n.lower()}_summary.json").write_text(
        json.dumps(summary, indent=2)
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
