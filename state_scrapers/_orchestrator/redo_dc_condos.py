#!/usr/bin/env python3
"""DC condo/coop re-discovery — parallel to the main 9-state orchestrator.

The original DC sweep used HOA-flavored queries ("Declaration of Covenants",
"Homeowners Association") and matched .gov noise (Congressional Record, court
filings, planning packets). DC's actual stock is condos and coops which use
different governing-doc language:

  - "Condominium Declaration", "Master Deed", "Declaration of Condominium"
  - "Unit Owners Association", "Council of Unit Owners"
  - "Cooperative Apartment", "Tenants Corp", "Cooperative Bylaws"
  - "Condominium Bylaws", "House Rules"
  - "DC Cooperative Housing Association"
  - DC-specific anchors: ZIPs 200xx, neighborhood + "condominium"

Bank results land in gs://hoaproxy-bank/v1/DC/ alongside the original sweep
(slug dedup is safe). After the main orchestrator completes UT, we'll re-run
prepare + import + Phase 10 for DC to pick up the new manifests.

Designed to run unattended via nohup.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Substantial DC condo buildings + neighborhoods. The original sweep already
# tried bare neighborhood names; here we anchor with condo/coop language.
DC_NEIGHBORHOODS_CONDO = [
    "Dupont Circle", "Logan Circle", "Foggy Bottom", "Georgetown",
    "Adams Morgan", "Kalorama", "West End", "Mount Vernon Triangle",
    "Penn Quarter", "Navy Yard", "NoMa", "U Street",
    "Cleveland Park", "Woodley Park", "Chevy Chase DC", "Friendship Heights",
    "Tenleytown", "Columbia Heights", "Petworth", "Mount Pleasant",
    "Capitol Hill", "Capitol Riverfront", "Southwest Waterfront",
    "Brookland", "Bloomingdale", "Shaw",
]

# Famous DC condo / coop buildings — recognizable names.
DC_KNOWN_BUILDINGS = [
    "The Watergate", "Wardman Tower", "The Cairo", "The Mendota",
    "Westchester Apartments", "Marlyn Apartments", "Chastleton",
    "Kennedy-Warren", "Tilden Gardens", "Macomb Gardens",
    "Cathedral Mansions", "The Plaza", "The Beacon", "City Vista",
    "The Whitman", "Madrigal Lofts", "Lansburgh", "The Quincy",
    "Park Connecticut", "Sedgwick Gardens",
]

DC_ZIPS_NW = ["20001", "20002", "20003", "20004", "20005", "20006", "20007",
              "20008", "20009", "20010", "20011", "20012", "20015", "20016",
              "20017", "20018", "20019", "20020", "20024", "20036", "20037"]


def now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def write_queries(qdir: Path, name: str, lines: list[str]) -> Path:
    qdir.mkdir(parents=True, exist_ok=True)
    path = qdir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def queries_for_neighborhood(n: str) -> list[str]:
    return [
        f'filetype:pdf "{n}" "Washington, DC" "Condominium Declaration"',
        f'filetype:pdf "{n}" "DC" "Declaration of Condominium" "Unit Owners Association"',
        f'filetype:pdf "{n}" "Washington" "Master Deed" "Condominium"',
        f'filetype:pdf "{n}" "District of Columbia" "Council of Unit Owners"',
        f'filetype:pdf "{n}" "Washington DC" "Cooperative" "Bylaws"',
        f'"{n}" "Washington" "Condominium Association" "Bylaws" filetype:pdf',
        f'"{n}" "DC" "House Rules" "Condominium" filetype:pdf',
        f'site:.org "{n}" "Washington" "Condominium" "Declaration"',
        f'inurl:/wp-content/uploads/ "Washington" "{n}" "condominium association"',
    ]


def queries_for_building(b: str) -> list[str]:
    return [
        f'"{b}" "Washington" "Condominium" filetype:pdf',
        f'"{b}" "DC" "Declaration" filetype:pdf',
        f'"{b}" "District of Columbia" "Bylaws"',
        f'"{b}" "Cooperative" "Tenants" "DC"',
    ]


def queries_for_zip(z: str) -> list[str]:
    return [
        f'filetype:pdf "{z}" "Condominium Declaration" "Washington"',
        f'filetype:pdf "{z}" "Master Deed" "DC"',
        f'filetype:pdf "{z}" "Unit Owners Association" "Washington"',
    ]


STATEWIDE_DC = [
    'filetype:pdf "Recorder of Deeds, District of Columbia" "Condominium Declaration"',
    'filetype:pdf "Recorder of Deeds, District of Columbia" "Cooperative"',
    'filetype:pdf "DC Condominium Act" "Declaration"',
    'filetype:pdf "Title 42 Subtitle X" "Condominium" "DC"',
    'filetype:pdf "Council of Unit Owners" "Washington" "DC"',
    'filetype:pdf "Cooperative Housing Association" "District of Columbia"',
    'filetype:pdf "Tenants Corporation" "Washington" "DC" "Bylaws"',
    'filetype:pdf "Limited-Equity Cooperative" "Washington" "DC"',
    'filetype:pdf "Cooperative Apartment Building" "DC" "Bylaws"',
    '"Washington DC" "Condominium Association" "Declaration" "Bylaws" filetype:pdf',
    '"Washington" "DC" "Condominium" "Master Deed" filetype:pdf',
    '"Washington" "DC" "Council of Unit Owners" filetype:pdf',
    'site:.org "Washington" "DC" "condominium association" "documents"',
    'site:.com "Washington" "DC" "condo association" "bylaws"',
    'site:caboma.org "Washington" "DC"',  # Coop association of greater DC sometimes hosts
    'site:cnhed.org "Washington" "DC" cooperative',
    # DC mgmt-co specific
    '"Comsource Management" "Washington" "DC" "Condominium" filetype:pdf',
    '"FirstService Residential" "Washington" "DC" "Condominium" filetype:pdf',
    '"Legum and Norman" "Washington" "DC" "Condominium" filetype:pdf',
    '"Cardinal Management" "Washington" "DC" "Condominium" filetype:pdf',
    '"Capitol Property Management" "Washington" "DC" filetype:pdf',
    '"McGrath Property Management" "Washington" "DC" filetype:pdf',
    '"Edgewood Management" "Washington" "DC" filetype:pdf',
    '"WJD Management" "Washington" "DC" "Condominium" filetype:pdf',
]


def main() -> int:
    qdir = ROOT / "state_scrapers/dc/queries"
    log_path = ROOT / "state_scrapers/_orchestrator/dc_condos_redo.log"
    run_id = f"dc_condos_redo_{now_id()}"

    def log(msg: str) -> None:
        line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    log(f"=== DC condo/coop re-discovery starting run_id={run_id} ===")

    # Build query files
    sweeps: list[tuple[Path, str]] = []
    # Statewide condo/coop sweep (most likely to surface real condos)
    p = write_queries(qdir, "dc_condo_statewide_serper_queries.txt", STATEWIDE_DC)
    sweeps.append((p, "DC"))

    # Per-neighborhood sweeps
    for n in DC_NEIGHBORHOODS_CONDO:
        slug = n.lower().replace(" ", "-").replace(",", "")
        qs = queries_for_neighborhood(n)
        p = write_queries(qdir, f"dc_condo_{slug}_serper_queries.txt", qs)
        sweeps.append((p, "DC"))

    # Per-known-building sweeps (only first 10 to bound budget)
    bld_lines: list[str] = []
    for b in DC_KNOWN_BUILDINGS[:14]:
        bld_lines.extend(queries_for_building(b))
    p = write_queries(qdir, "dc_condo_known_buildings_serper_queries.txt", bld_lines)
    sweeps.append((p, "DC"))

    # ZIP-anchored sweep — combine multiple ZIPs into one file
    zip_lines: list[str] = []
    for z in DC_ZIPS_NW[:10]:
        zip_lines.extend(queries_for_zip(z))
    p = write_queries(qdir, "dc_condo_zips_serper_queries.txt", zip_lines)
    sweeps.append((p, "DC"))

    log(f"Built {len(sweeps)} sweep files; starting Serper sweeps")

    serper_script = ROOT / "benchmark/scrape_state_serper_docpages.py"
    py = ROOT / ".venv/bin/python"

    total_banked = 0
    for i, (qfile, county) in enumerate(sweeps):
        cmd = [
            str(py), str(serper_script),
            "--state", "DC",
            "--state-name", "District of Columbia",
            "--queries-file", str(qfile),
            "--max-queries", "30",
            "--results-per-query", "10",
            "--max-leads", "60",
            "--min-score", "5",
            "--require-state-hint",
            "--fetch-pages",
            "--include-direct-pdfs",
            "--probe",
            "--probe-delay", "1.5",
            "--probe-timeout", "240",
            "--max-pdfs-per-lead", "8",
            "--bucket", "hoaproxy-bank",
            "--default-county", county,
        ]
        sweep_log = ROOT / f"state_scrapers/_orchestrator/dc_condos_redo_{i:02d}_{qfile.stem}.log"
        log(f"Sweep {i+1}/{len(sweeps)}: {qfile.name}")
        with sweep_log.open("w", encoding="utf-8") as out:
            try:
                proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=out, stderr=subprocess.STDOUT, timeout=2400)
                log(f"  rc={proc.returncode}")
            except subprocess.TimeoutExpired:
                log(f"  TIMEOUT (40 min) on {qfile.name}; continuing")
        time.sleep(10)  # breathing room between sweeps

    log("=== DC condo/coop re-discovery sweep complete ===")
    log("Run prepare+import+Phase 10 for DC after main orchestrator finishes:")
    log("  scripts/prepare_bank_for_ingest.py --state DC --max-docai-cost-usd 15 ...")

    # Touch a sentinel file the main orchestrator can detect
    sentinel = ROOT / "state_scrapers/_orchestrator/dc_condos_redo_DONE.flag"
    sentinel.write_text(json.dumps({"finished_at": datetime.now(timezone.utc).isoformat(timespec='seconds'), "run_id": run_id}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
