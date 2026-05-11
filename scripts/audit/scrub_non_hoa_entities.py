#!/usr/bin/env python3
"""Snapshot then delete entries that aren't real HOAs/condos/coops.

The 2026-05-10 county-GIS backfills pulled in a lot of names that look
like recorded subdivisions / plats / auditor records, not residential
associations with mandatory membership. We want to drop those from
the live site, but keep the lists so a more careful future pass can:

  - For each name, search for an associated HOA via Serper / aggregator
    lookups ("<name> homeowners association" filetype:pdf).
  - Re-add the ones that turn out to have a real HOA (via the bug-fixed
    /admin/create-stub-hoas with on_collision=disambiguate).

Modes
-----
* ``--mode delete-source``: drop every row tagged with the given source.
  Use for sources whose entire content is plats (e.g. MN's
  ``mn-statewide-subdivisions-liveby`` is 97% plats).

* ``--mode grade-and-delete``: pull all rows for the source, ask an LLM
  to classify each name as hoa / subdivision / other / uncertain, then
  delete only the non-``hoa`` rows. Use for mixed sources (most OH /
  VA / MI / WA / NC / MO county-GIS sources).

In both modes the deleted rows are snapshotted to
``state_scrapers/{state}/leads/{state}_unverified_subdivisions.jsonl``
(or whatever ``--snapshot-out`` specifies) BEFORE the deletes happen.
Re-runs are append-only so multiple sources merge into one file per state.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / "settings.env")

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_GRADER_MODEL = "deepseek/deepseek-v4-flash"

GRADE_SYSTEM = (
    "You are auditing entity names on a public HOA / condo association "
    "directory. The site only represents real residential communities with "
    "mandatory membership in a democratic neighborhood association — i.e. "
    "homeowners associations (HOAs), property owners associations (POAs), "
    "condominium associations, and residential housing cooperatives. "
    "It explicitly does NOT want: commercial condominiums; recorded land "
    "subdivisions/plats with no mandatory association; utility/water/special "
    "districts; civic groups; community development districts; PUDs without "
    "an active HOA; chambers of commerce."
)

GRADE_INSTR = (
    "Classify each name as:\n"
    "  hoa         — real residential HOA/POA/condo association/coop\n"
    "  subdivision — likely just a recorded subdivision/plat\n"
    "  other       — not residential at all\n"
    "  uncertain   — genuinely ambiguous\n"
    "\nRespond with strict JSON: "
    '{"results":[{"name":"...","verdict":"hoa","reason":"one short clause"}, ...]}'
    "\nUse the name only — names with an explicit association suffix "
    "(Homeowners Association, Property Owners Association, Condominium "
    "Association, Owners Corp, Tenants Corp, Cooperative, etc.) → hoa. "
    "Bare subdivision names without that suffix → subdivision."
)


def live_admin_token() -> str | None:
    if os.environ.get("HOAPROXY_ADMIN_BEARER"):
        return os.environ["HOAPROXY_ADMIN_BEARER"]
    api_key = os.environ.get("RENDER_API_KEY")
    sid = os.environ.get("RENDER_SERVICE_ID")
    if api_key and sid:
        try:
            r = requests.get(
                f"https://api.render.com/v1/services/{sid}/env-vars",
                headers={"Authorization": f"Bearer {api_key}"}, timeout=30,
            )
            for env in r.json():
                e = env.get("envVar", env)
                if e.get("key") == "JWT_SECRET" and e.get("value"):
                    return e["value"]
        except Exception:
            pass
    return os.environ.get("JWT_SECRET")


def fetch_rows_by_source(source: str, token: str) -> list[dict]:
    """Pull every hoa_locations row tagged with this source."""
    for attempt in range(5):
        try:
            r = requests.post(
                "https://hoaproxy.org/admin/list-corruption-targets",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"sources": [source]}, timeout=300,
            )
            if r.status_code == 200:
                body = r.json()
                return body if isinstance(body, list) else body.get("rows", [])
            print(f"  list-corruption-targets http {r.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"  list-corruption-targets error: {e}", file=sys.stderr)
        time.sleep(10 + attempt * 8)
    return []


def grade_names_in_chunks(rows: list[dict], model: str, chunk_size: int = 40) -> dict[str, dict]:
    """LLM-grade names in chunks. Returns {name -> {verdict, reason}}."""
    api_key = os.environ["OPENROUTER_API_KEY"]
    name_to_verdict: dict[str, dict] = {}
    names = [r.get("hoa") or "" for r in rows]
    # Dedup names while preserving order
    unique_names: list[str] = []
    seen: set[str] = set()
    for n in names:
        if n and n not in seen:
            seen.add(n)
            unique_names.append(n)
    print(f"  grading {len(unique_names)} unique names in chunks of {chunk_size}")
    for i in range(0, len(unique_names), chunk_size):
        chunk = unique_names[i:i + chunk_size]
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": GRADE_SYSTEM + "\n\n" + GRADE_INSTR},
                {"role": "user", "content": "Classify:\n" + "\n".join(f"- {n}" for n in chunk)},
            ],
            "temperature": 0,
            "max_tokens": 6000,
            "response_format": {"type": "json_object"},
        }
        for attempt in range(4):
            try:
                r = requests.post(OPENROUTER, headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://hoaproxy.org",
                    "X-Title": "hoaproxy non-hoa scrub",
                }, json=body, timeout=180)
                if r.status_code == 200:
                    content = r.json()["choices"][0]["message"]["content"].strip()
                    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
                    try:
                        results = json.loads(content).get("results", [])
                    except Exception:
                        # Try recovering JSON-ish body
                        m = re.search(r"\{[\s\S]*\}", content)
                        results = json.loads(m.group(0)).get("results", []) if m else []
                    for r in results:
                        n = r.get("name")
                        if n:
                            name_to_verdict[n] = {
                                "verdict": (r.get("verdict") or "uncertain").lower(),
                                "reason": r.get("reason") or "",
                            }
                    break
            except Exception:
                pass
            time.sleep(3 + attempt * 3)
        # Mark anything the model missed as uncertain
        for n in chunk:
            name_to_verdict.setdefault(n, {"verdict": "uncertain", "reason": "no response"})
        if (i // chunk_size) % 5 == 0:
            print(f"    [{min(i + chunk_size, len(unique_names))}/{len(unique_names)}]", flush=True)
        time.sleep(0.5)
    return name_to_verdict


def append_snapshot(snapshot_path: Path, rows: list[dict], extra: dict | None = None) -> int:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(snapshot_path, "a", encoding="utf-8") as f:
        for r in rows:
            row = {
                "hoa_id": r.get("hoa_id"),
                "name": r.get("hoa") or r.get("name"),
                "metadata_type": r.get("metadata_type"),
                "street": r.get("street"),
                "city": r.get("city"),
                "state": r.get("state"),
                "postal_code": r.get("postal_code"),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "boundary_geojson": r.get("boundary_geojson") if isinstance(r.get("boundary_geojson"), dict) else None,
                "has_boundary": bool(r.get("has_boundary")),
                "source": r.get("source"),
                "location_quality": r.get("location_quality"),
            }
            if extra:
                row.update(extra)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    return written


def delete_ids(hoa_ids: list[int], token: str, batch_size: int = 25) -> tuple[int, list[dict]]:
    """Bulk delete via /admin/delete-hoa. Returns (deleted_count, failures)."""
    deleted = 0
    failures: list[dict] = []
    for i in range(0, len(hoa_ids), batch_size):
        chunk = hoa_ids[i:i + batch_size]
        for attempt in range(5):
            try:
                r = requests.post(
                    "https://hoaproxy.org/admin/delete-hoa",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"hoa_ids": chunk}, timeout=180,
                )
                if r.status_code == 200:
                    body = r.json()
                    deleted += int(body.get("deleted", 0))
                    break
                else:
                    last = f"http {r.status_code}: {r.text[:200]}"
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
            time.sleep(8 + attempt * 6)
        else:
            failures.append({"batch_start": i, "error": last})
        if i % (batch_size * 10) == 0:
            print(f"  [{min(i + batch_size, len(hoa_ids))}/{len(hoa_ids)}]  deleted={deleted}", flush=True)
        time.sleep(1.0)
    return deleted, failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="hoa_locations.source string to target")
    ap.add_argument(
        "--mode", required=True,
        choices=["delete-source", "grade-and-delete"],
        help=(
            "delete-source: drop every row tagged with this source (use for "
            "all-junk sources like MN's statewide-liveby).\n"
            "grade-and-delete: LLM-grade names, drop only non-hoa rows."
        ),
    )
    ap.add_argument("--state", required=True, help="State code (used for snapshot file path)")
    ap.add_argument(
        "--snapshot-out", default=None,
        help="JSONL snapshot file. Default: state_scrapers/{state}/leads/{state}_unverified_subdivisions.jsonl",
    )
    ap.add_argument("--model", default=DEFAULT_GRADER_MODEL)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--max-delete", type=int, default=50000,
        help="Refuse to delete more than this without --ack-large",
    )
    ap.add_argument("--ack-large", action="store_true")
    args = ap.parse_args()

    state_lc = args.state.lower()
    snapshot_path = Path(args.snapshot_out) if args.snapshot_out else (
        ROOT / f"state_scrapers/{state_lc}/leads/{state_lc}_unverified_subdivisions.jsonl"
    )

    token = live_admin_token()
    if not token:
        print("no admin token", file=sys.stderr)
        return 2

    print(f"[scrub] fetching rows tagged source='{args.source}'")
    rows = fetch_rows_by_source(args.source, token)
    print(f"[scrub] {len(rows)} rows fetched")
    if not rows:
        return 0

    # Decide which rows to delete
    delete_ids_list: list[int] = []
    snapshot_extra_by_id: dict[int, dict] = {}
    if args.mode == "delete-source":
        for r in rows:
            hid = r.get("hoa_id")
            if isinstance(hid, int):
                delete_ids_list.append(hid)
                snapshot_extra_by_id[hid] = {"verdict": "subdivision_unverified", "scrub_mode": "delete-source"}
    else:
        # grade-and-delete
        name_to_verdict = grade_names_in_chunks(rows, args.model)
        by_verdict: dict[str, int] = {}
        for r in rows:
            name = r.get("hoa") or ""
            v = name_to_verdict.get(name, {"verdict": "uncertain"})
            by_verdict[v["verdict"]] = by_verdict.get(v["verdict"], 0) + 1
            if v["verdict"] != "hoa":
                hid = r.get("hoa_id")
                if isinstance(hid, int):
                    delete_ids_list.append(hid)
                    snapshot_extra_by_id[hid] = {
                        "verdict": v["verdict"],
                        "reason": v.get("reason"),
                        "scrub_mode": "grade-and-delete",
                    }
        print(f"[scrub] verdicts on names: {by_verdict}")
        print(f"[scrub] {len(delete_ids_list)} rows targeted for deletion (verdict != 'hoa')")

    if len(delete_ids_list) > args.max_delete and not args.ack_large:
        print(f"refusing to delete {len(delete_ids_list)} > --max-delete={args.max_delete}; pass --ack-large", file=sys.stderr)
        return 2

    # Build the snapshot from rows whose hoa_id is in delete list
    snap_rows = []
    by_id = {r.get("hoa_id"): r for r in rows if isinstance(r.get("hoa_id"), int)}
    for hid in delete_ids_list:
        r = by_id.get(hid)
        if r is None: continue
        snap_rows.append(r)

    if not args.apply:
        print(f"[scrub] DRY RUN — would snapshot {len(snap_rows)} rows and delete {len(delete_ids_list)} ids")
        print(f"  snapshot path: {snapshot_path}")
        if delete_ids_list:
            print(f"  first ids to delete: {delete_ids_list[:5]}")
        return 0

    # Snapshot first, before any destructive op
    written = 0
    for r in snap_rows:
        hid = r.get("hoa_id")
        extra = snapshot_extra_by_id.get(hid, {})
        written += append_snapshot(snapshot_path, [r], extra=extra)
    print(f"[scrub] snapshotted {written} rows to {snapshot_path}")

    # Then delete
    deleted, failures = delete_ids(delete_ids_list, token)
    print(f"[scrub] deleted={deleted}  failures={len(failures)}")

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
