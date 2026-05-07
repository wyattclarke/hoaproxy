#!/usr/bin/env python3
"""Upgrade FL bank manifest geometry from ZIP-centroid → OSM place polygon.

Usage:
    source .venv/bin/activate
    set -a; source settings.env 2>/dev/null; set +a
    python scripts/fl_enrich_manifests_with_osm.py [--dry-run] [--limit N] [--county COUNTY_SLUG]

Strategy:
    For each manifest NOT already at geometry.source == "osm-place":
    1. Parse candidate place names from the HOA name.
    2. Query Nominatim with each candidate + county + "Florida, USA".
    3. Accept the first result with class in {place,boundary,landuse} and
       address.state == "Florida" and roughly matching county.
    4. Write geometry back (overwrite prior source) with direct GCS
       read-modify-write — same pattern as fl_enrich_manifests_with_sunbiz.py.

Rate limit: 1 req/sec hard cap (Nominatim ToS).
Cache: data/osm_nominatim_cache.json (keyed by query string).
Audit log: data/fl_osm_enrichment_audit.jsonl
Idempotent: skips manifests whose geometry.source == "osm-place".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_JSON = REPO_ROOT / "data" / "osm_nominatim_cache.json"
AUDIT_JSONL = REPO_ROOT / "data" / "fl_osm_enrichment_audit.jsonl"

BANK_BUCKET = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")
BANK_PREFIX = f"gs://{BANK_BUCKET}/v1/FL"

NOMINATIM_BASE = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "HOAproxy public-records research (+https://hoaproxy.org; contact: hello@hoaproxy.org)"

# ---------------------------------------------------------------------------
# Phase 1: place-name extractor
# ---------------------------------------------------------------------------

# Tail tokens to strip (order matters — longest first to avoid partial strips)
_TAIL_PATTERNS = [
    r"HOMEOWNERS\s+ASSOCIATION",
    r"HOMEOWNER\s+ASSOCIATION",
    r"HOME\s+OWNERS\s+ASSOCIATION",
    r"PROPERTY\s+OWNERS\s+ASSOCIATION",
    r"OWNERS\s+ASSOCIATION",
    r"CONDOMINIUM\s+ASSOCIATION",
    r"CONDO\s+ASSOCIATION",
    r"CONDOMINIUM",
    r"MASTER\s+ASSOCIATION",
    r"COMMUNITY\s+ASSOCIATION",
    r"NEIGHBORHOOD\s+ASSOCIATION",
    r"HOMEOWNERS\s+ASSOC",
    r"ASSOCIATION\s+OF",
    r"ASSOCIATION",
    r"HOA",
    r"POA",
    r"INC\.",
    r"INC",
    r"LLC\.",
    r"LLC",
    r"NUMBER\s+\d+",
    r"NO\.\s*\d+",
    r"#\d+",
]

# Compiled regex: match any tail token (possibly preceded by comma/space) at end of string
_TAIL_RE = re.compile(
    r"[,\s]*\b(?:" + "|".join(_TAIL_PATTERNS) + r")\b[,\.\s#]*$",
    re.IGNORECASE,
)

# Leading determiners to strip
_LEADING_THE_RE = re.compile(r"^\s*THE\s+", re.IGNORECASE)

# Trailing punctuation
_TRAILING_PUNCT_RE = re.compile(r"[,\.\s]+$")

# Junk: starts with a digit (pure number tokens OR alphanumeric codes like "5830943187CC")
_JUNK_NAME_RE = re.compile(r"^\d")
_JUNK_TOKENS = {"and", "access", "associations", "formerly", "of", "the"}

# Patterns that indicate the "name" field is a document title not an HOA name
_DOCUMENT_TITLE_RE = re.compile(
    r"^\s*(amendment|declaration|bylaws|rules|regulations|"
    r"articles\s+of|covenants|conditions|easements|"
    r"restated|recorded|exhibit|section\s+\d|"
    r"chapter\s+\d|page\s+\d|vol\.|volume\s+\d)\b",
    re.IGNORECASE,
)

# Minimum length for a place name to be worth querying
_MIN_QUERY_LEN = 4


def _strip_tail(name: str) -> str:
    """Strip all trailing HOA noise tokens, iterating until stable."""
    prev = None
    s = name.strip()
    while s != prev:
        prev = s
        s = _TAIL_RE.sub("", s)
        s = _TRAILING_PUNCT_RE.sub("", s).strip()
    s = _LEADING_THE_RE.sub("", s).strip()
    s = _TRAILING_PUNCT_RE.sub("", s).strip()
    return s


def _title_case(s: str) -> str:
    """Title-case with common small-word exceptions."""
    small = {"a", "an", "the", "at", "by", "for", "in", "of", "on", "to", "up", "and", "or", "nor"}
    words = s.split()
    result = []
    for i, w in enumerate(words):
        if i == 0 or w.lower() not in small:
            result.append(w.capitalize())
        else:
            result.append(w.lower())
    return " ".join(result)


def _extract_place_name(hoa_name: str) -> list[str]:
    """Produce 1–3 candidate place names from an HOA name, most- to least-specific.

    Examples:
        "TOWNES AT WEST RIVER COMMUNITY ASSOCIATION, INC." → ["Townes at West River", "West River"]
        "OAK PARK HOMEOWNERS ASSOCIATION INC" → ["Oak Park"]
        "VILLAGES OF SUMTER MASTER ASSOCIATION INC" → ["Villages of Sumter", "Sumter"]
    """
    stripped = _strip_tail(hoa_name)
    if not stripped:
        return []

    candidates = []
    seen: set[str] = set()

    def _add(c: str) -> None:
        c = c.strip()
        if not c:
            return
        # Skip if purely digits
        if re.match(r"^\d+$", c):
            return
        # Skip if too short
        if len(c) < _MIN_QUERY_LEN:
            return
        # Skip if too long to be a place name (likely garbled document text)
        if len(c) > 60:
            return
        # Skip if candidate looks like a sentence (contains punctuation mid-string or many words)
        word_count = len(c.split())
        if word_count > 6:
            return
        # Skip if contains sentence-ending punctuation in the middle
        if re.search(r"[.!?;]", c[:-1]):
            return
        # Skip stop-word-only names or generic real-estate terms
        _generic = {"property", "properties", "community", "master", "reserve",
                    "estates", "village", "villas", "manor", "grove", "gardens",
                    "park", "place", "point", "pointe", "cove", "shores", "trace",
                    "preserve", "commons", "landing", "heights", "ridge", "hills",
                    "meadows", "lakes", "creek", "springs", "pines", "oaks",
                    "palms", "island", "club", "court", "run", "glen", "bay",
                    "harbor", "harbour", "bluff", "bend", "crossing", "trails",
                    "woods", "forest", "south", "north", "east", "west", "central"}
        if c.lower() in _JUNK_TOKENS or c.lower() in _generic:
            return
        tc = _title_case(c)
        if tc not in seen:
            seen.add(tc)
            candidates.append(tc)

    # Primary candidate: full stripped name
    _add(stripped)

    # Secondary candidate: try to extract a "at XXXX" or "of XXXX" sub-phrase
    # e.g. "Townes at West River" → "West River"
    # e.g. "Villages of Sumter" → "Sumter"
    words = stripped.split()
    m_at = re.search(r"\bat\s+(.+)$", stripped, re.IGNORECASE)
    m_of = re.search(r"\bof\s+(.+)$", stripped, re.IGNORECASE)
    if m_at:
        sub = m_at.group(1).strip()
        _add(sub)
    elif m_of:
        sub = m_of.group(1).strip()
        _add(sub)

    # Tertiary: last single word if it looks like a proper noun (≥5 chars, alpha only,
    # not a preposition). Skip if the primary is already just one word.
    if len(words) >= 3:
        last_one = words[-1]
        if (
            len(last_one) >= 5
            and last_one.isalpha()
            and last_one.lower() not in {"river", "creek", "lakes", "beach", "dunes",
                                          "landing", "estates", "grove", "village",
                                          "manor", "haven", "ridge", "hills", "woods"}
        ):
            _add(last_one)

    return candidates[:3]  # cap at 3


# ---------------------------------------------------------------------------
# Junk name filter
# ---------------------------------------------------------------------------

def _is_junk_name(name: str) -> bool:
    """Return True if the HOA name is clearly junk (won't match OSM)."""
    if not name:
        return True
    stripped = name.strip()
    first_token = stripped.split()[0] if stripped else ""
    if _JUNK_NAME_RE.match(first_token):
        return True
    if stripped.lower() in _JUNK_TOKENS:
        return True
    # Document titles masquerading as HOA names
    if _DOCUMENT_TITLE_RE.match(stripped):
        return True
    return False


# ---------------------------------------------------------------------------
# GCS helpers (same as sunbiz script)
# ---------------------------------------------------------------------------

def gcs_read_json(uri: str) -> dict | None:
    """Download a GCS JSON object and parse it. Returns None on error."""
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
    """Write a dict as JSON to a GCS URI. Returns True on success."""
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    try:
        result = subprocess.run(
            ["gsutil", "cp", "-", uri],
            input=payload, capture_output=True, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Nominatim cache
# ---------------------------------------------------------------------------

_cache: dict[str, list[dict]] = {}
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
    CACHE_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_JSON.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(_cache, f, indent=2, sort_keys=True)
    tmp.replace(CACHE_JSON)
    _cache_dirty = False


# ---------------------------------------------------------------------------
# Nominatim query
# ---------------------------------------------------------------------------

_last_request_time: float = 0.0


def _nominatim_search(query: str, county: str) -> list[dict]:
    """Query Nominatim with 1 req/sec rate limit. Uses cache."""
    global _last_request_time, _cache_dirty

    # Build the query string as Nominatim will see it
    q = f"{query}, {county} County, Florida, USA"
    cache_key = q

    if cache_key in _cache:
        return _cache[cache_key]

    # Rate limit: ensure ≥1.5s between requests (more conservative than
    # Nominatim's 1 req/sec floor — avoids burst-detection 429s that persist
    # for 15+ min once triggered).
    elapsed = time.time() - _last_request_time
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)

    params = urllib.parse.urlencode({
        "q": q,
        "format": "json",
        "polygon_geojson": "1",
        "addressdetails": "1",
        "limit": "3",
    })
    url = f"{NOMINATIM_BASE}?{params}"

    # Retry with backoff on 429 (Nominatim burst protection: 10s, 20s, 40s)
    data: list[dict] = []
    for attempt in range(3):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = 10 * (2 ** attempt)
                print(f"  [RATE] Nominatim 429 for {q!r} — sleeping {wait}s (attempt {attempt+1}/3)")
                time.sleep(wait)
                _last_request_time = time.time()
                if attempt == 2:
                    print(f"  [WARN] Nominatim 429 exhausted retries for {q!r}")
            else:
                print(f"  [WARN] Nominatim HTTP {exc.code} for {q!r}: {exc}")
                break
        except Exception as exc:
            print(f"  [WARN] Nominatim error for {q!r}: {exc}")
            break

    _last_request_time = time.time()
    _cache[cache_key] = data
    _cache_dirty = True
    return data


# ---------------------------------------------------------------------------
# County name normalization for matching
# ---------------------------------------------------------------------------

def _norm_county(s: str) -> str:
    """Normalize county name for loose comparison."""
    return re.sub(r"[^a-z]", "", (s or "").lower())


# ---------------------------------------------------------------------------
# Result filtering and geometry extraction
# ---------------------------------------------------------------------------

ACCEPTED_CLASSES = {"place", "boundary", "landuse"}


def _filter_results(results: list[dict], manifest_county: str) -> dict | None:
    """Return best matching Nominatim result or None."""
    norm_mc = _norm_county(manifest_county)
    for r in results:
        cls = r.get("class", "")
        if cls not in ACCEPTED_CLASSES:
            continue
        addr = r.get("address") or {}
        # Must be Florida
        state = addr.get("state", "")
        if state.lower() not in {"florida", "fl"}:
            continue
        # County match (loose): accept if manifest county matches result county,
        # OR if result has no county field (e.g. it's a city-level entry).
        result_county = addr.get("county", "")
        if result_county:
            norm_rc = _norm_county(result_county.replace(" County", ""))
            if norm_mc and norm_rc and norm_mc not in norm_rc and norm_rc not in norm_mc:
                continue
        return r
    return None


def _build_geometry(result: dict, candidate: str) -> dict:
    """Convert a Nominatim result into our geometry dict shape."""
    geojson = result.get("geojson") or {}
    geojson_type = geojson.get("type", "").lower()
    has_polygon = geojson_type in {"polygon", "multipolygon"}

    lat = float(result.get("lat", 0))
    lon = float(result.get("lon", 0))

    # If polygon available, compute centroid from it; else use lat/lon
    centroid = {"lat": lat, "lon": lon}

    bb = result.get("boundingbox")  # ["minlat", "maxlat", "minlon", "maxlon"]
    bounding_box = None
    if bb and len(bb) == 4:
        try:
            bounding_box = [float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])]
        except (ValueError, TypeError):
            pass

    geo: dict = {
        "centroid": centroid,
        "source": "osm-place",
        "osm_type": result.get("osm_type"),
        "osm_id": int(result.get("osm_id", 0)) if result.get("osm_id") else None,
        "place_class": result.get("class"),
        "place_type": result.get("type"),
        "matched_query": candidate,
        "confidence": "place-polygon" if has_polygon else "place-centroid",
    }

    if has_polygon:
        geo["polygon"] = geojson

    if bounding_box:
        geo["bounding_box"] = bounding_box

    return geo


# ---------------------------------------------------------------------------
# Manifest patching
# ---------------------------------------------------------------------------

def patch_manifest_geometry(
    uri: str,
    manifest: dict,
    new_geometry: dict,
    candidate: str,
    osm_display_name: str,
    dry_run: bool,
) -> bool:
    """Overwrite geometry with OSM data; preserve address and all other fields."""
    updated = dict(manifest)

    # Replace geometry entirely (OSM wins over prior zip-centroid)
    updated["geometry"] = new_geometry

    # Append provenance entry
    sources = list(manifest.get("metadata_sources") or [])
    sources.append({
        "source": "osm-place-enrichment",
        "candidate_query": candidate,
        "osm_display_name": osm_display_name,
        "fields_provided": ["centroid", "polygon", "bounding_box", "osm_type", "osm_id"],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    updated["metadata_sources"] = sources

    if dry_run:
        return True
    return gcs_write_json(uri, updated)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich FL bank manifests with OSM place geometry.")
    parser.add_argument("--dry-run", action="store_true", help="Read + query but do not write to GCS.")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N manifests (0 = all).")
    parser.add_argument("--county", type=str, default="", help="Process only this county slug.")
    args = parser.parse_args()

    # Load cache
    print("[init] Loading Nominatim cache …")
    _load_cache()
    initial_cache_size = len(_cache)
    print(f"[init] Cache has {initial_cache_size} entries.")

    # List FL manifests
    if args.county:
        list_glob = f"{BANK_PREFIX}/{args.county}/**/manifest.json"
    else:
        list_glob = f"{BANK_PREFIX}/**/manifest.json"

    print(f"[list] Listing FL manifests in bank (may take 30–60s) …")
    t0 = time.time()
    result = subprocess.run(
        ["gsutil", "ls", list_glob],
        capture_output=True, text=True, timeout=180,
    )
    manifest_uris = [
        l.strip() for l in result.stdout.strip().split("\n")
        if l.strip().endswith("manifest.json")
    ]
    print(f"[list] Found {len(manifest_uris)} FL manifests in {time.time()-t0:.1f}s.")

    if args.limit:
        manifest_uris = manifest_uris[: args.limit]
        print(f"[list] Limited to {len(manifest_uris)} manifests.")

    # Stats
    stats: dict[str, int] = {
        "processed": 0,
        "skipped_already_osm": 0,
        "skipped_unknown_county": 0,
        "skipped_junk_name": 0,
        "skipped_no_candidates": 0,
        "read_fail": 0,
        "nominatim_hit_cache": 0,
        "nominatim_miss_cache": 0,
        "match_polygon": 0,
        "match_centroid": 0,
        "no_match": 0,
        "write_ok": 0,
        "write_fail": 0,
    }

    audit_entries: list[dict] = []
    example_matches: list[dict] = []
    example_no_matches: list[dict] = []

    start_time = time.time()
    request_count = 0

    for i, uri in enumerate(manifest_uris):
        # Parse path: gs://bucket/v1/FL/{county_slug}/{hoa_slug}/manifest.json
        rel = uri.replace(f"gs://{BANK_BUCKET}/v1/FL/", "")
        if rel.endswith("/manifest.json"):
            rel = rel[: -len("/manifest.json")]
        parts = rel.split("/")
        if len(parts) < 2:
            print(f"  [WARN] Cannot parse path: {uri}")
            continue
        county_slug = parts[0]
        hoa_slug = parts[1]

        if (i + 1) % 250 == 0:
            elapsed = time.time() - start_time
            cache_hits = stats["nominatim_hit_cache"]
            cache_misses = stats["nominatim_miss_cache"]
            print(
                f"  … {i+1}/{len(manifest_uris)} processed | "
                f"osm_polygon={stats['match_polygon']} osm_centroid={stats['match_centroid']} "
                f"no_match={stats['no_match']} | "
                f"cache hits={cache_hits} misses={cache_misses} | "
                f"elapsed={elapsed:.0f}s"
            )
            # Flush cache periodically
            _save_cache()

        # Read manifest
        manifest = gcs_read_json(uri)
        if manifest is None:
            stats["read_fail"] += 1
            continue

        stats["processed"] += 1
        name = manifest.get("name") or ""
        addr = manifest.get("address") or {}
        existing_geo = manifest.get("geometry") or {}

        # Skip if already upgraded to OSM
        if existing_geo.get("source") == "osm-place":
            stats["skipped_already_osm"] += 1
            continue

        # Skip unknown county
        manifest_county = addr.get("county") or county_slug.replace("-", " ").title()
        if county_slug == "_unknown-county" or (addr.get("county") or "").lower() == "_unknown-county":
            stats["skipped_unknown_county"] += 1
            continue

        # Skip junk names
        if _is_junk_name(name):
            stats["skipped_junk_name"] += 1
            continue

        # Get candidates
        candidates = _extract_place_name(name)
        if not candidates:
            stats["skipped_no_candidates"] += 1
            audit_entries.append({
                "uri": uri,
                "county_slug": county_slug,
                "name": name,
                "result": "NO_CANDIDATES",
            })
            if len(example_no_matches) < 5:
                example_no_matches.append({
                    "name": name,
                    "county_slug": county_slug,
                    "candidates": [],
                    "reason": "no_candidates",
                })
            continue

        # Query Nominatim with each candidate until we get a match
        matched_result = None
        matched_candidate = None
        queries_tried: list[str] = []

        for candidate in candidates:
            cache_key = f"{candidate}, {manifest_county} County, Florida, USA"
            was_cached = cache_key in _cache
            results = _nominatim_search(candidate, manifest_county)
            if was_cached:
                stats["nominatim_hit_cache"] += 1
            else:
                stats["nominatim_miss_cache"] += 1
                request_count += 1
            queries_tried.append(candidate)

            filtered = _filter_results(results, manifest_county)
            if filtered:
                matched_result = filtered
                matched_candidate = candidate
                break

        if matched_result is None:
            stats["no_match"] += 1
            audit_entries.append({
                "uri": uri,
                "county_slug": county_slug,
                "name": name,
                "candidates": queries_tried,
                "result": "NO_MATCH",
            })
            if len(example_no_matches) < 5:
                example_no_matches.append({
                    "name": name,
                    "county_slug": county_slug,
                    "candidates": queries_tried,
                    "reason": "no_nominatim_match",
                })
            continue

        # Build geometry
        new_geo = _build_geometry(matched_result, matched_candidate)
        has_polygon = "polygon" in new_geo

        if has_polygon:
            stats["match_polygon"] += 1
        else:
            stats["match_centroid"] += 1

        display_name = matched_result.get("display_name", "")
        osm_class = matched_result.get("class", "")
        osm_type = matched_result.get("type", "")

        # Write geometry back
        ok = patch_manifest_geometry(
            uri, manifest, new_geo, matched_candidate, display_name, args.dry_run
        )
        if ok:
            stats["write_ok"] += 1
        else:
            stats["write_fail"] += 1

        audit_entries.append({
            "uri": uri,
            "county_slug": county_slug,
            "name": name,
            "matched_candidate": matched_candidate,
            "osm_display_name": display_name,
            "osm_class": osm_class,
            "osm_type": osm_type,
            "has_polygon": has_polygon,
            "result": "MATCH",
            "write_ok": ok,
            "dry_run": args.dry_run,
        })

        if len(example_matches) < 5:
            example_matches.append({
                "name": name,
                "county_slug": county_slug,
                "matched_candidate": matched_candidate,
                "osm_display_name": display_name,
                "osm_class": osm_class,
                "osm_type": osm_type,
                "has_polygon": has_polygon,
            })

        if not args.dry_run and ok:
            print(
                f"  MATCH {county_slug}/{hoa_slug} "
                f"→ {display_name!r} [{osm_class}/{osm_type}] "
                f"{'polygon' if has_polygon else 'centroid'}"
            )

    # Final cache flush
    _save_cache()

    # Write audit log
    with open(AUDIT_JSONL, "w") as f:
        for entry in audit_entries:
            f.write(json.dumps(entry) + "\n")

    wall_time = time.time() - start_time
    final_cache_size = len(_cache)

    print()
    print("=" * 70)
    print("OSM GEOMETRY ENRICHMENT COMPLETE")
    print(f"  Total manifests listed:         {len(manifest_uris)}")
    print(f"  Processed (read OK):            {stats['processed']}")
    print(f"  Skipped (already osm-place):    {stats['skipped_already_osm']}")
    print(f"  Skipped (unknown county):       {stats['skipped_unknown_county']}")
    print(f"  Skipped (junk name):            {stats['skipped_junk_name']}")
    print(f"  Skipped (no candidates):        {stats['skipped_no_candidates']}")
    print(f"  --- Nominatim ---")
    print(f"  Cache entries (start):          {initial_cache_size}")
    print(f"  Cache entries (end):            {final_cache_size}")
    print(f"  Cache hits:                     {stats['nominatim_hit_cache']}")
    print(f"  Cache misses (live req):        {stats['nominatim_miss_cache']}")
    print(f"  Live requests made:             {request_count}")
    print(f"  --- Match results ---")
    print(f"  Match (polygon):                {stats['match_polygon']}")
    print(f"  Match (centroid only):          {stats['match_centroid']}")
    print(f"  No match:                       {stats['no_match']}")
    print(f"  --- Write results ---")
    print(f"  Write OK:                       {stats['write_ok']}")
    print(f"  Write fail:                     {stats['write_fail']}")
    print(f"  Read fail:                      {stats['read_fail']}")
    print(f"  --- Timing ---")
    print(f"  Wall time:                      {wall_time:.1f}s ({wall_time/60:.1f}min)")
    print(f"  Total requests:                 {request_count}")
    if args.dry_run:
        print("  [DRY RUN — no GCS writes performed]")
    print(f"  Audit log:                      {AUDIT_JSONL}")
    print("=" * 70)

    print("\n5 example MATCHED:")
    for e in example_matches:
        print(f"  {e['county_slug']}/{e['name']!r}")
        print(f"    candidate: {e['matched_candidate']!r}")
        print(f"    osm: {e['osm_display_name']!r} [{e['osm_class']}/{e['osm_type']}]")
        print(f"    has_polygon: {e['has_polygon']}")

    print("\n5 example NO_MATCH:")
    for e in example_no_matches:
        print(f"  {e['county_slug']}/{e['name']!r}")
        print(f"    candidates tried: {e['candidates']}")
        print(f"    reason: {e['reason']}")


if __name__ == "__main__":
    main()
