#!/usr/bin/env python3
"""Recovery tool — restore already-deleted real HOAs as docless stubs.

WARNING — superseded by ``clean_junk_docs.py`` for new content-audit work.

This script is the post-hoc recovery half of the original 2026-05-09 audit
flow that turned out to be lossy: ``delete_junk_hoas.py`` deleted entities
flagged as having junk content, then ``restore_stubs.py`` re-created them as
stubs carrying only ``name`` / ``state`` / ``city`` — **silently dropping
``latitude``, ``longitude``, ``boundary_geojson``, ``street``, ``postal_code``,
and ``location_quality``** because ``/admin/delete-hoa`` cascades through
``hoa_locations``. The 1,147 entities restored by this script that way ended
up docless AND geometryless. A separate Pass B run is re-geocoding those
from bank manifests + HERE.

Going forward, content cleanup should use ``clean_junk_docs.py``, which
calls the new ``/admin/clear-hoa-docs`` endpoint to drop documents while
preserving the entity row and all its hoa_locations geometry. That leaves
no recovery work for a follow-up tool.

This script remains here only for the original 2026-05-09 audit recovery
flow and any future case where a rogue ``/admin/delete-hoa`` happens
without geometry preservation. Don't use it as part of a routine cleanup.

Classification (unchanged):
  - WAS_REAL_HOA  — name passes the HOA-shape heuristic. Restored as a
                    stub via ``/admin/create-stub-hoas``.
  - NOT_REAL_HOA  — name is a document fragment (e.g. "Stormwater Drainage
                    Policy HOA"). Stays deleted.

Run:
  python scripts/audit/restore_stubs.py            # dry-run (print)
  python scripts/audit/restore_stubs.py --apply
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

# Patterns that indicate "name is a document fragment, not a real HOA"
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
    r"\bSloa\s+Bulletin\b",  # newsletter title
    r"\bBoard\s+of\s+County\s+Commissioners\b",
    r"\bTo\s+Whom\s+It\s+May\s+Concern\b",  # form letter
]
_JUNK_NAME_RE = re.compile("|".join(JUNK_NAME_FRAGMENTS), re.IGNORECASE)

# Reason fragments that imply the HOA name maps to a different HOA / state /
# court case. These should stay deleted (because the name itself was an
# error of attribution, not because the HOA is real).
KEEP_DELETED_REASON_RE = re.compile(
    r"\b(wrong\s+(?:state|HOA|jurisdiction|entity|county)|"
    r"different\s+(?:state|HOA|entity|county|association|property|jurisdiction|sunrise)|"
    r"another\s+(?:state|HOA)|"
    r"in\s+(?:Florida|Idaho|Colorado|Alabama|Georgia|Nebraska|Mississippi|Oregon|Tennessee|North\s+Carolina|South\s+Carolina|California|Washington|Texas|Arizona|New\s+York|Utah|Hawaii|Maine|Vermont|Connecticut|Massachusetts|Pennsylvania|Virginia|Ohio|Wisconsin|Minnesota|Michigan|Iowa|Indiana|Kansas|Kentucky|Louisiana|Arkansas|Oklahoma|New\s+Mexico|Nevada|Wyoming|Montana|North\s+Dakota|South\s+Dakota|Alaska)\b|"
    r"\bFederal\s+Register\b|"
    r"\bcourt\s+filing\b|\bcourt\s+opinion\b|\bcourt\s+appeal\b|"
    r"\blegal\s+brief\b|\blegal\s+petition\b)",
    re.IGNORECASE,
)

# Heuristic: name "looks like a real HOA" if it contains community-type suffix.
HOA_NAME_HINT_RE = re.compile(
    r"\b(association|owners|homeowners|condominiums?|condos?|coop|cooperative|"
    r"council|townhomes?|townhouse|villas?|tower|apartments?|estates|"
    r"property\s+owners?|hoa|h\.o\.a\.|poa)\b",
    re.IGNORECASE,
)


def looks_like_real_hoa(name: str) -> bool:
    if _JUNK_NAME_RE.search(name):
        return False
    return bool(HOA_NAME_HINT_RE.search(name))


# States where the entity universe was sourced from an authoritative public
# registry (DCCA AOUO list, DC CONDO REGIME table, etc.). For these, every
# entity-shaped name should be restored even if it lacks a typical HOA-suffix.
REGISTRY_SOURCED_STATES = {"HI", "DC"}


def classify(entry: dict) -> str:
    """Return 'restore' or 'keep_deleted'.

    Name-first: if the HOA's name itself looks like a real HOA (community-type
    suffix present, no document-fragment markers), restore as a stub. The fact
    that its banked documents turned out to belong to a different HOA does NOT
    mean the entity is fake — it just means doc discovery went wrong. The
    registered entity is still real and worth carrying as a docless stub.

    For registry-sourced states (HI, DC), restore any name that is not an
    obvious document-fragment, regardless of HOA-suffix presence — these
    entities came from authoritative public registries.

    Only keep deleted when:
      - the name itself is a document fragment (e.g. "Stormwater Drainage
        Policy HOA", "Sloa Bulletin November", "January 30, 2023 the Board…").
    """
    name = entry.get("hoa") or ""
    if _JUNK_NAME_RE.search(name):
        return "keep_deleted"
    state = (entry.get("state") or "").upper()
    if state in REGISTRY_SOURCED_STATES:
        # Trust the registry source; only block obvious doc fragments.
        return "restore"
    if not looks_like_real_hoa(name):
        return "keep_deleted"
    return "restore"


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


def build_stub_record(entry: dict) -> dict:
    return {
        "name": entry["hoa"],
        "metadata_type": "condo" if "condominium" in entry["hoa"].lower() or "condo " in entry["hoa"].lower() else None,
        "city": entry.get("city"),
        "state": entry.get("state"),
        "source": "audit_2026_05_09_restored_stub",
        "source_url": None,
        "location_quality": "city_only" if entry.get("city") else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--state", default=None,
                    help="Restrict to one state (defaults: all states with grade files)")
    ap.add_argument("--out", default="state_scrapers/_orchestrator/quality_audit_2026_05_09/restore_stubs_outcome.json")
    args = ap.parse_args()

    audit_root = Path("state_scrapers")
    grade_files: list[tuple[str, Path]] = []
    for state_dir in sorted(audit_root.iterdir()):
        if not state_dir.is_dir():
            continue
        if args.state and state_dir.name != args.state.lower():
            continue
        for name in (f"{state_dir.name}_grades.json", f"{state_dir.name}_sample.json"):
            p = state_dir / "results" / "audit_2026_05_09" / name
            if p.exists():
                grade_files.append((state_dir.name.upper(), p))
                break

    print(f"[restore] reading {len(grade_files)} grade files")
    restore_records: list[dict] = []
    keep_deleted_records: list[dict] = []
    by_state: dict[str, dict] = {}
    for state, gpath in grade_files:
        d = json.loads(gpath.read_text())
        junk = [r for r in d.get("results", []) if r.get("verdict") == "junk"]
        st = {"restored": [], "kept_deleted": [], "total_junk": len(junk)}
        for entry in junk:
            decision = classify(entry)
            if decision == "restore":
                rec = build_stub_record(entry)
                rec["_grade_state"] = state
                restore_records.append(rec)
                st["restored"].append(entry["hoa"])
            else:
                keep_deleted_records.append({"state": state, "hoa": entry["hoa"], "reason": entry.get("reason")})
                st["kept_deleted"].append({"hoa": entry["hoa"], "reason": entry.get("reason", "")[:80]})
        by_state[state] = st

    print(f"[restore] decisions: restore={len(restore_records)} keep_deleted={len(keep_deleted_records)}")
    for state, st in sorted(by_state.items()):
        if st["total_junk"] == 0:
            continue
        print(f"  {state}: junk={st['total_junk']}  restore={len(st['restored'])}  keep_deleted={len(st['kept_deleted'])}")

    if not args.apply:
        # Dry run — show samples
        print()
        print("=== KEEP-DELETED samples (5 per state) ===")
        seen_states: dict[str, int] = {}
        for r in keep_deleted_records:
            seen_states[r["state"]] = seen_states.get(r["state"], 0) + 1
            if seen_states[r["state"]] <= 5:
                print(f"  {r['state']}  {r['hoa'][:60]}  | {(r['reason'] or '')[:80]}")
        print()
        print("=== RESTORE samples (5 per state) ===")
        seen_states = {}
        for r in restore_records:
            st = r["_grade_state"]
            seen_states[st] = seen_states.get(st, 0) + 1
            if seen_states[st] <= 5:
                print(f"  {st}  {r['name'][:60]}  city={r.get('city')}")
        print()
        print("Pass --apply to create stubs.")
        return 0

    token = live_admin_token()
    if not token:
        print("[restore] no admin token", file=sys.stderr)
        return 2

    # Strip helper fields before sending
    payload_records = [{k: v for k, v in r.items() if not k.startswith("_")} for r in restore_records]

    BATCH = 100
    created = 0
    updated = 0
    failed: list[dict] = []
    for i in range(0, len(payload_records), BATCH):
        chunk = payload_records[i:i + BATCH]
        try:
            r = requests.post(
                "https://hoaproxy.org/admin/create-stub-hoas",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"records": chunk}, timeout=300,
            )
            if r.status_code == 200:
                body = r.json()
                created += int(body.get("created", 0))
                updated += int(body.get("updated", 0))
                print(f"  batch {i // BATCH + 1}: created={body.get('created')} updated={body.get('updated')} skipped={body.get('skipped')}")
            else:
                failed.append({"batch_start": i, "http": r.status_code, "body": r.text[:300]})
                print(f"  batch {i // BATCH + 1}: FAIL http {r.status_code}: {r.text[:200]}")
        except Exception as e:
            failed.append({"batch_start": i, "error": f"{type(e).__name__}: {e}"})
        time.sleep(2.0)

    summary = {
        "restored_total": len(payload_records),
        "kept_deleted_total": len(keep_deleted_records),
        "created": created,
        "updated": updated,
        "failed": failed,
        "by_state": {s: {"restore_count": len(v["restored"]), "kept_deleted": len(v["kept_deleted"])} for s, v in by_state.items()},
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: summary[k] for k in ("restored_total", "kept_deleted_total", "created", "updated")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
