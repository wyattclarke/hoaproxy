#!/usr/bin/env python3
"""Repair the 2026-05-09 audit corruption.

Two passes, both default to dry-run. Pass --apply to write.

Pass A — fix the upsert-collision damage (~30K–40K rows touched).
  /admin/create-stub-hoas COALESCE-overwrote state, city, postal_code,
  location_quality, source on every name collision while preserving
  latitude / longitude / boundary_geojson / street. The repair:
    1. For rows with one of the 11 audit-backfill source strings AND
       latitude IS NOT NULL: bbox-classify lat/lon → actual_state. If
       actual_state differs from the (overwritten) state, restore it.
    2. For all rows with that source filter, restore location_quality via
       a promote-only cascade — boundary→polygon, street→address,
       postal_code+lat→zip_centroid. Never demote.

Pass B — re-geocode the 1,147 deleted-then-stubbed HOAs.
  /admin/delete-hoa cascaded hoa_locations; the restore pass re-created
  them from grade JSONs that captured only name/city/state. lat/lon/
  boundary/street are gone. The bank still has the original address. The
  repair: read each stub's bank manifest, geocode its postal_code via
  HERE, write back lat/lon + quality=zip_centroid.

Run:
  python scripts/audit/repair_audit_corruption.py --pass A --dry-run
  python scripts/audit/repair_audit_corruption.py --pass A --apply
  python scripts/audit/repair_audit_corruption.py --pass B --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env")

DEFAULT_BASE_URL = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")
BANK_BUCKET = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")

# 11 source strings that flag rows touched by the 2026-05-09 backfill.
PASS_A_SOURCES = [
    "tx-trec-hoa-management-certificate",
    "fl-sunbiz",
    "sos-ca-bizfile",
    "co-dora-hoa-information-office",
    "ny-dos-active-corporations",
    "or-sos-active-nonprofit-corporations",
    "sos-ct",
    "hi-dcca-aouo-contact-list",
    "il-cook-assessor-chicagoland",
    "sos-ri",
    "az-tucson-hoa-gis",
]

PASS_B_SOURCE = "audit_2026_05_09_restored_stub"

# US state bboxes with a small (~0.15°) padding to absorb GPS jitter / rounding.
# Format: (min_lat, max_lat, min_lon, max_lon).  AK's bbox excludes the
# dateline-crossing Aleutians; PR/DC included.  HI is the main-island bbox.
STATE_BBOXES: dict[str, tuple[float, float, float, float]] = {
    "AL": (30.13, 35.02, -88.49, -84.88),
    "AK": (54.00, 71.50, -168.00, -130.00),
    "AZ": (31.20, 37.10, -114.90, -108.90),
    "AR": (32.95, 36.55, -94.70, -89.55),
    "CA": (32.40, 42.10, -124.60, -114.00),
    "CO": (36.85, 41.15, -109.20, -101.85),
    "CT": (40.90, 42.20, -73.90, -71.60),
    "DE": (38.40, 39.95, -75.85, -75.00),
    "DC": (38.78, 39.01, -77.13, -76.90),
    "FL": (24.30, 31.10, -87.70, -79.80),
    "GA": (30.30, 35.10, -85.70, -80.75),
    "HI": (18.85, 22.40, -160.40, -154.60),
    "ID": (41.95, 49.05, -117.30, -111.00),
    "IL": (36.85, 42.60, -91.70, -86.90),
    "IN": (37.70, 41.80, -88.20, -84.70),
    "IA": (40.30, 43.60, -96.70, -90.10),
    "KS": (36.90, 40.10, -102.10, -94.50),
    "KY": (36.45, 39.20, -89.65, -81.90),
    "LA": (28.85, 33.10, -94.10, -88.75),
    "ME": (42.95, 47.55, -71.20, -66.85),
    "MD": (37.85, 39.80, -79.55, -75.00),
    "MA": (41.15, 42.95, -73.55, -69.85),
    "MI": (41.65, 48.35, -90.50, -82.30),
    "MN": (43.40, 49.45, -97.30, -89.40),
    "MS": (30.10, 35.05, -91.70, -88.05),
    "MO": (35.90, 40.70, -95.85, -89.05),
    "MT": (44.30, 49.10, -116.10, -103.95),
    "NE": (39.95, 43.05, -104.10, -95.25),
    "NV": (34.95, 42.05, -120.10, -113.95),
    "NH": (42.65, 45.40, -72.65, -70.55),
    "NJ": (38.85, 41.40, -75.65, -73.85),
    "NM": (31.25, 37.05, -109.10, -102.95),
    "NY": (40.40, 45.10, -79.95, -71.70),
    "NC": (33.75, 36.65, -84.40, -75.40),
    "ND": (45.85, 49.10, -104.15, -96.50),
    "OH": (38.35, 42.40, -84.90, -80.40),
    "OK": (33.55, 37.10, -103.10, -94.35),
    "OR": (41.85, 46.40, -124.80, -116.30),
    "PA": (39.65, 42.35, -80.60, -74.60),
    "RI": (41.05, 42.10, -72.00, -71.00),
    "SC": (32.00, 35.30, -83.45, -78.45),
    "SD": (42.40, 46.05, -104.15, -96.40),
    "TN": (34.90, 36.80, -90.40, -81.55),
    "TX": (25.50, 36.60, -106.80, -93.40),
    "UT": (36.95, 42.05, -114.10, -109.00),
    "VT": (42.65, 45.10, -73.55, -71.40),
    "VA": (36.40, 39.60, -83.80, -75.10),
    "WA": (45.45, 49.10, -124.90, -116.85),
    "WV": (37.10, 40.70, -82.75, -77.65),
    "WI": (42.40, 47.20, -93.05, -86.75),
    "WY": (40.95, 45.10, -111.15, -104.00),
    "PR": (17.85, 18.55, -67.95, -65.20),
}

QUALITY_RANK = {
    None: -1, "": -1, "unknown": 0,
    "city_only": 1, "place_centroid": 2, "zip_centroid": 3,
    "address": 4, "polygon": 5,
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def live_admin_token() -> str | None:
    # Explicit override wins; otherwise pull from settings.env (loaded by
    # the caller via load_dotenv). The Render env-vars fallback that lived
    # here was removed 2026-05-16 after the Hetzner cutover.
    return os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")


# ---------------------------------------------------------------------------
# Bbox classify
# ---------------------------------------------------------------------------

_HERE_REVGEO_CACHE: dict[tuple[float, float], str | None] = {}


def here_reverse_geocode_state(lat: float, lon: float) -> str | None:
    """Return the 2-letter US state code for (lat, lon) via HERE, or None."""
    key = (round(float(lat), 3), round(float(lon), 3))
    if key in _HERE_REVGEO_CACHE:
        return _HERE_REVGEO_CACHE[key]
    api_key = os.environ.get("HERE_API_KEY")
    if not api_key:
        _HERE_REVGEO_CACHE[key] = None
        return None
    try:
        r = requests.get(
            "https://revgeocode.search.hereapi.com/v1/revgeocode",
            params={"at": f"{lat},{lon}", "apiKey": api_key, "limit": 1},
            timeout=20,
        )
        if r.status_code != 200:
            _HERE_REVGEO_CACHE[key] = None
            return None
        items = r.json().get("items") or []
        if not items:
            _HERE_REVGEO_CACHE[key] = None
            return None
        addr = items[0].get("address") or {}
        if addr.get("countryCode") not in ("USA", "USA-PR"):
            _HERE_REVGEO_CACHE[key] = None
            return None
        sc = addr.get("stateCode")
        _HERE_REVGEO_CACHE[key] = sc.upper() if sc else None
        return _HERE_REVGEO_CACHE[key]
    except Exception:
        _HERE_REVGEO_CACHE[key] = None
        return None


def classify_state(lat: float, lon: float, cur_state: str | None = None,
                   *, allow_here_fallback: bool = True) -> str | None:
    """Return the most likely state for (lat, lon).

    Priority:
    1. If the claimed state's bbox contains the point, keep it (the row's
       current state is geographically plausible, no correction warranted).
    2. Else if exactly one bbox matches, use that.
    3. Else (zero or multiple matches), reverse-geocode via HERE for an
       authoritative answer when ``allow_here_fallback`` is true.
    4. Else None (ambiguous, leave row alone).
    """
    if lat is None or lon is None:
        return None
    matches = [
        st for st, (mnlat, mxlat, mnlon, mxlon) in STATE_BBOXES.items()
        if mnlat <= lat <= mxlat and mnlon <= lon <= mxlon
    ]
    cs = (cur_state or "").upper()
    if cs and cs in matches:
        return cs
    if len(matches) == 1:
        return matches[0]
    if allow_here_fallback:
        return here_reverse_geocode_state(float(lat), float(lon))
    return None


def derive_quality(*, has_boundary: bool, street: str | None,
                   postal_code: str | None, lat, city: str | None) -> str | None:
    if has_boundary:
        return "polygon"
    if street and street.strip():
        return "address"
    if postal_code and postal_code.strip() and lat is not None:
        return "zip_centroid"
    if city and city.strip():
        return "city_only"
    return None


# ---------------------------------------------------------------------------
# Pass A — state + quality fix on upsert collisions
# ---------------------------------------------------------------------------

def fetch_rows(token: str, base_url: str, sources: list[str]) -> list[dict]:
    r = requests.post(
        f"{base_url}/admin/list-corruption-targets",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"sources": sources, "require_lat": False},
        timeout=300,
    )
    r.raise_for_status()
    return r.json()["rows"]


def plan_pass_a(rows: list[dict]) -> tuple[list[dict], dict]:
    """Compute (write_records, summary) for Pass A. Each write_record is a
    dict ready for /admin/backfill-locations."""
    plan: list[dict] = []
    state_fixes_by_state: dict[str, int] = {}
    state_ambiguous = 0
    state_in_bbox = 0
    quality_promotions: dict[str, int] = {}
    quality_no_change = 0

    for row in rows:
        new_state = None
        new_quality = None
        lat = row.get("latitude")
        lon = row.get("longitude")
        cur_state = row.get("state")
        cur_quality = row.get("location_quality")
        derived = derive_quality(
            has_boundary=bool(row.get("has_boundary")),
            street=row.get("street"),
            postal_code=row.get("postal_code"),
            lat=lat,
            city=row.get("city"),
        )

        # State fix — bbox first, HERE reverse-geocode as a fallback. Keep
        # cur_state when it's geographically plausible; only "fix" when we're
        # confident.
        if lat is not None and lon is not None:
            actual = classify_state(float(lat), float(lon), cur_state=cur_state)
            if actual is None:
                state_ambiguous += 1
            elif actual != (cur_state or "").upper():
                new_state = actual
                key = f"{cur_state}->{actual}"
                state_fixes_by_state[key] = state_fixes_by_state.get(key, 0) + 1
            else:
                state_in_bbox += 1

        # Quality promote-only
        if derived and QUALITY_RANK.get(derived, 0) > QUALITY_RANK.get(cur_quality, -1):
            new_quality = derived
            key = f"{cur_quality}->{derived}"
            quality_promotions[key] = quality_promotions.get(key, 0) + 1
        else:
            quality_no_change += 1

        if new_state is None and new_quality is None:
            continue

        rec = {"hoa": row["hoa"]}
        if new_state is not None:
            rec["state"] = new_state
        if new_quality is not None:
            rec["location_quality"] = new_quality
        plan.append(rec)

    summary = {
        "total_rows_examined": len(rows),
        "rows_to_update": len(plan),
        "state_in_bbox_no_change": state_in_bbox,
        "state_lat_ambiguous": state_ambiguous,
        "state_fixes_by_transition": state_fixes_by_state,
        "quality_promotions_by_transition": quality_promotions,
        "quality_no_change": quality_no_change,
    }
    return plan, summary


# ---------------------------------------------------------------------------
# Pass B — re-geocode 1,147 stubs from bank manifests + HERE
# ---------------------------------------------------------------------------

def slugify_for_bank(name: str) -> str:
    from hoaware.bank import slugify
    return slugify(name)


def build_state_slug_index(state: str, gcs_client) -> dict[str, str]:
    """List all manifest.json under v1/{STATE}/ and return slug → blob_path."""
    bucket = gcs_client.bucket(BANK_BUCKET)
    index: dict[str, str] = {}
    prefix = f"v1/{state}/"
    for blob in gcs_client.list_blobs(bucket, prefix=prefix):
        if not blob.name.endswith("/manifest.json"):
            continue
        # blob.name = "v1/HI/honolulu/liholiho/manifest.json"
        parts = blob.name.split("/")
        if len(parts) >= 5:
            slug = parts[-2]
            # Prefer the path closer to the state's known counties; the unverified
            # path "v1/STATE/_unverified/.../slug" has more components — skip if a
            # canonical-county entry already exists for this slug.
            if slug in index and "/_unverified/" in blob.name:
                continue
            index[slug] = blob.name
    return index


def read_manifest(gcs_client, blob_path: str) -> dict | None:
    try:
        bucket = gcs_client.bucket(BANK_BUCKET)
        return json.loads(bucket.blob(blob_path).download_as_bytes())
    except Exception:
        return None


_HERE_CACHE: dict[str, tuple[float, float] | None] = {}


def here_geocode_postal(postal_code: str, country: str = "USA") -> tuple[float, float] | None:
    key = f"{country}:{postal_code}"
    if key in _HERE_CACHE:
        return _HERE_CACHE[key]
    api_key = os.environ.get("HERE_API_KEY")
    if not api_key:
        _HERE_CACHE[key] = None
        return None
    try:
        r = requests.get(
            "https://geocode.search.hereapi.com/v1/geocode",
            params={"q": f"{postal_code} {country}", "apiKey": api_key, "limit": 1},
            timeout=20,
        )
        if r.status_code != 200:
            _HERE_CACHE[key] = None
            return None
        items = r.json().get("items") or []
        if not items:
            _HERE_CACHE[key] = None
            return None
        pos = items[0].get("position") or {}
        lat = pos.get("lat"); lon = pos.get("lng")
        if lat is None or lon is None:
            _HERE_CACHE[key] = None
            return None
        _HERE_CACHE[key] = (float(lat), float(lon))
        return _HERE_CACHE[key]
    except Exception:
        _HERE_CACHE[key] = None
        return None


def plan_pass_b(rows: list[dict]) -> tuple[list[dict], dict]:
    """Compute write records for Pass B by reading bank manifests + geocoding."""
    try:
        from google.cloud import storage as gcs
    except ImportError:
        print("FATAL: google-cloud-storage not installed", file=sys.stderr); sys.exit(2)

    client = gcs.Client()
    plan: list[dict] = []
    by_state_count: dict[str, int] = {}
    state_indexes: dict[str, dict[str, str]] = {}

    no_manifest = 0
    no_postal = 0
    geocode_fail = 0
    geocoded = 0

    by_state_rows: dict[str, list[dict]] = {}
    for r in rows:
        st = (r.get("state") or "").upper()
        by_state_rows.setdefault(st, []).append(r)

    for state, srows in by_state_rows.items():
        if not state:
            continue
        print(f"  [B] indexing bank for {state} ({len(srows)} stubs)...")
        state_indexes[state] = build_state_slug_index(state, client)
        idx = state_indexes[state]
        for row in srows:
            slug = slugify_for_bank(row["hoa"])
            blob_path = idx.get(slug)
            if not blob_path:
                no_manifest += 1
                continue
            manifest = read_manifest(client, blob_path)
            if not manifest:
                no_manifest += 1
                continue
            addr = manifest.get("address") or {}
            postal = addr.get("postal_code")
            if not postal:
                no_postal += 1
                continue
            geo = here_geocode_postal(str(postal).strip())
            if not geo:
                geocode_fail += 1
                continue
            lat, lon = geo
            geocoded += 1
            rec = {
                "hoa": row["hoa"],
                "latitude": lat,
                "longitude": lon,
                "postal_code": str(postal).strip(),
                "location_quality": "zip_centroid",
                "source": PASS_B_SOURCE,
            }
            if addr.get("city"):
                rec["city"] = addr["city"]
            plan.append(rec)
            by_state_count[state] = by_state_count.get(state, 0) + 1

    summary = {
        "rows_examined": len(rows),
        "rows_to_update": len(plan),
        "no_manifest_in_bank": no_manifest,
        "manifest_no_postal_code": no_postal,
        "geocode_failed": geocode_fail,
        "geocoded_ok": geocoded,
        "by_state": by_state_count,
        "unique_postal_codes_geocoded": len([v for v in _HERE_CACHE.values() if v]),
    }
    return plan, summary


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_plan(token: str, base_url: str, plan: list[dict], batch: int = 200) -> dict:
    matched = 0
    not_found = 0
    bad_quality = 0
    failures: list[dict] = []
    for i in range(0, len(plan), batch):
        chunk = plan[i:i + batch]
        try:
            r = requests.post(
                f"{base_url}/admin/backfill-locations",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"records": chunk}, timeout=300,
            )
            if r.status_code == 200:
                body = r.json()
                matched += int(body.get("matched", 0))
                not_found += int(body.get("not_found", 0))
                bad_quality += int(body.get("bad_quality", 0))
                print(f"  batch {i // batch + 1}/{(len(plan) + batch - 1) // batch}: "
                      f"matched={body.get('matched')} not_found={body.get('not_found')}")
            else:
                failures.append({"batch_start": i, "http": r.status_code, "body": r.text[:300]})
        except Exception as e:
            failures.append({"batch_start": i, "error": f"{type(e).__name__}: {e}"})
        time.sleep(1.0)
    return {
        "matched": matched,
        "not_found": not_found,
        "bad_quality": bad_quality,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass", dest="phase", required=True, choices=["A", "B", "both"])
    ap.add_argument("--apply", action="store_true", help="Apply the plan (default: dry-run)")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--report", default=None,
                    help="Write the plan + summary as JSON to this path")
    ap.add_argument("--limit", type=int, default=0,
                    help="Cap the row count (debugging)")
    args = ap.parse_args()

    token = live_admin_token()
    if not token:
        print("FATAL: no admin token", file=sys.stderr); return 2

    overall: dict = {"dry_run": not args.apply}

    phases = ["A", "B"] if args.phase == "both" else [args.phase]
    for phase in phases:
        print(f"\n=== Pass {phase} ===")
        if phase == "A":
            rows = fetch_rows(token, args.base_url, PASS_A_SOURCES)
        else:
            rows = fetch_rows(token, args.base_url, [PASS_B_SOURCE])
        if args.limit:
            rows = rows[:args.limit]
        print(f"  fetched {len(rows)} rows")

        if phase == "A":
            plan, summary = plan_pass_a(rows)
        else:
            plan, summary = plan_pass_b(rows)

        print(f"  plan: {len(plan)} updates")
        print(json.dumps(summary, indent=2, sort_keys=True))

        if args.apply and plan:
            print(f"  applying {len(plan)} updates...")
            apply_summary = apply_plan(token, args.base_url, plan)
            print(json.dumps(apply_summary, indent=2, sort_keys=True))
            overall[f"pass_{phase}_applied"] = apply_summary

        overall[f"pass_{phase}"] = {"plan": plan, "summary": summary}

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(overall, indent=2, sort_keys=True))
        print(f"\nreport written to {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
