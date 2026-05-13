#!/usr/bin/env python3
"""Repair out-of-bbox (OOB) HOA locations via OCR + HERE Geocoder, demote on failure.

For each state, find HOAs whose ``hoa_locations.latitude/longitude`` lands
outside the state's bbox (padded 0.05°). Try to repair via OCR-extracted
property address + HERE Geocoder; demote to ``city_only`` only if HERE
can't place the entity inside the bbox.

See ``docs/oob-location-repair-runbook.md`` for the full design.

Modes
-----
``--mode tally`` (default; read-only)
    Query ``/admin/list-corruption-targets`` once per source-batch, classify
    each row OOB / in-bbox / polygon-oob, and write
    ``state_scrapers/_orchestrator/oob_repair_{date}/tally.json``. Prints
    projected HERE-call budget.

``--mode repair`` (writes to live DB)
    Requires ``tally.json`` to exist and a non-default ``--max-here-calls``
    to be passed. Processes states in descending order of OOB count.
    Stops on first HTTP 429 from HERE.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Reuse HERE / extraction utilities from the existing enrich script.
ENRICH_PATH = ROOT / "scripts" / "enrich_locations_from_ocr_here.py"
_spec = importlib.util.spec_from_file_location("_enrich", ENRICH_PATH)
_enrich = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_enrich)  # type: ignore[union-attr]

STATE_FULL: dict[str, str] = _enrich.STATE_FULL
HERE_ENDPOINT: str = _enrich.HERE_ENDPOINT
STREET_TYPES: str = _enrich.STREET_TYPES
STREET_ADDR_RE = _enrich.STREET_ADDR_RE
# NB: enrich's CITY_STATE_ZIP_RE has a pre-existing bug — its pattern was
# written with f-string-style ``{{1,40}}`` literals so it never matched in
# production. We redefine it here with the correct ``{1,40}`` repetition.
CITY_STATE_ZIP_RE = re.compile(
    r"\b([A-Z][\w\s.&'-]{1,40}?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b"
)
ZIP_RE = _enrich.ZIP_RE
live_admin_token = _enrich.live_admin_token

DEFAULT_BASE_URL = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")
BBOX_PATH = ROOT / "state_scrapers" / "_meta" / "state_bboxes.json"
BBOX_PAD_DEG = 0.05  # ~3.5 mi N–S, ~4 km E–W at 40° N

# Comprehensive sources observed across REGISTRIES + per-state seed JSONLs.
# Sources NOT in this list (very rare manual stubs / one-off imports) will
# be missed by this pass. Update if a new source appears.
KNOWN_SOURCES: tuple[str, ...] = (
    "agent_city",
    "arcgis_tucson",
    "audit_2026_05_09_restored_stub",
    "az-tucson-hoa-gis",
    "ca_sos_bulk_corp",
    "census-zcta-2023",
    "census_geocoder_cleanup",
    "census_zcta_2024_zip_centroid",
    "census_zcta_doc_zip",
    "census_zcta_ocr_zip",
    "city_centroid_fallback",
    "co-dora-hoa-information-office",
    "co_dora_hoa_registry",
    "co_dora_registry",
    "cook-assessor-3723-97qp-mail-name",
    "corp",
    "county_gis_fuzzy",
    "county_gis_nc_cumberland_subs",
    "county_gis_nc_mecklenburg_subs",
    "county_gis_nc_wake_subs",
    "dc-gis-cama-condo-regime",
    "dc-mar-via-osm-nominatim",
    "doc_inferred_zip",
    "doc_inferred_zip_loose",
    "dupage-arcgis-parcelswithrealestatecc-billname",
    "fl-sunbiz",
    "ga-slug-cleanup",
    "ga-slug-cleanup-merge",
    "gcs_prepared_ingest",
    "here-geocoder-enrichment",
    "hi-dcca-aouo-contact-list",
    "il-cook-assessor-chicagoland",
    "il-existing-bank-manifest",
    "il-mgmt-co-acm",
    "il-mgmt-co-associa-chicagoland",
    "il-mgmt-co-draperkramer",
    "il-mgmt-co-fsresidential",
    "il-mgmt-co-habitat",
    "il-mgmt-co-lieberman",
    "il-mgmt-co-sudler",
    "il-mgmt-co-vanguard",
    "il-mgmt-co-wolinlevin",
    "il-owned-domain-direct-pdf",
    "inferred",
    "live-name-reconciliation",
    "local_sync",
    "loose_nominatim",
    "ma-massgis-l3-parcels-owner-name",
    "manual",
    "manual_city_fix",
    "md-baltimore-county-subdivisions",
    "md-county-gis",
    "md-harford-county-subdivisions",
    "me-curated-serper-direct-pdf",
    "me-curated-serper-direct-pdf-2",
    "me_zip_centroid_backfill",
    "mi-county-gis",
    "mi-kalamazoo-county-plats",
    "mi-kent-county-plat-map",
    "miami-dade-recorder",
    "mn-statewide-subdivisions-liveby",
    "mo-springfield-subdivisions",
    "nc-county-gis",
    "nc-new-hanover-county-subdivisions",
    "nc-wake-county-subdivisions",
    "nominatim",
    "nominatim_cleanup",
    "nominatim_ocr_clues",
    "ny-acris-decl-2026-05",
    "ny-dos-active-corporations",
    "ny_dos_active_corporations",
    "ocr",
    "ocr_llm_validation",
    "oh-county-gis",
    "oh-delaware-county-condos",
    "oh-delaware-county-subdivisions",
    "oh-hamilton-county-condo-common-parcels",
    "oh-hamilton-county-condo-polygons",
    "oh-stark-county-auditor-plat-index",
    "or-sos-active-nonprofit-corporations",
    "or_sos_active_nonprofit_corporations",
    "osm-place",
    "osm-place-enrichment",
    "pm_address_zip",
    "pm_website_scrape",
    "real-estate-listing-via-serper",
    "search-serper-me-docpages",
    "search-serper-vt-docpages",
    "serper_places",
    "serper_places_cleanup",
    "serper_places_cleanup_abby_medical",
    "serper_places_cleanup_omega_professional_center",
    "serper_places_or_prepared_import_cleanup",
    "serper_places_rejected_category",
    "serper_search",
    "site-restricted-serper",
    "sos-ca-bizfile",
    "sos-ct",
    "sos-ri",
    "subdpoly-arcgis",
    "tn-cocke-grainger-gap-curated",
    "tn_stale_geometry_cleanup",
    "tn_stale_geometry_cleanup_final",
    "tn_stale_geometry_cleanup_incremental",
    "trec_hoa_management_certificates",
    "tx-trec-hoa-management-certificate",
    "tx_trec_hoa_management_certificates",
    "va-chesterfield-county-subdivisions",
    "va-county-gis",
    "va-fairfax-county-subdivisions",
    "va-henrico-county-hoa-lookup",
    "va-loudoun-county-subdivisions",
    "va-stafford-county-subdivisions",
    "vt-curated-serper",
    "wa-snohomish-county-subdivisions",
    "zip_gazetteer",
    "zip_gazetteer_recovery",
)

# Anchor phrases used to up-weight property addresses and down-weight
# sponsor/law-firm/registered-agent addresses in OCR text.
POSITIVE_ANCHORS = (
    r"premises",
    r"subject property",
    r"located at",
    r"known as",
    r"commonly known",
    r"property is",
    r"property situated",
    r"real property",
    r"described as",
    r"property address",
    r"site address",
)
NEGATIVE_ANCHORS = (
    r"attorneys?",
    r"counsel",
    r"law offices?",
    r"registered agent",
    r"prepared by",
    r"returned to",
    r"recorder",
    r"after recording",
    r"mail to",
    r"please return",
    r"office of the secretary",
    r"agent for service",
)
POS_RE = re.compile("|".join(POSITIVE_ANCHORS), re.IGNORECASE)
NEG_RE = re.compile("|".join(NEGATIVE_ANCHORS), re.IGNORECASE)


def load_bboxes() -> dict[str, dict[str, float]]:
    raw = json.loads(BBOX_PATH.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def padded(bbox: dict[str, float], pad: float = BBOX_PAD_DEG) -> dict[str, float]:
    return {
        "min_lat": bbox["min_lat"] - pad,
        "max_lat": bbox["max_lat"] + pad,
        "min_lon": bbox["min_lon"] - pad,
        "max_lon": bbox["max_lon"] + pad,
    }


def in_bbox(lat: float, lon: float, bbox: dict[str, float]) -> bool:
    return (
        bbox["min_lat"] <= lat <= bbox["max_lat"]
        and bbox["min_lon"] <= lon <= bbox["max_lon"]
    )


def fetch_oob_rows(
    base_url: str,
    token: str,
    bboxes: dict[str, dict[str, float]],
) -> dict[str, list[dict[str, Any]]]:
    """Hit /admin/list-corruption-targets once, classify each row by state.

    Returns: {state: [row, ...]} where each row is OOB for that state.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"sources": list(KNOWN_SOURCES), "require_lat": True}
    r = requests.post(
        f"{base_url}/admin/list-corruption-targets",
        headers=headers,
        json=body,
        timeout=600,
    )
    r.raise_for_status()
    all_rows = r.json().get("rows", [])
    by_state: dict[str, list[dict[str, Any]]] = {}
    for row in all_rows:
        st = (row.get("state") or "").upper()
        if st not in bboxes:
            continue
        lat = row.get("latitude")
        lon = row.get("longitude")
        if lat is None or lon is None:
            continue
        pad_box = padded(bboxes[st])
        if in_bbox(float(lat), float(lon), pad_box):
            continue
        by_state.setdefault(st, []).append(row)
    return by_state


def fetch_state_coverage(base_url: str, token: str) -> dict[str, dict[str, int]]:
    r = requests.get(
        f"{base_url}/admin/state-doc-coverage",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    r.raise_for_status()
    out: dict[str, dict[str, int]] = {}
    for row in r.json().get("results", []):
        out[row["state"]] = {
            "live": int(row.get("live") or 0),
            "with_docs": int(row.get("with_docs") or 0),
        }
    return out


def fetch_hoa_ocr_text(name: str, base_url: str, max_chars: int = 6000) -> str:
    """Concatenate OCR text from up to 5 documents for an HOA."""
    enc = quote(name, safe="")
    try:
        rl = requests.get(f"{base_url}/hoas/{enc}/documents", timeout=240)
    except requests.RequestException:
        return ""
    if rl.status_code != 200:
        return ""
    try:
        docs_raw = rl.json()
    except ValueError:
        return ""
    docs = docs_raw if isinstance(docs_raw, list) else (docs_raw.get("results") or [])

    chunks: list[str] = []
    total = 0
    for d in docs[:5]:
        if total >= max_chars:
            break
        rp = d.get("relative_path") or d.get("path") or d.get("filename")
        if not rp:
            continue
        try:
            r = requests.get(
                f"{base_url}/hoas/{enc}/documents/searchable",
                params={"path": rp},
                timeout=60,
            )
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        for m in re.finditer(r"<pre[^>]*>([\s\S]*?)</pre>", r.text, re.IGNORECASE):
            inner = re.sub(r"<[^>]+>", " ", m.group(1))
            inner = unescape(inner)
            inner = re.sub(r"\s+", " ", inner).strip()
            if inner:
                chunks.append(inner)
                total += len(inner)
                if total >= max_chars:
                    break
    return " ".join(chunks)[:max_chars]


def _score_anchor(text: str, span: tuple[int, int], window: int = 80) -> int:
    start, end = span
    around = text[max(0, start - window):min(len(text), end + window)]
    score = 0
    if POS_RE.search(around):
        score += 2
    if NEG_RE.search(around):
        score -= 3
    return score


def extract_address_candidates_anchored(
    ocr_text: str,
    state: str,
) -> list[dict[str, Any]]:
    """Return ranked address candidates: street+city+ST+ZIP queries.

    Each candidate dict: {query, score, occurrences, source_span}.

    Ranking:
      - +2 for any positive anchor within 80 chars
      - -3 for any negative anchor within 80 chars
      - +1 for each repeat occurrence of the same address in the text
      - first-seen rank as tie-breaker
    """
    candidates: dict[str, dict[str, Any]] = {}
    state_upper = state.upper()
    for m in CITY_STATE_ZIP_RE.finditer(ocr_text):
        city, st, zipc = m.groups()
        if st.upper() != state_upper:
            continue
        back = ocr_text[max(0, m.start() - 80):m.start()]
        street_matches = list(STREET_ADDR_RE.finditer(back))
        if street_matches:
            street = street_matches[-1].group(1).strip()
            q = f"{street}, {city.strip()}, {st} {zipc}"
        else:
            q = f"{city.strip()}, {st} {zipc}"
        existing = candidates.get(q)
        score = _score_anchor(ocr_text, (m.start(), m.end()))
        if existing:
            existing["occurrences"] += 1
            existing["score"] += score
        else:
            candidates[q] = {
                "query": q,
                "score": score,
                "occurrences": 1,
                "first_seen": m.start(),
            }
    ranked = sorted(
        candidates.values(),
        key=lambda c: (c["occurrences"] >= 2, c["score"], -c["first_seen"]),
        reverse=True,
    )
    return ranked[:5]


def here_geocode_strict(
    query: str,
    api_key: str,
    state: str,
    bbox: dict[str, float],
    cache: dict[str, Any],
    rate_sleep: float,
) -> tuple[dict[str, Any] | None, int | None]:
    """Hit HERE; accept only houseNumber/street results in-state and in-bbox.

    Returns (item, status_code). status_code is non-None only on HTTP error
    that should stop the run (e.g. 429).
    """
    if query in cache:
        return cache[query], None
    params = {"q": query, "apikey": api_key, "in": "countryCode:USA", "limit": 5}
    url = HERE_ENDPOINT + "?" + urlencode(params)
    try:
        r = requests.get(url, timeout=15)
    except requests.RequestException:
        cache[query] = None
        return None, None
    if r.status_code == 429:
        return None, 429
    if r.status_code != 200:
        cache[query] = None
        return None, None
    try:
        items = r.json().get("items") or []
    except ValueError:
        cache[query] = None
        return None, None
    time.sleep(rate_sleep)

    state_full = STATE_FULL.get(state.upper(), state)
    best = None
    for item in items:
        rt = (item.get("resultType") or "").lower()
        if rt not in ("housenumber", "street"):
            continue
        addr = item.get("address") or {}
        ist = (addr.get("stateCode") or addr.get("state") or "").strip()
        if ist.upper() != state.upper() and ist != state_full:
            continue
        pos = item.get("position") or {}
        lat, lon = pos.get("lat"), pos.get("lng")
        if lat is None or lon is None:
            continue
        if not in_bbox(float(lat), float(lon), bbox):
            continue
        best = {
            "latitude": float(lat),
            "longitude": float(lon),
            "street": addr.get("street") or (addr.get("label") or "")[:120],
            "label": addr.get("label"),
            "result_type": rt,
        }
        break
    cache[query] = best
    return best, None


def write_ledger_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def load_ledger_done(path: Path) -> set[int]:
    if not path.exists():
        return set()
    out: set[int] = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            hid = row.get("hoa_id")
            if isinstance(hid, int):
                out.add(hid)
    return out


def post_backfill_batch(
    records: list[dict[str, Any]], base_url: str, token: str
) -> dict[str, Any]:
    r = requests.post(
        f"{base_url}/admin/backfill-locations",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"records": records},
        timeout=120,
    )
    if r.status_code != 200:
        return {"status": r.status_code, "error": r.text[:300]}
    return r.json()


def snapshot_db(base_url: str, token: str) -> dict[str, Any]:
    r = requests.post(
        f"{base_url}/admin/backup-full",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={},
        timeout=60,
    )
    return {"status": r.status_code, "body": (r.json() if r.status_code == 200 else r.text[:300])}


# ---------------------------------------------------------------- modes


def run_tally(args: argparse.Namespace, run_dir: Path) -> int:
    token = live_admin_token()
    if not token:
        print("FATAL: no admin token", file=sys.stderr)
        return 2
    bboxes = load_bboxes()
    coverage = fetch_state_coverage(args.base_url, token)
    by_state = fetch_oob_rows(args.base_url, token, bboxes)

    tally: dict[str, Any] = {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "bbox_pad_deg": BBOX_PAD_DEG,
        "states": {},
        "totals": {
            "oob_total": 0,
            "oob_polygon": 0,
            "oob_repairable": 0,
            "states_eligible": 0,
            "states_skipped_small": 0,
        },
    }
    for st in sorted(bboxes):
        rows = by_state.get(st, [])
        cov = coverage.get(st, {})
        live = cov.get("live", 0)
        with_docs = cov.get("with_docs", 0)
        polygon_oob = sum(1 for r in rows if (r.get("location_quality") == "polygon"))
        repairable = len(rows) - polygon_oob
        eligible = live >= 100 and with_docs >= 50 and repairable > 0
        tally["states"][st] = {
            "live": live,
            "with_docs": with_docs,
            "oob_total": len(rows),
            "oob_polygon": polygon_oob,
            "oob_repairable": repairable,
            "eligible": bool(eligible),
        }
        tally["totals"]["oob_total"] += len(rows)
        tally["totals"]["oob_polygon"] += polygon_oob
        tally["totals"]["oob_repairable"] += repairable
        if eligible:
            tally["totals"]["states_eligible"] += 1
        elif rows:
            tally["totals"]["states_skipped_small"] += 1

    # Projected HERE budget: top-3 candidates per repairable, miss rate ~50%
    # so expected ~2 calls per HOA.
    projected_here_lo = tally["totals"]["oob_repairable"]
    projected_here_hi = tally["totals"]["oob_repairable"] * 3
    tally["projected_here_calls_low"] = projected_here_lo
    tally["projected_here_calls_high"] = projected_here_hi

    out = run_dir / "tally.json"
    out.write_text(json.dumps(tally, indent=2))

    # Print human summary
    print(f"\nOOB tally — pad={BBOX_PAD_DEG}°\n")
    print(f"{'STATE':<6} {'LIVE':>7} {'WDOCS':>7} {'OOB':>6} {'POLY':>5} {'REPAIR':>7} ELIG")
    for st in sorted(tally["states"], key=lambda s: -tally["states"][s]["oob_repairable"]):
        s = tally["states"][st]
        if s["oob_total"] == 0:
            continue
        print(
            f"{st:<6} {s['live']:>7} {s['with_docs']:>7} {s['oob_total']:>6} "
            f"{s['oob_polygon']:>5} {s['oob_repairable']:>7} {'Y' if s['eligible'] else '-'}"
        )
    print()
    print(f"Total OOB rows:           {tally['totals']['oob_total']}")
    print(f"  polygon (manual review):{tally['totals']['oob_polygon']}")
    print(f"  repairable:             {tally['totals']['oob_repairable']}")
    print(f"States eligible:          {tally['totals']['states_eligible']}")
    print(f"States skipped (small):   {tally['totals']['states_skipped_small']}")
    print(f"Projected HERE calls:     {projected_here_lo} (low) — {projected_here_hi} (high)")
    print(f"\nWrote {out}")
    return 0


def run_repair(args: argparse.Namespace, run_dir: Path) -> int:
    tally_path = run_dir / "tally.json"
    if not tally_path.exists():
        print(f"FATAL: tally.json missing at {tally_path}. Run --mode tally first.", file=sys.stderr)
        return 2
    if args.max_here_calls <= 0:
        print("FATAL: --max-here-calls required for repair mode (use --max-here-calls 5000)", file=sys.stderr)
        return 2
    tally = json.loads(tally_path.read_text())

    token = live_admin_token()
    if not token:
        print("FATAL: no admin token", file=sys.stderr)
        return 2
    api_key = os.environ.get("HERE_API_KEY", "").strip()
    if not api_key:
        print("FATAL: HERE_API_KEY missing", file=sys.stderr)
        return 2

    bboxes = load_bboxes()
    by_state = fetch_oob_rows(args.base_url, token, bboxes)
    cache_path = ROOT / "data" / "here_geocode_cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache: dict[str, Any] = (
        json.loads(cache_path.read_text()) if cache_path.exists() else {}
    )

    rate_sleep = 1.0 / args.rate_per_sec if args.rate_per_sec > 0 else 0
    here_calls = 0
    totals = {"repaired": 0, "demoted": 0, "skipped_polygon": 0, "errors": 0}
    per_state: dict[str, dict[str, int]] = {}

    states_ordered = sorted(
        tally["states"],
        key=lambda s: -tally["states"][s]["oob_repairable"],
    )
    states_to_run = [s for s in states_ordered if tally["states"][s]["eligible"]]
    if args.only_states:
        only = {s.upper() for s in args.only_states.split(",") if s.strip()}
        states_to_run = [s for s in states_to_run if s in only]
    print(f"States to run: {len(states_to_run)} — {','.join(states_to_run)}")

    stop_reason: str | None = None

    for st in states_to_run:
        if stop_reason:
            break
        rows = by_state.get(st, [])
        state_dir = ROOT / "state_scrapers" / st.lower() / "results" / run_dir.name
        ledger_path = state_dir / "oob_repair_ledger.jsonl"
        done = load_ledger_done(ledger_path)
        per_state[st] = {"repaired": 0, "demoted": 0, "skipped_polygon": 0}
        bbox_padded = padded(bboxes[st])

        print(f"\n=== {st} — {len(rows)} OOB rows ({len(done)} already in ledger) ===")
        repair_batch: list[dict[str, Any]] = []
        demote_batch: list[dict[str, Any]] = []
        ledger_pending: list[dict[str, Any]] = []

        def flush() -> None:
            if repair_batch:
                resp = post_backfill_batch(repair_batch, args.base_url, token)
                print(f"  repair batch ({len(repair_batch)}): {resp}")
                time.sleep(0.5)
            if demote_batch:
                resp = post_backfill_batch(demote_batch, args.base_url, token)
                print(f"  demote batch ({len(demote_batch)}): {resp}")
                time.sleep(0.5)
            for lr in ledger_pending:
                write_ledger_row(ledger_path, lr)
            repair_batch.clear()
            demote_batch.clear()
            ledger_pending.clear()

        for i, row in enumerate(rows, 1):
            hid = row.get("hoa_id")
            if hid in done:
                continue
            quality = row.get("location_quality") or ""
            if quality == "polygon":
                lr = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "hoa_id": hid,
                    "hoa": row.get("hoa"),
                    "state": st,
                    "source": row.get("source"),
                    "old_lat": row.get("latitude"),
                    "old_lon": row.get("longitude"),
                    "old_quality": quality,
                    "decision": "skipped_polygon",
                    "reason": "polygon centroid OOB — needs manual review",
                }
                ledger_pending.append(lr)
                per_state[st]["skipped_polygon"] += 1
                totals["skipped_polygon"] += 1
                continue

            if here_calls >= args.max_here_calls:
                stop_reason = f"hit --max-here-calls={args.max_here_calls}"
                break

            name = row.get("hoa") or ""
            ocr = fetch_hoa_ocr_text(name, args.base_url)
            candidates = extract_address_candidates_anchored(ocr, st) if ocr else []

            here_hit = None
            tried = []
            for cand in candidates[:3]:
                if here_calls >= args.max_here_calls:
                    stop_reason = f"hit --max-here-calls={args.max_here_calls}"
                    break
                hit, http_err = here_geocode_strict(
                    cand["query"], api_key, st, bbox_padded, cache, rate_sleep
                )
                here_calls += 1
                tried.append(cand["query"])
                if http_err == 429:
                    stop_reason = "HERE 429 — quota or rate limit"
                    break
                if hit:
                    here_hit = hit
                    here_hit["_matched_query"] = cand["query"]
                    here_hit["_score"] = cand["score"]
                    here_hit["_occurrences"] = cand["occurrences"]
                    break

            if stop_reason:
                break

            lr = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "hoa_id": hid,
                "hoa": name,
                "state": st,
                "source": row.get("source"),
                "old_lat": row.get("latitude"),
                "old_lon": row.get("longitude"),
                "old_quality": quality,
                "ocr_address": tried[0] if tried else None,
                "candidates_tried": len(tried),
                "ocr_chars": len(ocr),
            }
            if here_hit:
                lr.update({
                    "here_address": here_hit.get("label"),
                    "new_lat": here_hit["latitude"],
                    "new_lon": here_hit["longitude"],
                    "decision": "repaired",
                    "reason": "HERE in-state hit",
                })
                repair_batch.append({
                    "hoa": name,
                    "latitude": here_hit["latitude"],
                    "longitude": here_hit["longitude"],
                    "street": here_hit.get("street") or None,
                    "location_quality": "address",
                })
                per_state[st]["repaired"] += 1
                totals["repaired"] += 1
            else:
                reason = "no_candidates" if not candidates else "no_here_hit"
                if not ocr:
                    reason = "no_ocr_text"
                lr.update({
                    "decision": "demoted",
                    "reason": reason,
                })
                demote_batch.append({
                    "hoa": name,
                    "location_quality": "city_only",
                    "clear_coordinates": True,
                    "clear_boundary_geojson": True,
                })
                per_state[st]["demoted"] += 1
                totals["demoted"] += 1
            ledger_pending.append(lr)

            if len(repair_batch) + len(demote_batch) >= 25:
                flush()
            if i % 10 == 0:
                cache_path.write_text(json.dumps(cache, indent=2))
                print(
                    f"  [{i}/{len(rows)}] here_calls={here_calls} "
                    f"repaired={per_state[st]['repaired']} demoted={per_state[st]['demoted']}"
                )

        flush()
        cache_path.write_text(json.dumps(cache, indent=2))
        (state_dir / "summary.json").write_text(json.dumps({
            "state": st,
            "oob_total": len(rows),
            **per_state[st],
            "stop_reason": stop_reason,
        }, indent=2))
        print(
            f"  {st} done: repaired={per_state[st]['repaired']} "
            f"demoted={per_state[st]['demoted']} skipped_polygon={per_state[st]['skipped_polygon']}"
        )

    # Orchestrator summary
    summary = {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "totals": totals,
        "per_state": per_state,
        "here_calls": here_calls,
        "max_here_calls": args.max_here_calls,
        "stop_reason": stop_reason,
        "states_processed": list(per_state.keys()),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print("\n=== TOTALS ===")
    print(json.dumps(summary, indent=2))
    return 0 if not stop_reason or stop_reason.startswith("hit --max") else 1


def run_retry_demoted(args: argparse.Namespace, run_dir: Path) -> int:
    """Retry HERE repair on rows previously demoted with reason in
    {no_candidates}. Used after fixing the CITY_STATE_ZIP_RE bug.

    Reads each state's existing ledger, filters to demoted+ocr_chars>0
    rows, refetches OCR, runs anchored extraction with the corrected
    regex, calls HERE, and posts a backfill upgrade (city_only → address)
    only on a clean in-state hit. Writes results to a sibling
    ``retry_ledger.jsonl`` so the original ledger stays intact.
    """
    token = live_admin_token()
    if not token:
        print("FATAL: no admin token", file=sys.stderr)
        return 2
    api_key = os.environ.get("HERE_API_KEY", "").strip()
    if not api_key:
        print("FATAL: HERE_API_KEY missing", file=sys.stderr)
        return 2
    if args.max_here_calls <= 0:
        print("FATAL: --max-here-calls required (e.g. 200)", file=sys.stderr)
        return 2

    bboxes = load_bboxes()
    cache_path = ROOT / "data" / "here_geocode_cache.json"
    cache: dict[str, Any] = (
        json.loads(cache_path.read_text()) if cache_path.exists() else {}
    )
    rate_sleep = 1.0 / args.rate_per_sec if args.rate_per_sec > 0 else 0

    # Find all per-state ledgers for this run
    state_dirs = sorted((ROOT / "state_scrapers").glob(
        f"*/results/{run_dir.name}/oob_repair_ledger.jsonl"
    ))
    print(f"Found {len(state_dirs)} per-state ledgers for run {run_dir.name}")

    here_calls = 0
    totals = {"upgraded": 0, "still_demoted": 0, "skipped": 0}
    stop_reason: str | None = None
    backfill_batch: list[dict[str, Any]] = []
    retry_pending: list[tuple[Path, dict[str, Any]]] = []

    def flush_batch() -> None:
        if not backfill_batch:
            return
        resp = post_backfill_batch(backfill_batch, args.base_url, token)
        print(f"  upgrade batch ({len(backfill_batch)}): {resp}")
        time.sleep(0.5)
        backfill_batch.clear()

    def flush_retry_ledger() -> None:
        for retry_path, row in retry_pending:
            write_ledger_row(retry_path, row)
        retry_pending.clear()

    for ledger_path in state_dirs:
        if stop_reason:
            break
        st = ledger_path.parts[-4].upper()
        if st not in bboxes:
            continue
        bbox_padded = padded(bboxes[st])
        retry_path = ledger_path.parent / "retry_ledger.jsonl"
        already_retried = load_ledger_done(retry_path)

        # Read original ledger rows
        candidates: list[dict[str, Any]] = []
        for line in ledger_path.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("decision") != "demoted":
                continue
            if (r.get("ocr_chars") or 0) <= 0:
                continue
            if r.get("hoa_id") in already_retried:
                continue
            candidates.append(r)
        if not candidates:
            continue
        print(f"\n=== {st} retry — {len(candidates)} candidates ===")

        for i, row in enumerate(candidates, 1):
            if here_calls >= args.max_here_calls:
                stop_reason = f"hit --max-here-calls={args.max_here_calls}"
                break
            name = row.get("hoa") or ""
            ocr = fetch_hoa_ocr_text(name, args.base_url)
            cands = extract_address_candidates_anchored(ocr, st) if ocr else []
            if not cands:
                retry_pending.append((retry_path, {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "hoa_id": row.get("hoa_id"),
                    "hoa": name,
                    "state": st,
                    "decision": "still_demoted",
                    "reason": "no_candidates_after_regex_fix",
                    "ocr_chars": len(ocr),
                }))
                totals["still_demoted"] += 1
                continue
            here_hit = None
            tried = []
            for cand in cands[:3]:
                if here_calls >= args.max_here_calls:
                    stop_reason = f"hit --max-here-calls={args.max_here_calls}"
                    break
                hit, http_err = here_geocode_strict(
                    cand["query"], api_key, st, bbox_padded, cache, rate_sleep
                )
                here_calls += 1
                tried.append(cand["query"])
                if http_err == 429:
                    stop_reason = "HERE 429 — quota or rate limit"
                    break
                if hit:
                    here_hit = hit
                    break
            if stop_reason:
                break

            log = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "hoa_id": row.get("hoa_id"),
                "hoa": name,
                "state": st,
                "candidates_tried": tried,
            }
            if here_hit:
                log.update({
                    "decision": "upgraded",
                    "new_lat": here_hit["latitude"],
                    "new_lon": here_hit["longitude"],
                    "here_address": here_hit.get("label"),
                })
                backfill_batch.append({
                    "hoa": name,
                    "latitude": here_hit["latitude"],
                    "longitude": here_hit["longitude"],
                    "street": here_hit.get("street") or None,
                    "location_quality": "address",
                })
                totals["upgraded"] += 1
            else:
                log.update({"decision": "still_demoted", "reason": "no_here_hit"})
                totals["still_demoted"] += 1
            retry_pending.append((retry_path, log))

            if len(backfill_batch) >= 25:
                flush_batch()
            if i % 5 == 0:
                cache_path.write_text(json.dumps(cache, indent=2))

        flush_batch()
        flush_retry_ledger()
        cache_path.write_text(json.dumps(cache, indent=2))

    flush_batch()
    flush_retry_ledger()
    cache_path.write_text(json.dumps(cache, indent=2))

    summary = {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "mode": "retry-demoted",
        "totals": totals,
        "here_calls": here_calls,
        "stop_reason": stop_reason,
    }
    (run_dir / "retry_summary.json").write_text(json.dumps(summary, indent=2))
    print("\n=== RETRY TOTALS ===")
    print(json.dumps(summary, indent=2))
    return 0


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("tally", "repair", "retry-demoted"), default="tally")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--run-id", default=f"oob_repair_{datetime.now(timezone.utc).strftime('%Y_%m_%d')}")
    ap.add_argument("--rate-per-sec", type=float, default=4.0,
                    help="HERE request pace (req/s)")
    ap.add_argument("--max-here-calls", type=int, default=0,
                    help="Hard cap on HERE calls per run (required for repair mode)")
    ap.add_argument("--only-states", type=str, default="",
                    help="Comma-separated state codes to limit the repair pass to")
    args = ap.parse_args()

    run_dir = ROOT / "state_scrapers" / "_orchestrator" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "tally":
        return run_tally(args, run_dir)
    if args.mode == "retry-demoted":
        return run_retry_demoted(args, run_dir)
    return run_repair(args, run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
