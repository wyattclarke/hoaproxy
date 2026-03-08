#!/usr/bin/env python3
from __future__ import annotations

import json
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "render_upload_state.json"
DOCS_ROOT = ROOT / "casnc_hoa_docs"
LOG_PREFIX = "[render-upload]"

REQUEST_TIMEOUT = (20, 120)
UPLOAD_TIMEOUT = (30, 1800)
POLL_EVERY_S = 20
POLL_TIMEOUT_S = 20 * 60
RETRY_COUNT = 5
MAX_HOA_ATTEMPTS = 4


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "completed": [],
            "timeouts": [],
            "failed_uploads": [],
        }
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "completed": [],
            "timeouts": [],
            "failed_uploads": [],
        }


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def with_retry(fn, label: str):
    last_err: Exception | None = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            log(f"{label} failed attempt={attempt}/{RETRY_COUNT}: {type(exc).__name__}: {exc}")
            time.sleep(min(2 * attempt, 10))
    if last_err is not None:
        raise last_err
    raise RuntimeError(f"{label} failed without exception")


def main() -> None:
    cfg = dotenv_values(ROOT / "settings.env")
    base = cfg.get("RENDER_APP_URL", "https://hoaware-app.onrender.com").rstrip("/")
    s = requests.Session()

    state = load_state()
    completed = set(state.get("completed", []))
    timeouts = set(state.get("timeouts", []))
    failed_uploads = set(state.get("failed_uploads", []))
    attempts = dict(state.get("attempts", {}))

    def get_json(path: str):
        def _do():
            r = s.get(f"{base}{path}", timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()

        return with_retry(_do, f"GET {path}")

    def get_docs(hoa: str):
        return get_json(f"/hoas/{quote(hoa, safe='')}/documents")

    health = get_json("/healthz")
    log(f"health={health}")

    while True:
        hoas = get_json("/hoas")
        pending: list[str] = []
        for hoa in hoas:
            if hoa in completed:
                continue
            try:
                docs = get_docs(hoa)
            except Exception:  # noqa: BLE001
                # Keep pending on read errors; retries happen next loop.
                pending.append(hoa)
                continue
            if docs:
                completed.add(hoa)
            else:
                pending.append(hoa)

        state["completed"] = sorted(completed)
        state["attempts"] = attempts
        save_state(state)
        log(
            f"scan total={len(hoas)} completed={len(completed)} pending={len(pending)} "
            f"timeouts={len(timeouts)} failed_uploads={len(failed_uploads)}"
        )

        if not pending:
            log("all HOAs indexed; exiting")
            return

        eligible = [h for h in pending if int(attempts.get(h, 0)) < MAX_HOA_ATTEMPTS]
        target = min(eligible, key=lambda h: int(attempts.get(h, 0)), default=None)
        if target is None:
            log(f"no eligible pending HOA (all reached {MAX_HOA_ATTEMPTS} attempts); exiting")
            return

        pdfs = sorted((DOCS_ROOT / target).glob("*.pdf"))
        if not pdfs:
            log(f"skip {target}: no local PDFs")
            attempts[target] = MAX_HOA_ATTEMPTS
            state["attempts"] = attempts
            failed_uploads.add(target)
            state["failed_uploads"] = sorted(failed_uploads)
            save_state(state)
            continue

        size_mb = sum(p.stat().st_size for p in pdfs) / (1024 * 1024)
        log(f"upload start hoa={target} pdfs={len(pdfs)} size_mb={size_mb:.1f}")

        try:
            with ExitStack() as stack:
                files = [
                    ("files", (p.name, stack.enter_context(p.open("rb")), "application/pdf"))
                    for p in pdfs
                ]
                r = s.post(
                    f"{base}/upload",
                    data={"hoa": target},
                    files=files,
                    timeout=UPLOAD_TIMEOUT,
                )
            if r.status_code != 200:
                log(f"upload fail hoa={target} status={r.status_code} body={r.text[:220]}")
                attempts[target] = int(attempts.get(target, 0)) + 1
                state["attempts"] = attempts
                failed_uploads.add(target)
                state["failed_uploads"] = sorted(failed_uploads)
                save_state(state)
                continue
            body = r.json()
            log(f"upload queued hoa={target} queued={body.get('queued')} files={len(body.get('saved_files', []))}")
        except Exception as exc:  # noqa: BLE001
            log(f"upload exception hoa={target} {type(exc).__name__}: {exc}")
            attempts[target] = int(attempts.get(target, 0)) + 1
            state["attempts"] = attempts
            failed_uploads.add(target)
            state["failed_uploads"] = sorted(failed_uploads)
            save_state(state)
            continue

        start = time.time()
        last_wait_log = -1
        while True:
            time.sleep(POLL_EVERY_S)
            try:
                docs = get_docs(target)
                if docs:
                    chunk_sum = sum(int(d.get("chunk_count", 0)) for d in docs)
                    elapsed = int(time.time() - start)
                    log(f"indexed hoa={target} docs={len(docs)} chunks={chunk_sum} elapsed_s={elapsed}")
                    completed.add(target)
                    attempts.pop(target, None)
                    state["attempts"] = attempts
                    state["completed"] = sorted(completed)
                    timeouts.discard(target)
                    failed_uploads.discard(target)
                    state["timeouts"] = sorted(timeouts)
                    state["failed_uploads"] = sorted(failed_uploads)
                    save_state(state)
                    break
            except Exception as exc:  # noqa: BLE001
                log(f"poll warning hoa={target} {type(exc).__name__}: {exc}")

            elapsed = int(time.time() - start)
            if elapsed - last_wait_log >= 60:
                last_wait_log = elapsed
                log(f"wait hoa={target} elapsed_s={elapsed}")

            if time.time() - start >= POLL_TIMEOUT_S:
                elapsed = int(time.time() - start)
                log(f"timeout hoa={target} no docs after {elapsed}s")
                attempts[target] = int(attempts.get(target, 0)) + 1
                state["attempts"] = attempts
                timeouts.add(target)
                state["timeouts"] = sorted(timeouts)
                save_state(state)
                break


if __name__ == "__main__":
    main()
