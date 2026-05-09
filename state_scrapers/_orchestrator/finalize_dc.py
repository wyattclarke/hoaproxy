#!/usr/bin/env python3
"""DC finalizer — run after both:
  1. Main orchestrator finished (status.json marks UT completed or halted)
  2. DC CAMA pipeline produced its sentinel

Re-runs prepare + import + Phase 10 for DC against the bank, which by now
contains both the original neighborhood-anchored sweep AND the CAMA name-list
sweep manifests (deduped by slug). Idempotent — bundles are skipped if
already imported.

Will WAIT (poll every 5 min) for both prerequisites before running, so it can
be launched at any time.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]

ORCH_STATUS = ROOT / "state_scrapers/_orchestrator/status.json"
CAMA_SENTINEL = ROOT / "state_scrapers/_orchestrator/dc_cama_pipeline_DONE.flag"
LOG_PATH = ROOT / "state_scrapers/_orchestrator/finalize_dc.log"

DC_BBOX = {"min_lat": 38.79, "max_lat": 39.00, "min_lon": -77.12, "max_lon": -76.91}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def orchestrator_done() -> bool:
    if not ORCH_STATUS.exists():
        return False
    try:
        s = json.loads(ORCH_STATUS.read_text(encoding="utf-8"))
    except Exception:
        return False
    if s.get("halted") or s.get("completed"):
        return True
    states = s.get("states") or {}
    ut = states.get("UT") or {}
    return bool(ut.get("completed"))


def cama_done() -> bool:
    return CAMA_SENTINEL.exists()


def run(cmd: list[str], log_file: Path, timeout: int = 4 * 3600) -> int:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"\n[{now_iso()}] $ {' '.join(cmd)}\n")
        f.flush()
        try:
            proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
            return proc.returncode
        except subprocess.TimeoutExpired:
            f.write(f"[TIMEOUT after {timeout}s]\n")
            return 124


def live_admin_token() -> str | None:
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


def import_loop(token: str, base_url: str, log_file: Path) -> dict:
    total = 0
    iterations = 0
    last_responses = []
    for _ in range(200):  # generous cap; each call imports up to 50 bundles
        iterations += 1
        try:
            r = requests.post(
                f"{base_url}/admin/ingest-ready-gcs",
                params={"state": "DC", "limit": 50},
                headers={"Authorization": f"Bearer {token}"},
                timeout=900,
            )
            with log_file.open("a", encoding="utf-8") as f:
                f.write(f"[{now_iso()}] /admin/ingest-ready-gcs?state=DC iter={iterations} status={r.status_code}\n")
            if r.status_code >= 400:
                last_responses.append({"status": r.status_code, "body": r.text[:500]})
                break
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            results = body.get("results") if isinstance(body, dict) else None
            if not isinstance(results, list) or not results:
                break
            imported_now = sum(1 for x in results if (x.get("status") or "").lower() == "imported")
            total += imported_now
            with log_file.open("a", encoding="utf-8") as f:
                f.write(f"  imported_now={imported_now} (cumulative={total})\n")
            if not imported_now and not body.get("found"):
                break
        except Exception as exc:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(f"  exception: {type(exc).__name__}: {exc}\n")
            last_responses.append({"error": f"{type(exc).__name__}: {exc}"})
            break
    return {"total_imported": total, "iterations": iterations, "tail": last_responses}


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)

    log("=== DC finalizer waiting for orchestrator + CAMA pipeline to finish ===")
    waited = 0
    while True:
        oc = orchestrator_done()
        cc = cama_done()
        log(f"  orchestrator_done={oc}  cama_done={cc}  waited={waited}min")
        if oc and cc:
            break
        time.sleep(300)
        waited += 5
        if waited > 24 * 60:  # 24h hard timeout
            log("Timed out waiting; bailing.")
            return 2

    run_id = f"dc_finalize_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    run_dir = ROOT / f"state_scrapers/dc/results/{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log(f"=== DC finalizer running run_id={run_id} ===")

    base_url = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")

    # Step 1: prepare bundles (DocAI cap $25 — bigger than original $10 since CAMA list doubled coverage)
    prepare_log = run_dir / "20_prepare.log"
    cmd_prep = [
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "scripts/prepare_bank_for_ingest.py"),
        "--state", "DC",
        "--limit", "10000",
        "--max-docai-cost-usd", "25",
        "--ledger", str(run_dir / "prepared_ingest_ledger.jsonl"),
        "--geo-cache", str(run_dir / "prepared_ingest_geo_cache.json"),
        "--bank-bucket", "hoaproxy-bank",
        "--prepared-bucket", "hoaproxy-ingest-ready",
    ]
    rc_prep = run(cmd_prep, prepare_log)
    log(f"prepare rc={rc_prep}")

    # Step 2: import loop
    token = live_admin_token()
    if not token:
        log("FATAL: no admin token for import")
        return 3
    import_log = run_dir / "30_import.log"
    res_imp = import_loop(token, base_url, import_log)
    log(f"import: {res_imp}")
    (run_dir / "live_import_report.json").write_text(json.dumps(res_imp, indent=2, sort_keys=True), encoding="utf-8")

    # Step 3: location enrichment (extract-doc-zips + ZIP centroid)
    enrich_log = run_dir / "40_enrich.log"
    cmd_enrich = [
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "state_scrapers/ri/scripts/enrich_ri_locations.py"),
        "--state", "DC",
        "--zip-cache", str(run_dir / "zip_cache.json"),
        "--output", str(run_dir / "location_enrichment.jsonl"),
        "--apply",
        "--skip-nominatim",
    ]
    rc_enrich = run(cmd_enrich, enrich_log)
    log(f"enrich rc={rc_enrich}")

    # Step 4: Phase 10 close
    phase10_log = run_dir / "50_phase10.log"
    cmd_p10 = [
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "scripts/phase10_close.py"),
        "--state", "DC",
        "--bbox-json", json.dumps(DC_BBOX, sort_keys=True),
        "--run-id", run_id,
        "--base-url", base_url,
        "--apply",
    ]
    rc_p10 = run(cmd_p10, phase10_log)
    log(f"phase10 rc={rc_p10}")

    # Final live count
    try:
        r = requests.get(f"{base_url}/hoas/summary", params={"state": "DC"}, timeout=60)
        body = r.json() if r.status_code == 200 else {}
        live_total = body.get("total") if isinstance(body, dict) else None
        log(f"FINAL DC live count: {live_total}")
    except Exception as exc:
        log(f"summary fetch failed: {type(exc).__name__}: {exc}")

    log("=== DC finalizer DONE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
