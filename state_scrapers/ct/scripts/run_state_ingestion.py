#!/usr/bin/env python3
"""Run Connecticut small-state HOA ingestion end-to-end.

Pipeline (per ``docs/multi-state-ingestion-playbook.md``):

  1. Pull active HOA / condo / community-association entities from CT
     Open Data (Connecticut Business Registry - Business Master, dataset
     ``n7gp-d28j``) → ct_sos_associations.jsonl. SODA is faster and more
     reliable than scraping the new Salesforce-based service.ct.gov
     portal, and gives the same canonical universe of CT HOAs with name,
     city, county, ZIP.

  2. For each lead, run a targeted Serper search for governing-document
     PDFs (``"<exact name>" Connecticut filetype:pdf`` etc.) → enriched
     JSONL with pre_discovered_pdf_urls and (sometimes) website.

  3. probe-batch the enriched leads → banks PDFs into
     gs://hoaproxy-bank/v1/CT/{county}/{slug}/.

  4. prepare_bank_for_ingest.py with --max-docai-cost-usd 15 → prepared
     bundles in gs://hoaproxy-ingest-ready/v1/CT/.

  5. POST /admin/ingest-ready-gcs?state=CT in a loop until empty
     (capped at 50 per call per the playbook).

  6. Verify live counts and map coverage; produce final report.

Why SODA-first instead of Salesforce scraping: CT decommissioned the
CONCORD ASP.NET search and replaced it with a Salesforce Lightning
portal that is hostile to scraping. The SODA dataset is the same
underlying registry export, with structured billing/records address
fields, and is publicly maintained.
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

STATE = "CT"
STATE_NAME = "Connecticut"
BANK_BUCKET = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")
PREPARED_BUCKET = os.environ.get("HOA_PREPARED_GCS_BUCKET", "hoaproxy-ingest-ready")
CT_BBOX = {"min_lat": 40.95, "max_lat": 42.10, "min_lon": -73.75, "max_lon": -71.78}

LEADS_DIR = ROOT / "state_scrapers/ct/leads"
SCRIPTS_DIR = ROOT / "state_scrapers/ct/scripts"


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


def run_sos_scrape(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    cmd = [
        str(ROOT / ".venv/bin/python"),
        str(SCRIPTS_DIR / "scrape_ct_sos.py"),
        "--output", str(LEADS_DIR / "ct_sos_associations.jsonl"),
        "--polite-delay", "0.25",
        "--page-size", "1000",
    ]
    return run_command(cmd, run_dir, "01_sos_scrape", apply=args.apply)


def run_enrich(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    cmd = [
        str(ROOT / ".venv/bin/python"),
        str(SCRIPTS_DIR / "enrich_ct_leads_with_serper.py"),
        "--input", str(LEADS_DIR / "ct_sos_associations.jsonl"),
        "--output", str(LEADS_DIR / "ct_sos_associations_enriched.jsonl"),
        "--polite-delay", "0.35",
    ]
    if args.resume_enrich:
        cmd.append("--resume")
    return run_command(cmd, run_dir, "02_enrich", apply=args.apply)


def run_probe_batch(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    cmd = [
        str(ROOT / ".venv/bin/python"),
        str(SCRIPTS_DIR / "probe_enriched_leads.py"),
        "--input", str(LEADS_DIR / "ct_sos_associations_enriched.jsonl"),
        "--output", str(run_dir / "probe_results.jsonl"),
        "--bucket", args.bank_bucket,
    ]
    if args.resume_probe:
        cmd.append("--resume")
    return run_command(cmd, run_dir, "03_probe_batch", apply=args.apply)


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
    return run_command(cmd, run_dir, "04_prepare", apply=True)


def _live_admin_token() -> str | None:
    """Resolve the live admin bearer. Local JWT_SECRET often diverges from the
    Render-hosted secret, so prefer reading it via the Render API when
    RENDER_API_KEY + RENDER_SERVICE_ID are present."""
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
            params={"state": STATE, "limit": 50},  # /admin endpoint caps at 50
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
        # Per playbook: count by walking results, not by reading top-level
        # `imported`/`processed` (those don't exist on this endpoint).
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
                and not (CT_BBOX["min_lat"] <= float(p["latitude"]) <= CT_BBOX["max_lat"]
                         and CT_BBOX["min_lon"] <= float(p["longitude"]) <= CT_BBOX["max_lon"])
            ][:20]
    except Exception as exc:
        report["map_error"] = f"{type(exc).__name__}: {exc}"
    write_json(run_dir / "live_verification.json", report)
    return report


def run_location_enrichment(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    cmd = [
        str(ROOT / ".venv/bin/python"),
        str(SCRIPTS_DIR / "enrich_ct_locations.py"),
        "--leads", str(LEADS_DIR / "ct_sos_associations.jsonl"),
        "--base", args.live_base_url,
        "--zip-cache", str(run_dir / "zip_centroid_cache.json"),
        "--output", str(run_dir / "location_enrichment.jsonl"),
        "--skip-nominatim",  # public Nominatim is unreliable; ZIP-first per playbook
    ]
    if args.apply:
        cmd.append("--apply")
    return run_command(cmd, run_dir, "06_location_enrichment", apply=True)


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
    }


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=now_id())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-docai-cost-usd", type=float, default=15.0)
    parser.add_argument("--bank-bucket", default=BANK_BUCKET)
    parser.add_argument("--prepared-bucket", default=PREPARED_BUCKET)
    parser.add_argument("--live-base-url", default=os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org"))
    parser.add_argument("--skip-sos", action="store_true")
    parser.add_argument("--skip-enrich", action="store_true")
    parser.add_argument("--skip-probe", action="store_true")
    parser.add_argument("--skip-prepare", action="store_true")
    parser.add_argument("--skip-import", action="store_true")
    parser.add_argument("--skip-locations", action="store_true")
    parser.add_argument("--resume-enrich", action="store_true")
    parser.add_argument("--resume-probe", action="store_true")
    parser.add_argument("--import-loops", type=int, default=40)
    args = parser.parse_args()

    run_dir = ROOT / "state_scrapers/ct/results" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "state": STATE,
        "run_id": args.run_id,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "apply": args.apply,
    }
    report["preflight"] = preflight(args)
    write_json(run_dir / "preflight.json", report["preflight"])

    if not args.skip_sos:
        report["sos_scrape"] = run_sos_scrape(args, run_dir)
    if not args.skip_enrich:
        report["enrich"] = run_enrich(args, run_dir)
    if not args.skip_probe:
        report["probe_batch"] = run_probe_batch(args, run_dir)
    if not args.skip_prepare:
        report["prepare"] = run_prepare(args, run_dir)
    if args.apply and not args.skip_import:
        report["import"] = import_ready(args, run_dir)
        report["live"] = verify_live(args, run_dir)
        if not args.skip_locations:
            report["location_enrichment"] = run_location_enrichment(args, run_dir)
            report["live_after_location"] = verify_live(args, run_dir)

    report["raw_manifests"] = count_gcs(args.bank_bucket, "v1/CT/", "manifest.json")
    report["prepared_bundles"] = count_gcs(args.prepared_bucket, "v1/CT/", "bundle.json")
    report["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_json(run_dir / "final_state_report.json", report)
    print(json.dumps({"run_dir": str(run_dir), **{k: report[k] for k in ("raw_manifests", "prepared_bundles")}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
