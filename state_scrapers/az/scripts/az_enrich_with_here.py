#!/usr/bin/env python3
"""Promote FL bank manifests from ZIP-centroid geometry to HERE address-precision lat/lon.

Usage:
    source .venv/bin/activate
    set -a; source settings.env 2>/dev/null; set +a

    # Phase 1: pre-flight audit (no API calls). Writes counts to results/az_here_audit.json.
    python state_scrapers/az/scripts/az_enrich_with_here.py --audit

    # Phase 2: 10-manifest smoke test (real HERE calls).
    python state_scrapers/az/scripts/az_enrich_with_here.py --smoke

    # Phase 3: full pass (DO NOT run until OSM finishes; user triggers).
    python state_scrapers/az/scripts/az_enrich_with_here.py --run [--limit N] [--rate-per-sec 4]

Idempotent: skips manifests already at geometry.source == "here-address" and won't downgrade
existing OSM polygon geometry. Re-runs hit a local response cache.

HERE Geocoder docs: https://developer.here.com/documentation/geocoding-search-api/
- Endpoint: GET https://geocode.search.hereapi.com/v1/geocode
- Auth:     ?apikey=<HERE_API_KEY>
- Free tier: 30,000 req/month, 5 req/sec. We pace at 4 req/sec for headroom.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = REPO_ROOT / "state_scrapers" / "fl" / "results"
CACHE_DIR = REPO_ROOT / "state_scrapers" / "fl" / "cache"
AUDIT_JSON = RESULTS_DIR / "az_here_audit.json"
SMOKE_JSON = RESULTS_DIR / "az_here_smoke.json"
CACHE_JSON = CACHE_DIR / "here_geocode_cache.json"

BANK_BUCKET = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")
BANK_PREFIX = f"gs://{BANK_BUCKET}/v1/AZ"

HERE_ENDPOINT = "https://geocode.search.hereapi.com/v1/geocode"

# Sources that mean "ZIP-centroid fallback" — i.e. eligible for HERE promotion.
ZIP_FALLBACK_SOURCES = {
    "sunbiz-zip-centroid",
    "sunbiz-mailing-address",
    "sunbiz-principal-address",
}

# Sources that mean "already promoted" — idempotency guard.
ALREADY_HERE_SOURCE = "here-address"

# OSM polygon source — never downgrade this with a HERE point.
OSM_PLACE_SOURCE = "osm-place"


# ---------------------------------------------------------------------------
# GCS helpers (mirrors az enrichers.py)
# ---------------------------------------------------------------------------

def gcs_read_json(uri: str) -> dict | None:
    try:
        result = subprocess.run(
            ["gsutil", "cat", uri],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception:
        return None


def gcs_write_json(uri: str, data: dict) -> bool:
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    try:
        result = subprocess.run(
            ["gsutil", "cp", "-", uri],
            input=payload, capture_output=True, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def list_az_manifests() -> list[str]:
    print(f"[list] Listing FL manifests under {BANK_PREFIX} (may take 30-60s) ...")
    t0 = time.time()
    result = subprocess.run(
        ["gsutil", "ls", f"{BANK_PREFIX}/**/manifest.json"],
        capture_output=True, text=True, timeout=300,
    )
    uris = [l.strip() for l in result.stdout.strip().split("\n") if l.strip().endswith("manifest.json")]
    print(f"[list] Found {len(uris)} FL manifests in {time.time()-t0:.1f}s.")
    return uris


# ---------------------------------------------------------------------------
# Classification (Phase 1)
# ---------------------------------------------------------------------------

def classify(manifest: dict) -> str:
    addr = manifest.get("address") or {}
    geo = manifest.get("geometry") or {}
    geo_source = geo.get("source")

    if geo_source == ALREADY_HERE_SOURCE:
        return "skip_already_here"

    # Don't downgrade an OSM polygon match.
    if geo_source == OSM_PLACE_SOURCE:
        return "skip_has_osm_polygon"

    street = (addr.get("street") or "").strip()
    if not street:
        return "skip_no_street"

    if geo_source in ZIP_FALLBACK_SOURCES:
        return "eligible"

    return "skip_other"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache: dict[str, dict] = {}
_cache_dirty = False


def _load_cache() -> None:
    global _cache
    if CACHE_JSON.exists():
        try:
            with open(CACHE_JSON) as f:
                _cache = json.load(f)
        except Exception:
            _cache = {}
    else:
        _cache = {}


def _save_cache() -> None:
    global _cache_dirty
    if not _cache_dirty:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_JSON.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(_cache, f, sort_keys=True)
    tmp.replace(CACHE_JSON)
    _cache_dirty = False


# ---------------------------------------------------------------------------
# HERE Geocoder
# ---------------------------------------------------------------------------

def _build_query(addr: dict) -> str:
    """Build a free-form address query string from manifest address fields."""
    parts: list[str] = []
    street = (addr.get("street") or "").strip()
    city = (addr.get("city") or "").strip()
    state = (addr.get("state") or "AZ").strip() or "AZ"
    postal = (addr.get("postal_code") or "").strip()
    if street:
        parts.append(street)
    if city:
        parts.append(city)
    sp = state
    if postal:
        sp = f"{state} {postal}".strip()
    if sp:
        parts.append(sp)
    parts.append("USA")
    return ", ".join(parts)


def here_geocode(query: str, api_key: str, timeout: float = 15.0) -> dict | None:
    """Call HERE Geocoder. Returns parsed JSON dict or None on transport error."""
    global _cache_dirty
    if query in _cache:
        return _cache[query]

    params = {
        "q": query,
        "in": "countryCode:USA",
        "limit": "5",
        "apikey": api_key,
    }
    url = f"{HERE_ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "hoaproxy-fl-here/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"_error": str(e)}

    _cache[query] = data
    _cache_dirty = True
    return data


def pick_best_item(payload: dict) -> dict | None:
    """Filter HERE response items: prefer houseNumber > street; reject place/locality fallbacks."""
    if not payload or "items" not in payload:
        return None
    items = payload.get("items") or []
    if not items:
        return None
    # Sort by preference: houseNumber > street > everything else (rejected)
    rank = {"houseNumber": 0, "street": 1}
    candidates = [it for it in items if it.get("resultType") in rank]
    if not candidates:
        return None
    candidates.sort(key=lambda it: (rank[it.get("resultType")], -float(it.get("scoring", {}).get("queryScore") or 0)))
    return candidates[0]


def build_here_geometry(item: dict, query: str) -> dict | None:
    """Convert a HERE item into a manifest geometry block."""
    pos = item.get("position") or {}
    lat = pos.get("lat")
    lon = pos.get("lng")
    if lat is None or lon is None:
        return None
    map_view = item.get("mapView") or {}
    bbox = None
    if all(k in map_view for k in ("south", "west", "north", "east")):
        bbox = [
            float(map_view["south"]),
            float(map_view["west"]),
            float(map_view["north"]),
            float(map_view["east"]),
        ]
    return {
        "centroid": {"lat": float(lat), "lon": float(lon)},
        "source": ALREADY_HERE_SOURCE,
        "result_type": item.get("resultType"),
        "query_score": float((item.get("scoring") or {}).get("queryScore") or 0.0),
        "bbox": bbox,
        "confidence": "address-precision",
        "matched_query": query,
        "matched_address": (item.get("address") or {}).get("label"),
    }


# ---------------------------------------------------------------------------
# Manifest patching (overwrite geometry; do not touch address.*)
# ---------------------------------------------------------------------------

def patch_manifest_geometry(uri: str, manifest: dict, new_geometry: dict, dry_run: bool) -> bool:
    updated = dict(manifest)
    updated["geometry"] = new_geometry
    sources = list(manifest.get("metadata_sources") or [])
    sources.append({
        "source": "here-geocoder-enrichment",
        "result_type": new_geometry.get("result_type"),
        "query_score": new_geometry.get("query_score"),
        "matched_query": new_geometry.get("matched_query"),
        "fields_provided": ["geometry.centroid", "geometry.bbox"],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    updated["metadata_sources"] = sources
    if dry_run:
        return True
    return gcs_write_json(uri, updated)


# ---------------------------------------------------------------------------
# Path parsing
# ---------------------------------------------------------------------------

def parse_uri(uri: str) -> tuple[str, str] | None:
    rel = uri.replace(f"gs://{BANK_BUCKET}/v1/AZ/", "")
    if rel.endswith("/manifest.json"):
        rel = rel[: -len("/manifest.json")]
    parts = rel.split("/")
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


# ---------------------------------------------------------------------------
# Phase 1: audit
# ---------------------------------------------------------------------------

def phase1_audit() -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    uris = list_az_manifests()
    counts = Counter()
    eligible_uris: list[str] = []
    eligible_by_county: Counter = Counter()
    completed = 0
    n = len(uris)

    def _classify_one(uri: str):
        m = gcs_read_json(uri)
        if m is None:
            return uri, None, "read_fail"
        return uri, m, classify(m)

    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = [ex.submit(_classify_one, u) for u in uris]
        for fut in as_completed(futures):
            uri, _m, bucket = fut.result()
            counts[bucket] += 1
            if bucket == "eligible":
                eligible_uris.append(uri)
                parsed = parse_uri(uri)
                if parsed:
                    eligible_by_county[parsed[0]] += 1
            completed += 1
            if completed % 500 == 0:
                print(f"  ... audited {completed}/{n}; counts so far: {dict(counts)}")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_manifests": len(uris),
        "counts": dict(counts),
        "estimated_here_requests": counts["eligible"],
        "estimated_pct_of_30k_free_tier": round(counts["eligible"] / 30000.0 * 100, 1),
        "eligible_by_county_top20": dict(eligible_by_county.most_common(20)),
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_JSON, "w") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
    # Also write the eligible URI list as a sidecar for Phase 2/3 to consume.
    eligible_list = AUDIT_JSON.with_name("az_here_eligible_uris.txt")
    with open(eligible_list, "w") as f:
        for u in eligible_uris:
            f.write(u + "\n")

    print()
    print("=" * 70)
    print("PHASE 1 AUDIT")
    print(f"  Total manifests:              {len(uris)}")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {k:30s} {v}")
    print(f"  Eligible HERE requests:       {counts['eligible']}")
    print(f"  Free tier usage:              {summary['estimated_pct_of_30k_free_tier']}% of 30k/mo")
    print(f"  Audit JSON:                   {AUDIT_JSON}")
    print(f"  Eligible URI list:            {eligible_list}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Phase 2: smoke
# ---------------------------------------------------------------------------

def phase2_smoke(api_key: str, dry_run: bool) -> None:
    eligible_list = AUDIT_JSON.with_name("az_here_eligible_uris.txt")
    if not eligible_list.exists():
        print(f"[smoke] {eligible_list} missing; run --audit first.")
        sys.exit(2)
    with open(eligible_list) as f:
        all_uris = [l.strip() for l in f if l.strip()]

    # Pick 10 from a mix of counties: walk in order, take first per county until we have 10.
    seen_counties: set[str] = set()
    picked: list[str] = []
    for u in all_uris:
        parsed = parse_uri(u)
        if parsed and parsed[0] not in seen_counties:
            seen_counties.add(parsed[0])
            picked.append(u)
        if len(picked) >= 10:
            break
    if len(picked) < 10:
        # Top up from remaining to reach 10
        for u in all_uris:
            if u not in picked:
                picked.append(u)
            if len(picked) >= 10:
                break

    _load_cache()
    examples: list[dict] = []
    for uri in picked:
        manifest = gcs_read_json(uri)
        if manifest is None:
            continue
        addr = manifest.get("address") or {}
        query = _build_query(addr)
        payload = here_geocode(query, api_key)
        time.sleep(0.25)  # 4 req/sec ceiling
        item = pick_best_item(payload or {})
        new_geo = build_here_geometry(item, query) if item else None

        ex = {
            "uri": uri,
            "name": manifest.get("name"),
            "query": query,
            "old_geometry_source": (manifest.get("geometry") or {}).get("source"),
            "old_centroid": (manifest.get("geometry") or {}).get("centroid"),
            "matched_address": (item.get("address") or {}).get("label") if item else None,
            "result_type": item.get("resultType") if item else None,
            "query_score": (item.get("scoring") or {}).get("queryScore") if item else None,
            "new_centroid": new_geo.get("centroid") if new_geo else None,
            "patched": False,
        }
        if new_geo:
            ok = patch_manifest_geometry(uri, manifest, new_geo, dry_run=dry_run)
            ex["patched"] = ok
            ex["dry_run"] = dry_run
        examples.append(ex)

    _save_cache()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(SMOKE_JSON, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dry_run": dry_run,
            "examples": examples,
        }, f, indent=2, sort_keys=True)

    print()
    print("=" * 70)
    print(f"PHASE 2 SMOKE  (n={len(examples)}, dry_run={dry_run})")
    for i, ex in enumerate(examples, 1):
        score = ex.get("query_score")
        score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "n/a"
        print(f"  {i:2d}. {ex['name']!r}")
        print(f"       q: {ex['query']}")
        print(f"       -> {ex['matched_address']!r}  [{ex['result_type']}, score={score_s}]")
        print(f"       old: {ex['old_geometry_source']} {ex['old_centroid']}")
        print(f"       new: {ex['new_centroid']}  patched={ex['patched']}")
    print(f"  Smoke JSON: {SMOKE_JSON}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Phase 3: full pass
# ---------------------------------------------------------------------------

def phase3_run(api_key: str, limit: int, rate_per_sec: float, dry_run: bool) -> None:
    rate_per_sec = max(0.5, min(rate_per_sec, 5.0))  # cap at 5 req/sec
    sleep_s = 1.0 / rate_per_sec

    eligible_list = AUDIT_JSON.with_name("az_here_eligible_uris.txt")
    if not eligible_list.exists():
        print(f"[run] {eligible_list} missing; run --audit first.")
        sys.exit(2)
    with open(eligible_list) as f:
        uris = [l.strip() for l in f if l.strip()]
    if limit:
        uris = uris[:limit]

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ledger_path = RESULTS_DIR / f"az_here_run_{ts}.jsonl"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    _load_cache()
    stats = Counter()
    t0 = time.time()
    print(f"[run] Processing {len(uris)} eligible manifests at {rate_per_sec} req/sec ...")
    print(f"[run] Ledger: {ledger_path}")

    with open(ledger_path, "w") as ledger:
        for i, uri in enumerate(uris):
            manifest = gcs_read_json(uri)
            if manifest is None:
                stats["read_fail"] += 1
                ledger.write(json.dumps({"uri": uri, "result": "read_fail"}) + "\n")
                continue

            # Re-check idempotency (manifest may have been updated since audit).
            geo_source = (manifest.get("geometry") or {}).get("source")
            if geo_source == ALREADY_HERE_SOURCE:
                stats["skip_already_here"] += 1
                continue
            if geo_source == OSM_PLACE_SOURCE:
                stats["skip_has_osm_polygon"] += 1
                continue

            addr = manifest.get("address") or {}
            if not (addr.get("street") or "").strip():
                stats["skip_no_street"] += 1
                continue

            query = _build_query(addr)
            cached = query in _cache
            payload = here_geocode(query, api_key)
            if not cached:
                time.sleep(sleep_s)
            if payload and "_error" in payload:
                stats["here_error"] += 1
                ledger.write(json.dumps({"uri": uri, "result": "here_error", "error": payload["_error"], "query": query}) + "\n")
                continue

            item = pick_best_item(payload or {})
            if item is None:
                stats["no_address_match"] += 1
                ledger.write(json.dumps({"uri": uri, "result": "no_address_match", "query": query}) + "\n")
                continue

            new_geo = build_here_geometry(item, query)
            if new_geo is None:
                stats["build_geo_failed"] += 1
                continue

            ok = patch_manifest_geometry(uri, manifest, new_geo, dry_run=dry_run)
            stats["patched_ok" if ok else "patch_fail"] += 1
            ledger.write(json.dumps({
                "uri": uri,
                "result": "patched_ok" if ok else "patch_fail",
                "result_type": new_geo.get("result_type"),
                "query_score": new_geo.get("query_score"),
                "matched_address": new_geo.get("matched_address"),
                "centroid": new_geo.get("centroid"),
                "dry_run": dry_run,
            }) + "\n")

            if (i + 1) % 100 == 0:
                _save_cache()
                rate = (i + 1) / max(1.0, time.time() - t0)
                print(f"  ... {i+1}/{len(uris)} processed ({rate:.1f} req/s effective). Stats: {dict(stats)}")

    _save_cache()

    print()
    print("=" * 70)
    print(f"PHASE 3 RUN COMPLETE  (dry_run={dry_run})")
    print(f"  Total processed:    {len(uris)}")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"    {k:30s} {v}")
    print(f"  Ledger: {ledger_path}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="HERE Geocoder enrichment for FL bank manifests.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit", action="store_true", help="Phase 1: classify all FL manifests, no API calls.")
    mode.add_argument("--smoke", action="store_true", help="Phase 2: 10-manifest smoke test (real HERE calls).")
    mode.add_argument("--run", action="store_true", help="Phase 3: full pass over eligible manifests.")
    parser.add_argument("--limit", type=int, default=0, help="Phase 3: process at most N manifests (0 = all).")
    parser.add_argument("--rate-per-sec", type=float, default=4.0, help="Phase 3: HERE QPS cap (max 5).")
    parser.add_argument("--dry-run", action="store_true", help="Skip GCS writes; just log what would happen.")
    args = parser.parse_args()

    if args.audit:
        phase1_audit()
        return

    api_key = os.environ.get("HERE_API_KEY", "").strip()
    if not api_key:
        print("ERROR: HERE_API_KEY not in environment. Did you `set -a; source settings.env; set +a`?")
        sys.exit(2)

    if args.smoke:
        phase2_smoke(api_key, dry_run=args.dry_run)
        return

    if args.run:
        phase3_run(api_key, limit=args.limit, rate_per_sec=args.rate_per_sec, dry_run=args.dry_run)
        return


if __name__ == "__main__":
    main()
