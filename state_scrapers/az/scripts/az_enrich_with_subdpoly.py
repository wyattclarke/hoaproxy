#!/usr/bin/env python3
"""Cross-link AZ bank manifests to Maricopa/Pima subdivision polygons.

For each manifest in `gs://hoaproxy-bank/v1/AZ/{maricopa,pima}/...`:
  1. Build a token-set from the manifest's name (and slug as fallback).
  2. Find the best-Jaccard subdpoly entry from `az_subdpoly.jsonl` whose
     county matches the manifest's bank-prefix county.
  3. Strong (>=0.50): adopt the subdpoly polygon's centroid + boundary.
     Stamp `geometry.source = "subdpoly-polygon"`.
     Weak (0.35..0.49): log to weak-match ledger, no write.
     None: skip.

Writes geometry back via direct gsutil read-modify-write.

Idempotent: skips manifests whose geometry.source is already
"subdpoly-polygon".

Usage:
    source .venv/bin/activate
    python state_scrapers/az/scripts/az_enrich_with_subdpoly.py \\
        --apply [--counties maricopa,pima] [--limit-per-county N]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SEED_PATH = ROOT / "state_scrapers" / "az" / "data" / "az_subdpoly.jsonl"
RESULTS_DIR = ROOT / "state_scrapers" / "az" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

BANK_BUCKET = "hoaproxy-bank"
BANK_PREFIX = f"gs://{BANK_BUCKET}/v1/AZ"

SLUG_NORMALIZE = re.compile(r"[^a-z0-9]+")
STOPWORDS = {
    "the", "a", "an", "of", "at", "in", "on", "and", "or",
    "hoa", "homeowners", "homeowner", "association", "associations",
    "condo", "condos", "condominium", "condominiums",
    "club", "estates", "homes", "place", "park", "village", "villas",
    "ccr", "ccrs", "covenants", "declaration", "bylaws", "amendment",
    "articles", "incorporation", "rules", "by",
    "inc", "llc", "ltd", "corp", "co",
    # AZ-frequent suffix tokens
    "az", "arizona",
}


def slugify(name: str) -> set[str]:
    s = SLUG_NORMALIZE.sub(" ", (name or "").lower()).strip()
    return {t for t in s.split() if t and t not in STOPWORDS and len(t) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def gcs_read_json(uri: str) -> dict | None:
    try:
        r = subprocess.run(
            ["gsutil", "cat", uri],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


def gcs_write_json(uri: str, data: dict) -> bool:
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    try:
        r = subprocess.run(
            ["gsutil", "cp", "-", uri],
            input=payload, capture_output=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


def list_county_manifests(county_slug: str) -> list[str]:
    cmd = ["gsutil", "ls", f"{BANK_PREFIX}/{county_slug}/**/manifest.json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as e:
        print(f"  [{county_slug}] gsutil ls failed: {e}", file=sys.stderr)
        return []
    out = r.stdout or ""
    return [l.strip() for l in out.splitlines() if l.strip().endswith("manifest.json")]


def load_seed_for_county(county: str) -> tuple[list[dict], dict[str, list[int]], list[set[str]]]:
    """Return (rows, token_index, candidate_token_sets)."""
    if not SEED_PATH.exists():
        raise FileNotFoundError(f"missing seed: {SEED_PATH}")
    rows: list[dict] = []
    cand_tokens: list[set[str]] = []
    token_idx: dict[str, list[int]] = defaultdict(list)
    with SEED_PATH.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (row.get("county") or "").lower() != county:
                continue
            toks = slugify(row.get("name") or "")
            if not toks:
                continue
            i = len(rows)
            rows.append(row)
            cand_tokens.append(toks)
            for t in toks:
                token_idx[t].append(i)
    return rows, token_idx, cand_tokens


def find_best(slug_toks: set[str], token_idx: dict[str, list[int]],
              cand_tokens: list[set[str]]) -> tuple[int, float]:
    if not slug_toks:
        return -1, 0.0
    cand_ix: set[int] = set()
    for t in slug_toks:
        cand_ix.update(token_idx.get(t, []))
    best = (-1, 0.0)
    for i in cand_ix:
        j = jaccard(slug_toks, cand_tokens[i])
        if j > best[1]:
            best = (i, j)
    return best


def enrich_county(
    county: str,
    apply: bool,
    limit: int | None,
    weak_log_path: Path,
) -> dict:
    print(f"\n=== {county} ===")
    rows, token_idx, cand_tokens = load_seed_for_county(county)
    if not rows:
        return {"reason": "no_seed_rows"}
    print(f"  seed: {len(rows):,} subdivision polygons")

    manifests = list_county_manifests(county)
    print(f"  manifests in bank: {len(manifests):,}")
    if limit:
        manifests = manifests[:limit]

    stats: Counter = Counter()
    weak_rows: list[dict] = []
    started = time.time()

    for i, uri in enumerate(manifests):
        m = gcs_read_json(uri)
        if not m:
            stats["read_fail"] += 1
            continue
        existing_geom = m.get("geometry") or {}
        if existing_geom.get("source") == "subdpoly-polygon":
            stats["skip_already_subdpoly"] += 1
            continue
        slug = uri.removeprefix(f"{BANK_PREFIX}/{county}/").rsplit("/manifest.json", 1)[0]
        slug_toks = slugify(m.get("name") or slug)
        if not slug_toks:
            slug_toks = slugify(slug)
        if not slug_toks:
            stats["empty_slug_tokens"] += 1
            continue
        idx, score = find_best(slug_toks, token_idx, cand_tokens)
        if score < 0.35:
            stats["no_match"] += 1
            continue
        if score < 0.50:
            stats["weak"] += 1
            weak_rows.append({
                "manifest_uri": uri,
                "county": county,
                "slug": slug,
                "manifest_name": m.get("name"),
                "best_subdpoly_name": rows[idx]["name"],
                "score": round(score, 3),
            })
            continue
        # Strong match.
        seed_row = rows[idx]
        new_geom = {
            "source": "subdpoly-polygon",
            "confidence": "subdpoly-polygon",
            "centroid_lat": seed_row["centroid"]["lat"],
            "centroid_lon": seed_row["centroid"]["lon"],
            # Also write canonical latitude/longitude so prepare_bank's
            # Nominatim guard (`geometry.get("latitude") is not None`) skips
            # this manifest in Phase 7.
            "latitude": seed_row["centroid"]["lat"],
            "longitude": seed_row["centroid"]["lon"],
            "boundary_geojson": seed_row.get("boundary_geojson"),
            "area_acres": seed_row.get("area_acres"),
            "match": {
                "subdpoly_name": seed_row["name"],
                "score": round(score, 3),
                "source_county": county,
            },
            "enriched_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        stats["strong"] += 1

        if not apply:
            continue

        audits = m.setdefault("audit", {})
        audits.setdefault("geometry_history", []).append({
            "previous_source": existing_geom.get("source"),
            "previous_confidence": existing_geom.get("confidence"),
            "replaced_at": new_geom["enriched_at"],
            "replaced_by": "subdpoly-polygon",
        })
        m["geometry"] = new_geom
        if gcs_write_json(uri, m):
            stats["write_ok"] += 1
        else:
            stats["write_fail"] += 1

        if (i + 1) % 50 == 0:
            elapsed = time.time() - started
            print(
                f"  [{county}] {i+1}/{len(manifests)} | "
                f"strong={stats['strong']} weak={stats['weak']} "
                f"no_match={stats['no_match']} skip={stats['skip_already_subdpoly']} | {elapsed:.0f}s"
            )

    if weak_rows:
        with open(weak_log_path, "a") as f:
            for row in weak_rows:
                f.write(json.dumps(row) + "\n")

    return dict(stats)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Write geometry updates to GCS (default: dry-run, stats only).")
    p.add_argument("--counties", default="maricopa,pima",
                   help="Comma-separated AZ county slugs to process.")
    p.add_argument("--limit-per-county", type=int, default=None)
    args = p.parse_args()

    counties = [c.strip() for c in args.counties.split(",") if c.strip()]
    started = time.time()
    weak_log_path = RESULTS_DIR / f"az_subdpoly_weak_matches_{int(started)}.jsonl"
    summary: dict[str, dict] = {}

    for county in counties:
        try:
            stats = enrich_county(
                county, apply=args.apply,
                limit=args.limit_per_county,
                weak_log_path=weak_log_path,
            )
        except Exception as e:
            print(f"  [{county}] FAILED: {e}", file=sys.stderr)
            stats = {"error": str(e)}
        summary[county] = stats
        out_path = RESULTS_DIR / f"az_subdpoly_summary_{int(started)}.json"
        out_path.write_text(json.dumps(summary, indent=2, sort_keys=True))

    elapsed = time.time() - started
    print("\n=== AZ subdpoly enrichment summary ===")
    total_strong = 0
    total_weak = 0
    for county, stats in sorted(summary.items()):
        s = stats.get("strong", 0)
        w = stats.get("weak", 0)
        n = stats.get("no_match", 0)
        sk = stats.get("skip_already_subdpoly", 0)
        wo = stats.get("write_ok", 0)
        total_strong += s
        total_weak += w
        print(f"  {county:20s}  strong={s:5d}  weak={w:5d}  no_match={n:5d}"
              f"  skip_existing={sk:5d}  write_ok={wo:5d}")
    print(f"\nTotal strong: {total_strong}")
    print(f"Total weak:   {total_weak}")
    print(f"Wall time:    {elapsed/60:.1f} min")
    print(f"Weak log:     {weak_log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
