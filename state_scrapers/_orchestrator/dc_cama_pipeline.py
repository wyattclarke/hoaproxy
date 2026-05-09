#!/usr/bin/env python3
"""DC name-list-first pipeline using DC GIS CAMA data.

The CONDO REGIME table at DC GIS exposes 3,289 registered condominium projects
by name. The COMMERCIAL CAMA table includes housing cooperatives (single-entity
ownership for tax purposes). These give us the EXHAUSTIVE entity universe for
DC condos+coops — what Serper-only discovery cannot do because most DC
governing docs aren't in Google's public index.

Pipeline:
  1. Pull all CONDO REGIME records → list of project names with REGIME_ID, COUNT.
  2. Clean and dedupe names (drop empty, drop "CONDO" suffix duplicates).
  3. Build name-anchored Serper queries: `"<NAME>" "Washington" "DC" filetype:pdf`
     plus `"<NAME>" "Washington" "DC" "Bylaws"`.
  4. Run benchmark/scrape_state_serper_docpages.py against the batched queries
     file (probe + bank inline). High precision per query, so even the modest
     ~5-15% hit rate on 3,000+ names yields hundreds of real banked condos.
  5. Run Serper + bank for cooperatives from COMMERCIAL CAMA.
  6. Touch a sentinel so the main orchestrator can run prepare + import + Phase
     10 against the new manifests after UT finishes.

Designed to run unattended via nohup, parallel to the main orchestrator.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]

DCGIS_BASE = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/Property_and_Land_WebMercator/FeatureServer"
CONDO_REGIME_TABLE = "72"          # 3,289 records — condominium projects
COMMERCIAL_CAMA_TABLE = "23"        # cooperatives live here

QDIR = ROOT / "state_scrapers/dc/queries"
LEADS_DIR = ROOT / "state_scrapers/dc/leads"
LOG_PATH = ROOT / "state_scrapers/_orchestrator/dc_cama_pipeline.log"
SENTINEL = ROOT / "state_scrapers/_orchestrator/dc_cama_pipeline_DONE.flag"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_paginated(table_id: str, where: str = "1=1", out_fields: str = "*", page_size: int = 1000) -> list[dict[str, Any]]:
    """Paginate ArcGIS REST query → list of attribute dicts."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "where": where,
            "outFields": out_fields,
            "f": "json",
            "resultRecordCount": page_size,
            "resultOffset": offset,
            "orderByFields": "OBJECTID",
        }
        url = f"{DCGIS_BASE}/{table_id}/query"
        r = requests.get(url, params=params, timeout=120)
        r.raise_for_status()
        body = r.json()
        feats = body.get("features") or []
        if not feats:
            break
        for f in feats:
            attrs = f.get("attributes") or {}
            out.append(attrs)
        if len(feats) < page_size:
            break
        offset += page_size
        time.sleep(0.2)
        # Safety stop
        if offset > 100000:
            break
    return out


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:80]


def clean_condo_name(raw: str) -> str | None:
    if not raw:
        return None
    name = raw.strip()
    # Title-case from CAMA's all-caps style: "218 VISTA CONDO" -> "218 Vista Condo"
    name = " ".join(w.capitalize() if w.isalpha() else w for w in name.split())
    # Skip junk that doesn't look like a condo name
    if len(name) < 3:
        return None
    if name.lower() in ("condo", "condominium", "condos", "n/a", "tbd"):
        return None
    return name


def queries_for_condo(name: str) -> list[str]:
    """Two precision-targeted queries per condo name, plus a coop variant."""
    return [
        f'"{name}" "Washington" "DC" filetype:pdf',
        f'"{name}" "Washington" "DC" "Bylaws"',
        f'"{name}" "DC" "Declaration of Condominium"',
    ]


def fetch_condo_regime() -> list[dict[str, Any]]:
    log("Fetching CONDO REGIME table…")
    rows = fetch_paginated(CONDO_REGIME_TABLE)
    log(f"  fetched {len(rows)} CONDO REGIME records")
    return rows


def fetch_commercial_cooperatives() -> list[dict[str, Any]]:
    """Cooperatives in DC are taxed as single corporate entities; the COMMERCIAL
    CAMA table marks them via USECODE. Common cooperative USECODEs include 005
    (residential cooperative) but the exact code list varies; we filter loosely
    on USECODE and additional name signals like 'COOP' or 'COOPERATIVE' in any
    string field returned."""
    log("Fetching COMMERCIAL CAMA table for cooperatives…")
    # Filter by use category description if present.
    where = "USECODE_DESC LIKE '%COOP%' OR USECODE_DESC LIKE '%Cooperative%'"
    rows = fetch_paginated(COMMERCIAL_CAMA_TABLE, where=where)
    log(f"  fetched {len(rows)} COMMERCIAL CAMA cooperative records (best-effort)")
    return rows


def build_query_file(condo_names: list[str], coop_names: list[str]) -> Path:
    QDIR.mkdir(parents=True, exist_ok=True)
    out = QDIR / "dc_cama_namelist_serper_queries.txt"
    queries: list[str] = []
    seen: set[str] = set()
    for n in condo_names:
        c = clean_condo_name(n)
        if not c or c.lower() in seen:
            continue
        seen.add(c.lower())
        queries.extend(queries_for_condo(c))
    for n in coop_names:
        c = clean_condo_name(n)
        if not c or c.lower() in seen:
            continue
        seen.add(c.lower())
        queries.extend([
            f'"{c}" "Washington" "DC" "Cooperative" filetype:pdf',
            f'"{c}" "Washington" "DC" "Bylaws"',
        ])
    out.write_text("\n".join(queries) + "\n", encoding="utf-8")
    log(f"Wrote {len(queries)} queries to {out}")
    return out


def write_leads_seed(condo_rows: list[dict[str, Any]]) -> Path:
    """Persist the CAMA condo list as a seed JSONL — useful for retrospective
    audit even if Serper finds no docs for some names."""
    LEADS_DIR.mkdir(parents=True, exist_ok=True)
    out = LEADS_DIR / "dc_cama_condo_seed.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for r in condo_rows:
            name = clean_condo_name(r.get("NAME") or "")
            if not name:
                continue
            payload = {
                "name": name,
                "state": "DC",
                "county": "DC",
                "metadata_type": "condo",
                "regime_id": r.get("REGIME_ID"),
                "regime": r.get("REGIME"),
                "complex": r.get("COMPLEX"),
                "unit_count": r.get("COUNT"),
                "source": "dc-gis-cama-condo-regime",
                "source_url": f"{DCGIS_BASE}/{CONDO_REGIME_TABLE}",
            }
            f.write(json.dumps(payload, sort_keys=True) + "\n")
    log(f"Wrote {out} (CAMA seed for retrospective audit)")
    return out


def run_serper_sweep(query_file: Path, run_id: str) -> int:
    """Invoke the existing benchmark scraper. It does Serper search + lead
    inference + probe + GCS bank inline. We do not split the file — the script
    handles all queries in order, with our --max-queries set high enough to
    cover everything."""
    cmd = [
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "benchmark/scrape_state_serper_docpages.py"),
        "--state", "DC",
        "--state-name", "District of Columbia",
        "--queries-file", str(query_file),
        "--max-queries", "10000",       # cover all queries in the file
        "--results-per-query", "8",
        "--max-leads", "3000",          # cap on leads emitted to keep probe wall-time reasonable
        "--min-score", "5",
        "--require-state-hint",
        "--fetch-pages",
        "--include-direct-pdfs",
        "--probe",
        "--probe-delay", "1.2",
        "--probe-timeout", "240",
        "--max-pdfs-per-lead", "8",
        "--bucket", "hoaproxy-bank",
        "--default-county", "DC",
        "--run-id", run_id,
        "--search-delay", "0.2",
    ]
    sweep_log = ROOT / "state_scrapers/_orchestrator/dc_cama_serper_sweep.log"
    with sweep_log.open("w", encoding="utf-8") as out:
        log(f"Launching Serper sweep ({query_file.name}); log → {sweep_log}")
        proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=out, stderr=subprocess.STDOUT)
    log(f"Serper sweep complete rc={proc.returncode}")
    return proc.returncode


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)

    if SENTINEL.exists():
        log(f"Sentinel exists: {SENTINEL}; refusing to re-run. Delete to retry.")
        return 0

    run_id = f"dc_cama_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    log(f"=== DC CAMA name-list-first pipeline starting run_id={run_id} ===")

    # 1. Pull CONDO REGIME (3,289 records)
    try:
        condo_rows = fetch_condo_regime()
    except Exception as exc:
        log(f"FATAL: could not fetch CONDO REGIME: {type(exc).__name__}: {exc}")
        return 2

    # 2. Pull cooperatives from COMMERCIAL CAMA (best-effort)
    try:
        coop_rows = fetch_commercial_cooperatives()
    except Exception as exc:
        log(f"WARN: could not fetch COMMERCIAL CAMA coops: {type(exc).__name__}: {exc}")
        coop_rows = []

    # 3. Persist CAMA seed as JSONL for retrospective
    write_leads_seed(condo_rows)

    # 4. Build query file
    condo_names = [r.get("NAME") or "" for r in condo_rows]
    coop_names = [r.get("PROPNAME") or r.get("NAME") or "" for r in coop_rows]
    qfile = build_query_file(condo_names, coop_names)

    # 5. Run Serper sweep (probe + bank inline)
    rc = run_serper_sweep(qfile, run_id)

    # 6. Sentinel
    SENTINEL.write_text(json.dumps({
        "finished_at": now_iso(),
        "run_id": run_id,
        "condo_regime_rows": len(condo_rows),
        "coop_rows": len(coop_rows),
        "serper_sweep_rc": rc,
    }, indent=2), encoding="utf-8")
    log("=== DC CAMA pipeline complete ===")
    log("Now run: scripts/prepare_bank_for_ingest.py --state DC --max-docai-cost-usd 25 ...")
    log("then POST /admin/ingest-ready-gcs?state=DC, then scripts/phase10_close.py --state DC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
