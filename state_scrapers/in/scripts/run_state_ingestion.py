#!/usr/bin/env python3
"""Run Indiana HOA ingestion end-to-end.

Pipeline (per ``docs/small-state-end-to-end-ingestion-plan.md``):

  1. For each per-county queries file, run benchmark/scrape_state_serper_docpages.py
     with ``--probe`` so leads are banked under
     ``gs://hoaproxy-bank/v1/IN/{county}/{slug}/`` immediately.
     Indiana's INBiz public business search is reCAPTCHA-gated for
     non-whitelisted IPs (and the bulk download is $9,500), so we don't have
     a free SoS-registry universe like CT/RI. Per-county Serper is the
     KS/TN-style pattern recommended for medium-sized states with moderate
     name-overlap.

  2. prepare_bank_for_ingest.py with --max-docai-cost-usd 50 → prepared
     bundles in gs://hoaproxy-ingest-ready/v1/IN/.

  3. POST /admin/ingest-ready-gcs?state=IN in a loop until empty (capped
     at 50 per call per the playbook).

  4. enrich_in_locations.py → ZIP-centroid backfill via zippopotam.us
     (Nominatim is opportunistic only — public instance rate-limits hard
     after ~100 sequential requests).

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

STATE = "IN"
STATE_NAME = "Indiana"
BANK_BUCKET = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")
PREPARED_BUCKET = os.environ.get("HOA_PREPARED_GCS_BUCKET", "hoaproxy-ingest-ready")
IN_BBOX = {"min_lat": 37.70, "max_lat": 41.80, "min_lon": -88.10, "max_lon": -84.70}

QUERIES_DIR = ROOT / "state_scrapers/in/queries"
SCRIPTS_DIR = ROOT / "state_scrapers/in/scripts"


# (queries-file, default-county). Order = priority for budget. Top metros
# first so OCR spend lands on highest-density counties before falling
# through to smaller ones.
COUNTY_RUNS: list[tuple[str, str | None]] = [
    ("in_hamilton_serper_queries.txt", "Hamilton"),
    ("in_marion_serper_queries.txt", "Marion"),
    ("in_hendricks_serper_queries.txt", "Hendricks"),
    ("in_johnson_serper_queries.txt", "Johnson"),
    ("in_boone_serper_queries.txt", "Boone"),
    ("in_hancock_serper_queries.txt", "Hancock"),
    ("in_lake_serper_queries.txt", "Lake"),
    ("in_porter_serper_queries.txt", "Porter"),
    ("in_allen_serper_queries.txt", "Allen"),
    ("in_st_joseph_serper_queries.txt", "St. Joseph"),
    ("in_elkhart_serper_queries.txt", "Elkhart"),
    ("in_tippecanoe_serper_queries.txt", "Tippecanoe"),
    ("in_madison_serper_queries.txt", "Madison"),
    ("in_vanderburgh_serper_queries.txt", "Vanderburgh"),
    ("in_monroe_serper_queries.txt", "Monroe"),
    ("in_clark_serper_queries.txt", "Clark"),
    ("in_morgan_serper_queries.txt", "Morgan"),
    ("in_warrick_serper_queries.txt", "Warrick"),
    # The "other_metros" queries cover ~40 mid/small counties with fewer queries
    # each. Lead.county is left None so the bank slug falls through to the
    # county hint Serper extracts from the URL/title (or _unknown-county/),
    # which prepare can repair from OCR text.
    ("in_other_metros_serper_queries.txt", None),
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


def run_county_serper(args: argparse.Namespace, run_dir: Path, queries_file: str, default_county: str | None, idx: int) -> dict[str, Any]:
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
    ledger = run_dir / "prepared_ingest_ledger.jsonl"
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
    api_key = os.environ.get("RENDER_API_KEY")
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
        body = record.get("body") if isinstance(record.get("body"), dict) else {}
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list) or not results:
            break
        imported_now = sum(1 for r in results if (r.get("status") or "").lower() == "imported")
        imported_total += imported_now
        if not imported_now and not body.get("found"):
            break
    write_json(run_dir / "live_import_report.json", {"total_imported": imported_total, "responses": responses})
    return {"total_imported": imported_total, "responses": responses}


def verify_live(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    report: dict[str, Any] = {"state": STATE}
    try:
        summary = requests.get(f"{args.live_base_url}/hoas/summary", params={"state": STATE}, timeout=60)
        report["summary_status"] = summary.status_code
        report["summary"] = summary.json() if summary.headers.get("content-type", "").startswith("application/json") else summary.text[:1000]
    except Exception as exc:
        report["summary_error"] = f"{type(exc).__name__}: {exc}"
    try:
        points = requests.get(f"{args.live_base_url}/hoas/map-points", params={"state": STATE}, timeout=60)
        report["map_status"] = points.status_code
        data = points.json() if points.headers.get("content-type", "").startswith("application/json") else []
        report["map_points"] = len(data) if isinstance(data, list) else None
        if isinstance(data, list):
            report["out_of_state_points"] = [
                p for p in data if isinstance(p, dict)
                and p.get("latitude") is not None and p.get("longitude") is not None
                and not (IN_BBOX["min_lat"] <= float(p["latitude"]) <= IN_BBOX["max_lat"]
                         and IN_BBOX["min_lon"] <= float(p["longitude"]) <= IN_BBOX["max_lon"])
            ][:20]
    except Exception as exc:
        report["map_error"] = f"{type(exc).__name__}: {exc}"
    write_json(run_dir / "live_verification.json", report)
    return report


def run_location_enrichment(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    cmd = [
        str(ROOT / ".venv/bin/python"),
        str(SCRIPTS_DIR / "enrich_in_locations.py"),
        "--base", args.live_base_url,
        "--zip-cache", str(run_dir / "zip_centroid_cache.json"),
        "--output", str(run_dir / "location_enrichment.jsonl"),
        "--skip-nominatim",
    ]
    if args.apply:
        cmd.append("--apply")
    return run_command(cmd, run_dir, "30_location_enrichment", apply=True)


def count_gcs(prefix_bucket: str, prefix: str, suffix: str) -> int:
    client = storage.Client()
    return sum(1 for blob in client.bucket(prefix_bucket).list_blobs(prefix=prefix) if blob.name.endswith(suffix))


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    storage.Client()
    return {
        "state": STATE,
        "gcs_bank_ok": True,
        "prepared_bucket_ok": True,
        "docai_config_present": bool(os.environ.get("HOA_DOCAI_PROJECT_ID") and os.environ.get("HOA_DOCAI_PROCESSOR_ID")),
        "serper_ok": bool(os.environ.get("SERPER_API_KEY")),
        "render_admin_ok": bool(os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")
                                or (os.environ.get("RENDER_API_KEY") and os.environ.get("RENDER_SERVICE_ID"))),
        "max_ocr_budget_usd": args.max_docai_cost_usd,
        "county_runs": [{"queries": q, "default_county": c} for q, c in COUNTY_RUNS],
    }


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=now_id())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-docai-cost-usd", type=float, default=50.0)
    parser.add_argument("--bank-bucket", default=BANK_BUCKET)
    parser.add_argument("--prepared-bucket", default=PREPARED_BUCKET)
    parser.add_argument("--live-base-url", default=os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org"))
    parser.add_argument("--max-queries-per-county", type=int, default=30)
    parser.add_argument("--results-per-query", type=int, default=10)
    parser.add_argument("--max-leads-per-county", type=int, default=80)
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--skip-prepare", action="store_true")
    parser.add_argument("--skip-import", action="store_true")
    parser.add_argument("--skip-locations", action="store_true")
    parser.add_argument("--counties-only", help="Comma-separated subset of counties to run (matches default_county exactly)")
    parser.add_argument("--import-loops", type=int, default=80)
    args = parser.parse_args()

    run_dir = ROOT / "state_scrapers/in/results" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "state": STATE,
        "run_id": args.run_id,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "apply": args.apply,
    }
    report["preflight"] = preflight(args)
    write_json(run_dir / "preflight.json", report["preflight"])

    if not args.skip_discovery:
        county_results = []
        only = set(c.strip() for c in args.counties_only.split(",")) if args.counties_only else None
        for idx, (queries_file, default_county) in enumerate(COUNTY_RUNS):
            if only and (default_county or "") not in only:
                continue
            res = run_county_serper(args, run_dir, queries_file, default_county, idx)
            county_results.append({"queries": queries_file, "default_county": default_county, **res})
        report["discovery"] = county_results

    if not args.skip_prepare:
        report["prepare"] = run_prepare(args, run_dir)

    if args.apply and not args.skip_import:
        report["import"] = import_ready(args, run_dir)
        report["live"] = verify_live(args, run_dir)
        if not args.skip_locations:
            report["location_enrichment"] = run_location_enrichment(args, run_dir)
            report["live_after_location"] = verify_live(args, run_dir)

    report["raw_manifests"] = count_gcs(args.bank_bucket, "v1/IN/", "manifest.json")
    report["prepared_bundles"] = count_gcs(args.prepared_bucket, "v1/IN/", "bundle.json")
    report["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_json(run_dir / "final_state_report.json", report)
    print(json.dumps({"run_dir": str(run_dir), **{k: report[k] for k in ("raw_manifests", "prepared_bundles")}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
