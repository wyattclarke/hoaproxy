#!/usr/bin/env python3
"""Bulk-create docless HOA stubs from authoritative state registries.

For each state's registry-derived leads JSONL, walk every entity and POST it
to /admin/create-stub-hoas. The endpoint upserts on name (creates if new,
updates location if existing), so re-running is safe.

Built to fill the gap between the registered universe (RI SoS 721, HI AOUO
1,445, CT SoS 3,447) and the smaller subset that ended up live with
documents. Aligns with the user's "I want all the HOAs" directive.

Run:
  python scripts/audit/backfill_registry_stubs.py --state RI
  python scripts/audit/backfill_registry_stubs.py --state HI
  python scripts/audit/backfill_registry_stubs.py --state CT
  python scripts/audit/backfill_registry_stubs.py --state ALL --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / "settings.env")


REGISTRIES = {
    "RI": {
        "leads": ROOT / "state_scrapers/ri/leads/ri_sos_associations.jsonl",
        "source": "sos-ri",
        "state": "RI",
    },
    "HI": {
        "leads": ROOT / "state_scrapers/hi/leads/hi_aouo_seed.jsonl",
        "source": "hi-dcca-aouo-contact-list",
        "state": "HI",
    },
    "CT": {
        "leads": ROOT / "state_scrapers/ct/leads/ct_sos_associations.jsonl",
        "source": "sos-ct",
        "state": "CT",
    },
    "CA": {
        "leads": ROOT / "state_scrapers/ca/leads/ca_registry_seed.jsonl",
        "source": "sos-ca-bizfile",
        "state": "CA",
    },
    "CO": {
        "leads": ROOT / "state_scrapers/co/leads/co_registry_seed.jsonl",
        "source": "co-dora-hoa-information-office",
        "state": "CO",
    },
    "TX": {
        "leads": ROOT / "state_scrapers/tx/leads/tx_registry_seed.jsonl",
        "source": "tx-trec-hoa-management-certificate",
        "state": "TX",
    },
    "NY": {
        "leads": ROOT / "state_scrapers/ny/leads/ny_registry_seed.jsonl",
        "source": "ny-dos-active-corporations",
        "state": "NY",
    },
    "OR": {
        "leads": ROOT / "state_scrapers/or/leads/or_registry_seed.jsonl",
        "source": "or-sos-active-nonprofit-corporations",
        "state": "OR",
    },
    "IL": {
        "leads": ROOT / "state_scrapers/il/leads/il_chicagoland_combined_seed.jsonl",
        "source": "il-cook-assessor-chicagoland",
        "state": "IL",
    },
    "FL": {
        "leads": ROOT / "state_scrapers/fl/leads/fl_sunbiz_seed.jsonl",
        "source": "fl-sunbiz",
        "state": "FL",
    },
    "AZ": {
        "leads": ROOT / "state_scrapers/az/leads/az_tucson_seed.jsonl",
        "source": "az-tucson-hoa-gis",
        "state": "AZ",
    },
}


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


def normalize_lead(lead: dict, state: str, source: str) -> dict | None:
    """Normalize a registry lead into the /admin/create-stub-hoas record shape.

    Notable defaults (changed after the 2026-05-09 audit incident):
      - ``location_quality`` is OMITTED — never set to ``"city_only"``. The
        upsert COALESCEs every column, so passing ``"city_only"`` against an
        existing row with a higher quality value (``"address"``,
        ``"polygon"``, ``"zip_centroid"``) silently demotes it. Letting the
        field stay NULL means existing values are preserved on upsert.
      - ``postal_code`` is always carried through when the source has it,
        so the row gets a ZIP-centroid geocoding anchor for downstream
        Phase-9 enrichment.
    """
    name = (lead.get("name") or "").strip()
    if not name or len(name) < 4:
        return None
    addr = lead.get("address") or {}
    city = addr.get("city") or lead.get("city")
    postal = addr.get("postal_code") or lead.get("postal_code")
    metadata_type = lead.get("metadata_type")
    if not metadata_type:
        n = name.lower()
        if "condominium" in n or "condo" in n or "owners association" in n:
            metadata_type = "condo"
        else:
            metadata_type = None
    return {
        "name": name,
        "metadata_type": metadata_type,
        "city": city,
        "state": state,
        "postal_code": postal,
        "source": source,
        "source_url": lead.get("source_url"),
        # Intentionally omit location_quality; see docstring.
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True, help="State key from REGISTRIES, or ALL")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--on-collision",
        default="disambiguate",
        choices=["skip", "disambiguate"],
        help=(
            "Cross-state name collision policy passed through to "
            "/admin/create-stub-hoas. 'skip' (safest) refuses any record "
            "whose name already exists in another state. 'disambiguate' "
            "(default for bulk registry imports) creates a separate row "
            "under '{name} ({STATE})' so coverage isn't lost when the same "
            "legal name is registered in multiple states."
        ),
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help=(
            "Records per /admin/create-stub-hoas POST. Larger batches are "
            "faster but the endpoint may return 500 above ~100 under load."
        ),
    )
    args = ap.parse_args()

    targets: list[str]
    if args.state.upper() == "ALL":
        targets = list(REGISTRIES.keys())
    else:
        if args.state.upper() not in REGISTRIES:
            print(f"unknown state: {args.state}", file=sys.stderr)
            return 2
        targets = [args.state.upper()]

    summary: dict = {}
    for st in targets:
        cfg = REGISTRIES[st]
        leads_path = cfg["leads"]
        if not leads_path.exists():
            print(f"{st}: leads file missing at {leads_path}", file=sys.stderr)
            continue

        records: list[dict] = []
        seen: set[str] = set()
        with open(leads_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    lead = json.loads(line)
                except Exception:
                    continue
                rec = normalize_lead(lead, cfg["state"], cfg["source"])
                if not rec:
                    continue
                key = rec["name"].lower()
                if key in seen:
                    continue
                seen.add(key)
                records.append(rec)

        print(f"[{st}] {len(records)} unique entities prepared")

        if not args.apply:
            print(f"[{st}] DRY RUN — sample records:")
            for r in records[:5]:
                print(f"   {r['name']:<60}  city={r.get('city')}  postal={r.get('postal_code')}")
            summary[st] = {"prepared": len(records), "applied": False}
            continue

        token = live_admin_token()
        if not token:
            print("[backfill] no admin token", file=sys.stderr)
            return 2

        BATCH = max(1, int(args.batch_size))
        created = 0
        updated = 0
        disambiguated = 0
        skipped_total = 0
        skipped_samples: list[dict] = []
        failed: list[dict] = []
        for i in range(0, len(records), BATCH):
            chunk = records[i:i + BATCH]
            try:
                r = requests.post(
                    "https://hoaproxy.org/admin/create-stub-hoas",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={
                        "records": chunk,
                        "on_collision": args.on_collision,
                    },
                    timeout=300,
                )
                if r.status_code == 200:
                    body = r.json()
                    c = int(body.get("created", 0))
                    u = int(body.get("updated", 0))
                    d = int(body.get("disambiguated", 0))
                    sk = int(body.get("skipped", 0))
                    created += c
                    updated += u
                    disambiguated += d
                    skipped_total += sk
                    if body.get("skipped_sample"):
                        skipped_samples.extend(body["skipped_sample"][:3])
                    print(
                        f"  [{st}] batch {i // BATCH + 1}: "
                        f"created={c} updated={u} disambiguated={d} skipped={sk}"
                    )
                else:
                    failed.append({"batch_start": i, "http": r.status_code, "body": r.text[:300]})
                    print(f"  [{st}] batch {i // BATCH + 1}: FAIL http {r.status_code}")
            except Exception as e:
                failed.append({"batch_start": i, "error": f"{type(e).__name__}: {e}"})
            time.sleep(2.0)

        summary[st] = {
            "prepared": len(records),
            "created": created,
            "updated": updated,
            "disambiguated": disambiguated,
            "skipped": skipped_total,
            "skipped_samples": skipped_samples[:30],
            "failed": failed,
            "on_collision": args.on_collision,
        }
        print(
            f"[{st}] DONE  created={created}  updated={updated}  "
            f"disambiguated={disambiguated}  skipped={skipped_total}  "
            f"failed={len(failed)}"
        )

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(summary, indent=2))

    print()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
