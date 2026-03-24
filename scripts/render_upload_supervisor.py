#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "render_resume_upload.py"
STATE_PATH = ROOT / "data" / "render_upload_state.json"

REQUEST_TIMEOUT = (20, 120)
RUN_TIMEOUT_S = 45 * 60
RESTART_SLEEP_S = 10
LOG_PREFIX = "[render-supervisor]"


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def snapshot(base_url: str, s: requests.Session) -> tuple[int, int, int]:
    r = s.get(f"{base_url}/hoas", timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    hoas: list[str] = r.json()

    completed = 0
    for hoa in hoas:
        d = s.get(f"{base_url}/hoas/{quote(hoa, safe='')}/documents", timeout=REQUEST_TIMEOUT)
        d.raise_for_status()
        if d.json():
            completed += 1
    pending = len(hoas) - completed
    return len(hoas), completed, pending


def clear_retry_blocks() -> bool:
    state = load_state()
    timeouts = state.get("timeouts", [])
    failed = state.get("failed_uploads", [])
    if not timeouts and not failed:
        return False
    state["timeouts"] = []
    state["failed_uploads"] = []
    save_state(state)
    return True


def main() -> None:
    cfg = dotenv_values(ROOT / "settings.env")
    base_url = cfg.get("RENDER_APP_URL", "https://hoaproxy-app.onrender.com").rstrip("/")
    s = requests.Session()

    h = s.get(f"{base_url}/healthz", timeout=REQUEST_TIMEOUT)
    h.raise_for_status()
    log(f"health={h.json()}")

    while True:
        total, completed, pending = snapshot(base_url, s)
        log(f"before-run total={total} completed={completed} pending={pending}")
        if pending == 0:
            log("all HOAs indexed; exiting")
            return

        started = time.time()
        proc = subprocess.Popen([sys.executable, str(RUNNER)], cwd=str(ROOT))
        timed_out = False
        try:
            rc = proc.wait(timeout=RUN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            rc = -9

        elapsed = int(time.time() - started)
        log(f"runner-finished rc={rc} timed_out={timed_out} elapsed_s={elapsed}")

        total2, completed2, pending2 = snapshot(base_url, s)
        log(f"after-run total={total2} completed={completed2} pending={pending2}")
        if pending2 == 0:
            log("all HOAs indexed; exiting")
            return

        if clear_retry_blocks():
            log("cleared timeout/failed lists to allow automatic retries")

        time.sleep(RESTART_SLEEP_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted")
