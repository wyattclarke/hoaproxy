#!/usr/bin/env python3
"""Clean junk-content HOAs from a content-grading JSON file.

Replaces the old delete-then-restore-stub flow that lost geometry on every
"real HOA, junk docs" case (see the 2026-05-09 audit incident retrospective).

For each ``verdict=="junk"`` entry in the supplied grade JSON:

  - **Real HOA name** (passes the name-shape heuristic): call
    ``/admin/clear-hoa-docs``. The hoa + hoa_locations row is preserved
    along with all geometry; only the documents and chunks are removed.
    Result: the entity remains live as a docless stub.

  - **Document-fragment name** (matches one of the known junk-name
    patterns — "Stormwater Drainage Policy HOA", "Sloa Bulletin
    November", etc.): call ``/admin/delete-hoa``. The entity was never a
    real HOA; full delete is the right outcome.

This is the going-forward replacement for ``restore_stubs.py``, which
remains in place only as a recovery tool for already-deleted-and-stubbed
rows from the 2026-05-09 audit.

Usage:
    python scripts/audit/clean_junk_docs.py --grades <path>           # dry-run
    python scripts/audit/clean_junk_docs.py --grades <path> --apply
    python scripts/audit/clean_junk_docs.py --apply                   # all states
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / "settings.env")

DEFAULT_BASE_URL = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")


# Patterns that indicate "name is a document fragment, not a real HOA".
# Mirrors restore_stubs.py — keep them in sync, since both use the same
# real-HOA-name heuristic.
JUNK_NAME_FRAGMENTS = [
    r"\bStormwater\s+Drainage\b",
    r"\bZONING\s+UPDATE\b",
    r"^R\s+E\s+S\s+O\s+L\s+U\s+T\s+I\s+O\s+N",
    r"^Summary\s+of\b",
    r"^Staff\s+Analysis",
    r"^January\s+\d+,?\s+\d{4}",
    r"^February\s+\d+,?\s+\d{4}",
    r"^March\s+\d+,?\s+\d{4}",
    r"^Madison\s+County\s+Zoning",
    r"\bSloa\s+Bulletin\b",
    r"\bBoard\s+of\s+County\s+Commissioners\b",
    r"\bTo\s+Whom\s+It\s+May\s+Concern\b",
]
_JUNK_NAME_RE = re.compile("|".join(JUNK_NAME_FRAGMENTS), re.IGNORECASE)

HOA_NAME_HINT_RE = re.compile(
    r"\b(association|owners|homeowners|condominiums?|condos?|coop|cooperative|"
    r"council|townhomes?|townhouse|villas?|tower|apartments?|estates|"
    r"property\s+owners?|hoa|h\.o\.a\.|poa)\b",
    re.IGNORECASE,
)

# Registry-sourced states whose entity names came from authoritative public
# registries. For these, every name is a real registered entity even if it
# lacks an obvious HOA suffix.
REGISTRY_SOURCED_STATES = {"HI", "DC"}


def looks_like_real_hoa(name: str) -> bool:
    if _JUNK_NAME_RE.search(name):
        return False
    return bool(HOA_NAME_HINT_RE.search(name))


def classify(entry: dict) -> str:
    """Return ``'clear_docs'`` or ``'delete_entity'``."""
    name = entry.get("hoa") or ""
    if _JUNK_NAME_RE.search(name):
        return "delete_entity"
    state = (entry.get("state") or "").upper()
    if state in REGISTRY_SOURCED_STATES:
        return "clear_docs"
    if not looks_like_real_hoa(name):
        return "delete_entity"
    return "clear_docs"


def live_admin_token() -> str | None:
    if os.environ.get("HOAPROXY_ADMIN_BEARER"):
        return os.environ["HOAPROXY_ADMIN_BEARER"]
    api_key = os.environ.get("RENDER_API_KEY")
    sid = os.environ.get("RENDER_SERVICE_ID")
    if api_key and sid:
        try:
            r = requests.get(
                f"https://api.render.com/v1/services/{sid}/env-vars",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            for env in r.json():
                e = env.get("envVar", env)
                if e.get("key") == "JWT_SECRET" and e.get("value"):
                    return e["value"]
        except Exception:
            pass
    return os.environ.get("JWT_SECRET")


def post_with_retry(
    url: str, *, token: str, payload: dict, timeout: int = 180, retries: int = 5
) -> tuple[int, dict | None, str | None]:
    last_err: str | None = None
    for attempt in range(retries):
        try:
            r = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
            if r.status_code == 200:
                return r.status_code, r.json(), None
            last_err = f"http {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(8 + attempt * 6)
    return 0, None, last_err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--grades",
        default=None,
        help=(
            "Path to a single grade JSON file. If omitted, reads every "
            "state_scrapers/*/results/audit_2026_05_09/{state}_grades.json "
            "found locally."
        ),
    )
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--batch-size", type=int, default=15)
    ap.add_argument("--max-process", type=int, default=2000,
                    help="Refuse to process more than this many in one run without --ack-large.")
    ap.add_argument("--ack-large", action="store_true")
    ap.add_argument(
        "--out",
        default="state_scrapers/_orchestrator/quality_audit_2026_05_09/clean_junk_outcome.json",
    )
    args = ap.parse_args()

    grade_files: list[tuple[str, Path]] = []
    if args.grades:
        p = Path(args.grades)
        if not p.exists():
            print(f"grade file missing: {p}", file=sys.stderr)
            return 2
        # Best-effort state inference from path
        state = "UNK"
        for part in p.parts:
            if len(part) == 2 and part.isalpha():
                state = part.upper()
                break
        grade_files.append((state, p))
    else:
        audit_root = Path("state_scrapers")
        for state_dir in sorted(audit_root.iterdir()):
            if not state_dir.is_dir():
                continue
            p = state_dir / "results" / "audit_2026_05_09" / f"{state_dir.name}_grades.json"
            if p.exists():
                grade_files.append((state_dir.name.upper(), p))

    if not grade_files:
        print("no grade files found")
        return 0

    print(f"[clean_junk_docs] reading {len(grade_files)} grade files")
    clear_targets: list[dict[str, Any]] = []
    delete_targets: list[dict[str, Any]] = []
    by_state: dict[str, dict[str, Any]] = {}

    for state, gpath in grade_files:
        d = json.loads(gpath.read_text())
        st = {"clear": 0, "delete": 0}
        for entry in d.get("results", []):
            if entry.get("verdict") != "junk":
                continue
            hoa_id = entry.get("hoa_id")
            if not isinstance(hoa_id, int):
                continue
            decision = classify(entry)
            target = {
                "hoa_id": hoa_id,
                "hoa": entry.get("hoa"),
                "state": state,
                "category": entry.get("category"),
                "reason": entry.get("reason"),
            }
            if decision == "clear_docs":
                clear_targets.append(target)
                st["clear"] += 1
            else:
                delete_targets.append(target)
                st["delete"] += 1
        by_state[state] = st

    print(
        f"[clean_junk_docs] decisions: clear_docs={len(clear_targets)} "
        f"delete_entity={len(delete_targets)}"
    )
    for state, st in sorted(by_state.items()):
        if st["clear"] or st["delete"]:
            print(f"  {state}: clear={st['clear']}  delete={st['delete']}")

    total = len(clear_targets) + len(delete_targets)
    if total > args.max_process and not args.ack_large:
        print(
            f"[clean_junk_docs] refusing: {total} > --max-process={args.max_process}; "
            f"pass --ack-large to override",
            file=sys.stderr,
        )
        return 2

    if not args.apply:
        print()
        print("=== clear_docs samples (preserve entity, drop docs) ===")
        seen: dict[str, int] = {}
        for t in clear_targets:
            seen[t["state"]] = seen.get(t["state"], 0) + 1
            if seen[t["state"]] <= 3:
                print(f"  {t['state']}  id={t['hoa_id']:>6}  {(t['hoa'] or '')[:55]}")
        print()
        print("=== delete_entity samples (full delete, name was not real) ===")
        seen = {}
        for t in delete_targets:
            seen[t["state"]] = seen.get(t["state"], 0) + 1
            if seen[t["state"]] <= 3:
                print(
                    f"  {t['state']}  id={t['hoa_id']:>6}  {(t['hoa'] or '')[:55]}  "
                    f"| {(t.get('reason') or '')[:60]}"
                )
        print()
        print("Pass --apply to execute.")
        return 0

    token = live_admin_token()
    if not token:
        print("[clean_junk_docs] no admin token", file=sys.stderr)
        return 2

    BATCH = max(1, args.batch_size)
    cleared = 0
    deleted = 0
    failures: list[dict] = []

    def _send_batch(endpoint: str, ids: list[int]) -> dict | None:
        url = f"{args.base_url}{endpoint}"
        status, body, err = post_with_retry(url, token=token, payload={"hoa_ids": ids})
        if body is None:
            failures.append({"endpoint": endpoint, "ids": ids[:5], "error": err})
            print(f"  FAIL {endpoint} ids={ids[:5]}…  {err}", flush=True)
            return None
        return body

    if clear_targets:
        print(f"[clean_junk_docs] /admin/clear-hoa-docs on {len(clear_targets)} HOAs")
        for i in range(0, len(clear_targets), BATCH):
            chunk = clear_targets[i : i + BATCH]
            ids = [t["hoa_id"] for t in chunk]
            body = _send_batch("/admin/clear-hoa-docs", ids)
            if body:
                cleared += int(body.get("cleared", 0))
                print(
                    f"  batch {i // BATCH + 1}/{(len(clear_targets) - 1) // BATCH + 1}: "
                    f"cleared={body.get('cleared')}",
                    flush=True,
                )
            time.sleep(1.5)

    if delete_targets:
        print(f"[clean_junk_docs] /admin/delete-hoa on {len(delete_targets)} HOAs")
        for i in range(0, len(delete_targets), BATCH):
            chunk = delete_targets[i : i + BATCH]
            ids = [t["hoa_id"] for t in chunk]
            body = _send_batch("/admin/delete-hoa", ids)
            if body:
                deleted += int(body.get("deleted", 0))
                print(
                    f"  batch {i // BATCH + 1}/{(len(delete_targets) - 1) // BATCH + 1}: "
                    f"deleted={body.get('deleted')}",
                    flush=True,
                )
            time.sleep(1.5)

    summary = {
        "clear_targets": len(clear_targets),
        "delete_targets": len(delete_targets),
        "cleared": cleared,
        "deleted": deleted,
        "failures": failures,
        "by_state": by_state,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(
        {k: summary[k] for k in ("clear_targets", "delete_targets", "cleared", "deleted")},
        indent=2,
    ))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
