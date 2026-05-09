#!/usr/bin/env python3
"""Sequential 9-state overnight orchestrator.

Runs DC → HI → IA → ID → KY → AL → LA → NV → UT in strict sequence.
Per state:
  1. python state_scrapers/{state}/scripts/run_state_ingestion.py --apply
  2. python scripts/phase10_close.py --state X --apply

Halt-on-hard-failure conditions:
  - Cumulative DocAI cost > $400
  - Render admin auth failure (no JWT obtainable)
  - Runner exits with code != 0 AND has produced no live HOAs for that state
    (a non-zero exit with live HOAs is a soft stop — Phase 10 still runs).

All progress is checkpointed to state_scrapers/_orchestrator/status.json so a
restart can pick up from the next pending state.

Designed to be invoked via nohup so it survives the launching shell exiting.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Pulled out of scaffold_states.py so we don't import that module at runtime.
STATE_ORDER = ["DC", "HI", "IA", "ID", "KY", "AL", "LA", "NV", "UT"]

STATE_BBOXES = {
    "DC": {"min_lat": 38.79, "max_lat": 39.00, "min_lon": -77.12, "max_lon": -76.91},
    "HI": {"min_lat": 18.86, "max_lat": 22.24, "min_lon": -160.27, "max_lon": -154.75},
    "IA": {"min_lat": 40.36, "max_lat": 43.50, "min_lon": -96.64, "max_lon": -90.14},
    "ID": {"min_lat": 41.99, "max_lat": 49.00, "min_lon": -117.24, "max_lon": -111.04},
    "KY": {"min_lat": 36.49, "max_lat": 39.15, "min_lon": -89.57, "max_lon": -81.96},
    "AL": {"min_lat": 30.14, "max_lat": 35.01, "min_lon": -88.47, "max_lon": -84.89},
    "LA": {"min_lat": 28.93, "max_lat": 33.02, "min_lon": -94.04, "max_lon": -88.81},
    "NV": {"min_lat": 35.00, "max_lat": 42.00, "min_lon": -120.01, "max_lon": -114.04},
    "UT": {"min_lat": 36.99, "max_lat": 42.00, "min_lon": -114.05, "max_lon": -109.04},
}

CUMULATIVE_DOCAI_HARD_CAP = 400.0

ORCH_DIR = ROOT / "state_scrapers/_orchestrator"
STATUS_PATH = ORCH_DIR / "status.json"
LOG_PATH = ORCH_DIR / "overnight.log"
DEFAULT_BASE_URL = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def now_id(state_code: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{state_code.lower()}_{ts}_overnight"


def read_status() -> dict[str, Any]:
    if STATUS_PATH.exists():
        try:
            return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "states": {}}


def write_status(status: dict[str, Any]) -> None:
    ORCH_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")


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


def fetch_cumulative_docai(base_url: str) -> float | None:
    """GET /admin/costs and return all-time DocAI USD spent, if available."""
    token = live_admin_token()
    if not token:
        return None
    try:
        r = requests.get(
            f"{base_url}/admin/costs",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        if r.status_code != 200:
            return None
        body = r.json() if isinstance(r.json(), dict) else {}
        # Several possible shapes — try the most common
        total = body.get("total_usd") or body.get("docai_total_usd")
        if isinstance(total, (int, float)):
            return float(total)
        all_time = body.get("all_time")
        if isinstance(all_time, dict):
            for k in ("total_usd", "docai_usd", "usd"):
                if isinstance(all_time.get(k), (int, float)):
                    return float(all_time[k])
        return None
    except Exception:
        return None


def fetch_live_count(state: str, base_url: str) -> int | None:
    try:
        r = requests.get(f"{base_url}/hoas/summary", params={"state": state}, timeout=60)
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, dict):
            if isinstance(data.get("total"), int):
                return int(data["total"])
            results = data.get("results") if isinstance(data.get("results"), list) else None
            if results is not None:
                return len(results)
        if isinstance(data, list):
            return len(data)
    except Exception:
        pass
    return None


def run_subprocess(cmd: list[str], log_file: Path, timeout_s: int) -> int:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"\n\n[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] $ {' '.join(cmd)}\n")
        f.flush()
        try:
            proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=f, stderr=subprocess.STDOUT, timeout=timeout_s)
            return proc.returncode
        except subprocess.TimeoutExpired:
            f.write(f"\n[TIMEOUT after {timeout_s}s]\n")
            return 124


def run_state(state: str, base_url: str, *, apply: bool) -> dict[str, Any]:
    run_id = now_id(state)
    log(f"=== {state} START run_id={run_id} ===")
    state_log = ORCH_DIR / f"{state.lower()}_overnight.log"
    bbox = STATE_BBOXES[state]

    runner = ROOT / f"state_scrapers/{state.lower()}/scripts/run_state_ingestion.py"
    if not runner.exists():
        return {"state": state, "status": "error", "reason": "missing_runner", "run_id": run_id}

    # Phase 1-9 via state's runner.  Discovery + prepare + import + verify.
    cmd = [
        str(ROOT / ".venv/bin/python"),
        str(runner),
        "--run-id", run_id,
    ]
    if apply:
        cmd.append("--apply")
    # Per-state runner timeout: 12 hours wall clock.  Tier-0 states (DC) usually
    # finish in <2h, Tier-1 metros up to 8-10h.
    rc = run_subprocess(cmd, state_log, timeout_s=12 * 3600)

    # Live count check after runner
    pre_phase10_count = fetch_live_count(state, base_url)
    log(f"{state} runner rc={rc}, live count after runner: {pre_phase10_count}")

    # Phase 10 close.  Run regardless of runner rc so we still clean up dirty
    # names, even if the runner had partial failures.
    cmd10 = [
        str(ROOT / ".venv/bin/python"),
        str(ROOT / "scripts/phase10_close.py"),
        "--state", state,
        "--bbox-json", json.dumps(bbox, sort_keys=True),
        "--run-id", run_id,
        "--base-url", base_url,
    ]
    if apply:
        cmd10.append("--apply")
    rc10 = run_subprocess(cmd10, state_log, timeout_s=2 * 3600)
    final_count = fetch_live_count(state, base_url)

    # Read final_state_report.json (merged)
    final_report = ROOT / f"state_scrapers/{state.lower()}/results/{run_id}/final_state_report.json"
    final_payload = {}
    if final_report.exists():
        try:
            final_payload = json.loads(final_report.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Decide hard-failure:
    #   - Runner non-zero AND no live HOAs for the state ever — clear hard fail
    #   - Otherwise treat as soft (could be no-yield or partial)
    is_hard_failure = (rc != 0 and (final_count or 0) == 0 and (pre_phase10_count or 0) == 0)

    return {
        "state": state,
        "run_id": run_id,
        "runner_rc": rc,
        "phase10_rc": rc10,
        "pre_phase10_live_count": pre_phase10_count,
        "final_live_count": final_count,
        "hard_failure": is_hard_failure,
        "final_state_report_present": final_report.exists(),
        "raw_manifests": final_payload.get("raw_manifests"),
        "prepared_bundles": final_payload.get("prepared_bundles"),
    }


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)
    ORCH_DIR.mkdir(parents=True, exist_ok=True)

    base_url = DEFAULT_BASE_URL
    apply = os.environ.get("OVERNIGHT_DRY_RUN") not in ("1", "true", "yes")

    log(f"=== OVERNIGHT RUN STARTED apply={apply} states={STATE_ORDER} ===")

    # Preflight: admin auth
    token = live_admin_token()
    if not token:
        log("HARD FAILURE: cannot obtain live admin token (HOAPROXY_ADMIN_BEARER / JWT_SECRET / RENDER_API_KEY all unavailable)")
        write_status({"halted": True, "reason": "no_admin_token", "halted_at": datetime.now(timezone.utc).isoformat(timespec='seconds')})
        return 2
    log("Preflight: admin token obtained.")

    # Preflight: cumulative DocAI baseline
    baseline_docai = fetch_cumulative_docai(base_url)
    log(f"Preflight: cumulative DocAI baseline = ${baseline_docai}")

    status = read_status()
    status.setdefault("states", {})
    status["last_started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status["base_url"] = base_url
    status["docai_baseline_usd"] = baseline_docai
    write_status(status)

    for state in STATE_ORDER:
        existing = status["states"].get(state) or {}
        if existing.get("completed") and (existing.get("final_live_count") or 0) > 0:
            log(f"{state}: already completed with {existing['final_live_count']} live; skipping.")
            continue

        # DocAI safety stop
        cur_docai = fetch_cumulative_docai(base_url)
        if cur_docai is not None and cur_docai > CUMULATIVE_DOCAI_HARD_CAP:
            log(f"HARD HALT: cumulative DocAI ${cur_docai:.2f} > ${CUMULATIVE_DOCAI_HARD_CAP} cap; stopping orchestrator before {state}")
            status["halted"] = True
            status["halt_reason"] = f"docai_cap_{cur_docai:.2f}"
            status["halted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            write_status(status)
            return 3

        status["states"][state] = {"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        write_status(status)

        result = run_state(state, base_url, apply=apply)
        result["completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        result["completed"] = True
        result["docai_after_state"] = fetch_cumulative_docai(base_url)
        status["states"][state] = result
        write_status(status)

        log(f"=== {state} END runner_rc={result['runner_rc']} live={result['final_live_count']} ===")

        if result["hard_failure"]:
            log(f"HARD HALT: {state} produced no live HOAs and runner failed; stopping orchestrator")
            status["halted"] = True
            status["halt_reason"] = f"{state}_hard_failure"
            status["halted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            write_status(status)
            return 4

        # Tiny breathing room between states (let any sqlite WAL settle)
        time.sleep(15)

    status["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status["completed"] = True
    write_status(status)
    log("=== OVERNIGHT RUN COMPLETE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
