#!/usr/bin/env python3
"""Wait for DC + HI namelist_discover.py runs to finish, then drive both
states through prepare + import + enrich + Phase 10 sequentially.

Reads PID files at:
  state_scrapers/_orchestrator/dc_namelist.pid
  state_scrapers/_orchestrator/hi_namelist.pid
And run_id files at:
  state_scrapers/_orchestrator/dc_namelist.run_id
  state_scrapers/_orchestrator/hi_namelist.run_id

Polls every 5 minutes until both PIDs have exited, then proceeds with
DC sequence (prepare → import loop → ZIP enrichment → Phase 10), then HI.

Designed to run autonomously via nohup.
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
ORCH = ROOT / "state_scrapers/_orchestrator"
LOG_PATH = ORCH / "finalize_dc_hi_namelist.log"

DC_BBOX = {"min_lat": 38.79, "max_lat": 39.00, "min_lon": -77.12, "max_lon": -76.91}
HI_BBOX = {"min_lat": 18.86, "max_lat": 22.24, "min_lon": -160.27, "max_lon": -154.75}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_pid(name: str) -> int | None:
    p = ORCH / f"{name}.pid"
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip().split("=", 1)[-1])
    except Exception:
        return None


def read_run_id(name: str) -> str | None:
    p = ORCH / f"{name}.run_id"
    if not p.exists():
        return None
    try:
        return p.read_text().strip().split("=", 1)[-1]
    except Exception:
        return None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def live_admin_token() -> str | None:
    # Explicit override wins; otherwise pull from settings.env.
    # Render env-vars fallback removed 2026-05-16 (Hetzner cutover).
    return os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")


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


def import_loop(state: str, base_url: str, token: str, log_file: Path) -> dict:
    total = 0
    iterations = 0
    for _ in range(200):
        iterations += 1
        try:
            r = requests.post(
                f"{base_url}/admin/ingest-ready-gcs",
                params={"state": state, "limit": 50},
                headers={"Authorization": f"Bearer {token}"},
                timeout=900,
            )
            with log_file.open("a", encoding="utf-8") as f:
                f.write(f"[{now_iso()}] /admin/ingest-ready-gcs?state={state} iter={iterations} status={r.status_code}\n")
            if r.status_code >= 400:
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
            break
    return {"total_imported": total, "iterations": iterations}


def fetch_live_count(state: str, base_url: str) -> int | None:
    try:
        r = requests.get(f"{base_url}/hoas/summary", params={"state": state, "limit": 5000}, timeout=60)
        if r.status_code != 200:
            return None
        body = r.json()
        if isinstance(body, dict):
            if isinstance(body.get("total"), int):
                return body["total"]
            results = body.get("results")
            if isinstance(results, list):
                return len(results)
    except Exception:
        pass
    return None


def drive_state(state: str, run_id: str, bbox: dict, base_url: str, token: str, max_docai_usd: int) -> dict:
    state_lower = state.lower()
    run_dir = ROOT / f"state_scrapers/{state_lower}/results/{run_id}_finalize"
    run_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {"state": state, "run_id": run_id}

    # 1. Prepare
    log(f"{state}: prepare bundles (DocAI cap ${max_docai_usd})")
    prep_log = run_dir / "20_prepare.log"
    rc_prep = run([
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "scripts/prepare_bank_for_ingest.py"),
        "--state", state,
        "--limit", "10000",
        "--max-docai-cost-usd", str(max_docai_usd),
        "--ledger", str(run_dir / "prepared_ingest_ledger.jsonl"),
        "--geo-cache", str(run_dir / "prepared_ingest_geo_cache.json"),
        "--bank-bucket", "hoaproxy-bank",
        "--prepared-bucket", "hoaproxy-ingest-ready",
    ], prep_log, timeout=4 * 3600)
    result["prepare_rc"] = rc_prep
    log(f"{state}: prepare rc={rc_prep}")

    # 2. Import loop
    log(f"{state}: import loop")
    imp_log = run_dir / "30_import.log"
    res_imp = import_loop(state, base_url, token, imp_log)
    result["import"] = res_imp
    (run_dir / "live_import_report.json").write_text(json.dumps(res_imp, indent=2, sort_keys=True), encoding="utf-8")
    log(f"{state}: imported {res_imp['total_imported']} bundles")

    # 3. Location enrichment
    log(f"{state}: location enrichment")
    enr_log = run_dir / "40_enrich.log"
    rc_enr = run([
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "state_scrapers/ri/scripts/enrich_ri_locations.py"),
        "--state", state,
        "--zip-cache", str(run_dir / "zip_cache.json"),
        "--output", str(run_dir / "location_enrichment.jsonl"),
        "--apply", "--skip-nominatim",
    ], enr_log, timeout=2 * 3600)
    result["enrich_rc"] = rc_enr

    # 4. Phase 10 close
    log(f"{state}: Phase 10 close")
    p10_log = run_dir / "50_phase10.log"
    rc_p10 = run([
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "scripts/phase10_close.py"),
        "--state", state,
        "--bbox-json", json.dumps(bbox, sort_keys=True),
        "--run-id", f"{run_id}_finalize",
        "--base-url", base_url,
        "--apply",
    ], p10_log, timeout=2 * 3600)
    result["phase10_rc"] = rc_p10

    # 5. Final live count
    result["final_live_count"] = fetch_live_count(state, base_url)
    log(f"{state}: FINAL live count = {result['final_live_count']}")
    return result


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)

    log("=== DC + HI namelist finalizer waiting for discovery PIDs to exit ===")
    waited = 0
    while True:
        dc_alive = pid_alive(read_pid("dc_namelist"))
        hi_alive = pid_alive(read_pid("hi_namelist"))
        log(f"  dc_alive={dc_alive} hi_alive={hi_alive} waited={waited}min")
        if not dc_alive and not hi_alive:
            break
        time.sleep(300)
        waited += 5
        if waited > 12 * 60:
            log("Timed out waiting (>12h); proceeding anyway.")
            break

    base_url = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")
    token = live_admin_token()
    if not token:
        log("FATAL: no admin token")
        return 2

    dc_run_id = read_run_id("dc_namelist") or "dc_namelist_unknown"
    hi_run_id = read_run_id("hi_namelist") or "hi_namelist_unknown"

    summary: dict = {"started_at": now_iso()}

    # DC first
    log("=== DC drive: prepare → import → enrich → Phase 10 ===")
    summary["dc"] = drive_state("DC", dc_run_id, DC_BBOX, base_url, token, max_docai_usd=25)

    # HI second (sequential to avoid sqlite write contention)
    log("=== HI drive: prepare → import → enrich → Phase 10 ===")
    summary["hi"] = drive_state("HI", hi_run_id, HI_BBOX, base_url, token, max_docai_usd=25)

    summary["finished_at"] = now_iso()
    out_path = ORCH / "finalize_dc_hi_namelist_summary.json"
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    log(f"=== Finalizer DONE — summary at {out_path} ===")
    log(f"  DC final live: {summary['dc'].get('final_live_count')}")
    log(f"  HI final live: {summary['hi'].get('final_live_count')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
