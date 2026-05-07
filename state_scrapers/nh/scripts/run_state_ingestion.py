#!/usr/bin/env python3
"""Run New Hampshire HOA ingestion end-to-end.

NH is a Tier-1 state (CAI <2,500). The kickoff brief recommended SoS-first,
but the NH QuickStart business-search portal at quickstart.sos.nh.gov is
behind an Akamai/Imperva JavaScript challenge that returns HTTP 403 to any
unauthenticated bot client. There is no SODA / open-data export of the NH
corporations registry. Per the playbook ("if it requires payment / blocked /
closed, immediately fall back to keyword-Serper-per-county over NH's 10
counties"), this run uses keyword-Serper-per-county.

Pipeline (per ``docs/multi-state-ingestion-playbook.md``):

  1. Discovery — keyword-serper sweeps from queries/*.txt files
     (KS/TN/GA/IN-style pattern). NH names overlap with other states
     (Bristol, Lincoln, Washington, Hampton, Manchester all appear in
     multiple states), so per-county anchoring + state-hint requirement
     is mandatory.

  2. prepare_bank_for_ingest.py with --max-docai-cost-usd 10
     → prepared bundles in gs://hoaproxy-ingest-ready/v1/NH/.

  3. POST /admin/ingest-ready-gcs?state=NH in a loop until empty
     (capped at 50 per call per the playbook).

  4. State-local location enrichment → ZIP-centroid backfill via zippopotam.us.

  5. Verify live counts and map coverage; produce final report.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from google.cloud import storage

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------
STATE = "NH"
STATE_NAME = "New Hampshire"
# NH bounding box (USGS-rounded extents). Pittsburg in the north tips up to
# 45.305; Salem on the MA border bottoms out at 42.697. Lon: -72.557 to
# -70.610. Padded slightly to absorb ZIP centroid drift at the edges.
STATE_BBOX: dict[str, float] = {
    "min_lat": 42.69,
    "max_lat": 45.32,
    "min_lon": -72.58,
    "max_lon": -70.60,
}
# ---------------------------------------------------------------------------

BANK_BUCKET = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")
PREPARED_BUCKET = os.environ.get("HOA_PREPARED_GCS_BUCKET", "hoaproxy-ingest-ready")

QUERIES_DIR = ROOT / f"state_scrapers/{STATE.lower()}/queries"
LEADS_DIR   = ROOT / f"state_scrapers/{STATE.lower()}/leads"
SCRIPTS_DIR = ROOT / f"state_scrapers/{STATE.lower()}/scripts"


# ---------------------------------------------------------------------------
# Per-county query runs. Order = priority for budget; highest HOA-density
# metros first.
#   - Hillsborough  (Manchester/Nashua) — population center
#   - Rockingham    (seacoast / Salem / Portsmouth on MA border)
#   - Belknap       (Lake Winnipesaukee resort condos)
#   - Carroll       (White Mountains / North Conway resort condos)
#   - Merrimack     (Concord)
#   - Strafford     (Dover / Rochester)
#   - Grafton       (Hanover / Lebanon / Plymouth)
#   - Cheshire      (Keene)
#   - Sullivan      (Claremont / Newport)
#   - Coos          (Berlin / North Country) — sparse
# ---------------------------------------------------------------------------
COUNTY_RUNS: list[tuple[str, str | None]] = [
    ("nh_hillsborough_serper_queries.txt", "Hillsborough"),
    ("nh_rockingham_serper_queries.txt",   "Rockingham"),
    ("nh_belknap_serper_queries.txt",      "Belknap"),
    ("nh_carroll_serper_queries.txt",      "Carroll"),
    ("nh_merrimack_serper_queries.txt",    "Merrimack"),
    ("nh_strafford_serper_queries.txt",    "Strafford"),
    ("nh_grafton_serper_queries.txt",      "Grafton"),
    ("nh_cheshire_serper_queries.txt",     "Cheshire"),
    ("nh_sullivan_serper_queries.txt",     "Sullivan"),
    ("nh_coos_serper_queries.txt",         "Coos"),
]


def now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def run_command(cmd: list[str], run_dir: Path, name: str, *, apply: bool = True) -> dict[str, Any]:
    log_path = run_dir / f"{name}.log"
    if not apply:
        return {"name": name, "skipped": True, "command": cmd}
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=log, stderr=subprocess.STDOUT)
    return {"name": name, "returncode": proc.returncode, "log": str(log_path), "command": cmd}


def run_county_serper(
    args: argparse.Namespace,
    run_dir: Path,
    queries_file: str,
    default_county: str | None,
    idx: int,
) -> dict[str, Any]:
    queries_path = QUERIES_DIR / queries_file
    if not queries_path.exists():
        return {"skipped": True, "reason": f"missing queries file: {queries_path}"}
    cmd = [
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "benchmark/scrape_state_serper_docpages.py"),
        "--state", STATE,
        "--state-name", STATE_NAME,
        "--queries-file", str(queries_path),
        "--max-queries", str(args.max_queries_per_county),
        "--results-per-query", str(args.results_per_query),
        "--max-leads", str(args.max_leads_per_county),
        "--min-score", "5",
        "--require-state-hint",
        "--fetch-pages",
        "--include-direct-pdfs",
        "--probe",
        "--probe-delay", "1.5",
        "--probe-timeout", "240",
        "--max-pdfs-per-lead", "8",
        "--bucket", args.bank_bucket,
    ]
    if default_county:
        cmd += ["--default-county", default_county]
    label = f"discover_{idx:02d}_{Path(queries_file).stem}"
    return run_command(cmd, run_dir, label, apply=args.apply)


def run_prepare(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    ledger    = run_dir / "prepared_ingest_ledger.jsonl"
    geo_cache = run_dir / "prepared_ingest_geo_cache.json"
    cmd = [
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "scripts/prepare_bank_for_ingest.py"),
        "--state", STATE,
        "--limit", "10000",
        "--max-docai-cost-usd", str(args.max_docai_cost_usd),
        "--ledger", str(ledger),
        "--geo-cache", str(geo_cache),
        "--bank-bucket", args.bank_bucket,
        "--prepared-bucket", args.prepared_bucket,
    ]
    if not args.apply:
        cmd.append("--dry-run")
    return run_command(cmd, run_dir, "20_prepare", apply=True)


def _live_admin_token() -> str | None:
    if os.environ.get("HOAPROXY_ADMIN_BEARER"):
        return os.environ["HOAPROXY_ADMIN_BEARER"]
    api_key    = os.environ.get("RENDER_API_KEY")
    service_id = os.environ.get("RENDER_SERVICE_ID")
    if api_key and service_id:
        try:
            r = requests.get(
                f"https://api.render.com/v1/services/{service_id}/env-vars",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            r.raise_for_status()
            for env in r.json():
                e = env.get("envVar", env)
                if e.get("key") == "JWT_SECRET" and e.get("value"):
                    return e["value"]
        except Exception:
            pass
    return os.environ.get("JWT_SECRET")


def import_ready(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    token = _live_admin_token()
    if not token:
        return {"skipped": True, "reason": "missing_admin_bearer_or_jwt_secret"}
    imported_total = 0
    responses: list[dict[str, Any]] = []
    for _ in range(args.import_loops):
        response = requests.post(
            f"{args.live_base_url}/admin/ingest-ready-gcs",
            params={"state": STATE, "limit": 50},
            headers={"Authorization": f"Bearer {token}"},
            timeout=900,
        )
        record: dict[str, Any] = {"status_code": response.status_code}
        try:
            record["body"] = response.json()
        except Exception:
            record["body"] = response.text[:1000]
        responses.append(record)
        if response.status_code >= 400:
            break
        body    = record.get("body") if isinstance(record.get("body"), dict) else {}
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list) or not results:
            break
        imported_now = sum(
            1 for r in results if (r.get("status") or "").lower() == "imported"
        )
        imported_total += imported_now
        if not imported_now and not body.get("found"):
            break
    write_json(
        run_dir / "live_import_report.json",
        {"total_imported": imported_total, "responses": responses},
    )
    return {"total_imported": imported_total, "responses": responses}


def verify_live(args: argparse.Namespace, run_dir: Path, suffix: str = "") -> dict[str, Any]:
    report: dict[str, Any] = {"state": STATE}
    try:
        summary = requests.get(
            f"{args.live_base_url}/hoas/summary",
            params={"state": STATE},
            timeout=60,
        )
        report["summary_status"] = summary.status_code
        report["summary"] = (
            summary.json()
            if summary.headers.get("content-type", "").startswith("application/json")
            else summary.text[:1000]
        )
    except Exception as exc:
        report["summary_error"] = f"{type(exc).__name__}: {exc}"
    try:
        points = requests.get(
            f"{args.live_base_url}/hoas/map-points",
            params={"state": STATE},
            timeout=60,
        )
        report["map_status"] = points.status_code
        data = (
            points.json()
            if points.headers.get("content-type", "").startswith("application/json")
            else []
        )
        report["map_points"] = len(data) if isinstance(data, list) else None
        if isinstance(data, list):
            report["out_of_state_points"] = [
                p
                for p in data
                if isinstance(p, dict)
                and p.get("latitude") is not None
                and p.get("longitude") is not None
                and not (
                    STATE_BBOX["min_lat"] <= float(p["latitude"]) <= STATE_BBOX["max_lat"]
                    and STATE_BBOX["min_lon"] <= float(p["longitude"]) <= STATE_BBOX["max_lon"]
                )
            ][:20]
    except Exception as exc:
        report["map_error"] = f"{type(exc).__name__}: {exc}"
    fname = f"live_verification{suffix}.json"
    write_json(run_dir / fname, report)
    return report


def run_location_enrichment(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    enrich_script = SCRIPTS_DIR / f"enrich_{STATE.lower()}_locations.py"
    if not enrich_script.exists():
        enrich_script = ROOT / "state_scrapers/ri/scripts/enrich_ri_locations.py"
    cmd = [
        str(ROOT / ".venv/bin/python"),
        str(enrich_script),
        "--base", args.live_base_url,
        "--zip-cache", str(run_dir / "zip_centroid_cache.json"),
        "--output", str(run_dir / "location_enrichment.jsonl"),
        "--skip-nominatim",
        "--state", STATE,
    ]
    if args.apply:
        cmd.append("--apply")
    return run_command(cmd, run_dir, "30_location_enrichment", apply=True)


def count_gcs(bucket: str, prefix: str, suffix: str) -> int:
    client = storage.Client()
    return sum(
        1 for blob in client.bucket(bucket).list_blobs(prefix=prefix)
        if blob.name.endswith(suffix)
    )


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    storage.Client()
    return {
        "state": STATE,
        "tier": "1",
        "discovery_mode": args.discovery_mode,
        "gcs_bank_ok": True,
        "prepared_bucket_ok": True,
        "docai_config_present": bool(
            os.environ.get("HOA_DOCAI_PROJECT_ID")
            and os.environ.get("HOA_DOCAI_PROCESSOR_ID")
        ),
        "serper_ok": bool(os.environ.get("SERPER_API_KEY")),
        "render_admin_ok": bool(
            os.environ.get("HOAPROXY_ADMIN_BEARER")
            or os.environ.get("JWT_SECRET")
            or (os.environ.get("RENDER_API_KEY") and os.environ.get("RENDER_SERVICE_ID"))
        ),
        "max_ocr_budget_usd": args.max_docai_cost_usd,
        "county_runs": [{"queries": q, "default_county": c} for q, c in COUNTY_RUNS],
    }


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=now_id())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--discovery-mode",
        choices=["keyword-serper", "sos-first", "manual-leads"],
        default="keyword-serper",
    )
    parser.add_argument("--max-docai-cost-usd", type=float, default=10.0)
    parser.add_argument("--bank-bucket", default=BANK_BUCKET)
    parser.add_argument("--prepared-bucket", default=PREPARED_BUCKET)
    parser.add_argument(
        "--live-base-url",
        default=os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org"),
    )
    parser.add_argument("--max-queries-per-county", type=int, default=30)
    parser.add_argument("--results-per-query", type=int, default=10)
    parser.add_argument("--max-leads-per-county", type=int, default=80)
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--skip-prepare", action="store_true")
    parser.add_argument("--skip-import", action="store_true")
    parser.add_argument("--skip-locations", action="store_true")
    parser.add_argument("--counties-only")
    parser.add_argument("--import-loops", type=int, default=80)
    args = parser.parse_args()

    run_dir = ROOT / f"state_scrapers/{STATE.lower()}/results" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "state": STATE,
        "run_id": args.run_id,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "apply": args.apply,
        "discovery_mode": args.discovery_mode,
    }

    report["preflight"] = preflight(args)
    write_json(run_dir / "preflight.json", report["preflight"])

    if not args.skip_discovery:
        if args.discovery_mode == "keyword-serper":
            county_results = []
            only = (
                set(c.strip() for c in args.counties_only.split(","))
                if args.counties_only
                else None
            )
            for idx, (queries_file, default_county) in enumerate(COUNTY_RUNS):
                if only and (default_county or "") not in only:
                    continue
                res = run_county_serper(args, run_dir, queries_file, default_county, idx)
                county_results.append(
                    {"queries": queries_file, "default_county": default_county, **res}
                )
            report["discovery"] = county_results

    if not args.skip_prepare:
        report["prepare"] = run_prepare(args, run_dir)

    if args.apply and not args.skip_import:
        report["import"] = import_ready(args, run_dir)
        report["live"]   = verify_live(args, run_dir)
        if not args.skip_locations:
            report["location_enrichment"]  = run_location_enrichment(args, run_dir)
            report["live_after_location"]  = verify_live(args, run_dir, suffix="_post_location")

    report["raw_manifests"]    = count_gcs(args.bank_bucket, f"v1/{STATE}/", "manifest.json")
    report["prepared_bundles"] = count_gcs(args.prepared_bucket, f"v1/{STATE}/", "bundle.json")
    report["finished_at"]      = datetime.now(timezone.utc).isoformat(timespec="seconds")

    write_json(run_dir / "final_state_report.json", report)
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                **{k: report[k] for k in ("raw_manifests", "prepared_bundles")},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
