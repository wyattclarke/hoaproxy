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
    # 2026-05-10 second wave — county-GIS sources discovered after the
    # statewide-SoS hunt was blocked for these states. Each seed file
    # carries its own per-record `source` string; the dict-level `source`
    # is just the fallback. Multi-file states list every file under
    # `leads_glob`.
    "MA": {
        "leads": ROOT / "state_scrapers/ma/leads/ma_condo_trusts_statewide_seed.jsonl",
        "source": "ma-massgis-l3-parcels-owner-name",
        "state": "MA",
    },
    "MN": {
        "leads": ROOT / "state_scrapers/mn/leads/mn_statewide_subdivisions_seed.jsonl",
        "source": "mn-statewide-subdivisions-liveby",
        "state": "MN",
    },
    "MO": {
        "leads": ROOT / "state_scrapers/mo/leads/mo_springfield_subdivisions_seed.jsonl",
        "source": "mo-springfield-subdivisions",
        "state": "MO",
    },
    "WA": {
        "leads": ROOT / "state_scrapers/wa/leads/wa_snohomish_subdivisions_seed.jsonl",
        "source": "wa-snohomish-county-subdivisions",
        "state": "WA",
    },
    "OH": {
        "leads_glob": "state_scrapers/oh/leads/oh_*_seed.jsonl",
        "source": "oh-county-gis",
        "state": "OH",
    },
    "VA": {
        "leads_glob": "state_scrapers/va/leads/va_*_seed.jsonl",
        "source": "va-county-gis",
        "state": "VA",
    },
    "MD": {
        "leads_glob": "state_scrapers/md/leads/md_*_seed.jsonl",
        "source": "md-county-gis",
        "state": "MD",
    },
    "NC2": {
        # NC already has a small live presence under sources we don't own.
        # The new wave's NC seeds are county-GIS — give the dict key a
        # different label so it doesn't shadow the (empty) prior NC entry.
        "leads_glob": "state_scrapers/nc/leads/nc_*_seed.jsonl",
        "source": "nc-county-gis",
        "state": "NC",
    },
    "MI": {
        "leads_glob": "state_scrapers/mi/leads/mi_*_seed.jsonl",
        "source": "mi-county-gis",
        "state": "MI",
    },
}


def live_admin_token() -> str | None:
    # Explicit override wins; otherwise pull from settings.env. The Render
    # env-vars fallback was removed 2026-05-16 after the Hetzner cutover.
    return os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")


def normalize_lead(lead: dict, state: str, source: str | None = None) -> dict | None:
    """Normalize a registry lead into the /admin/create-stub-hoas record shape.

    The per-state ``source`` argument is a fallback used only when the lead
    record itself doesn't carry a ``source`` field. Multi-source seed files
    (e.g. county-GIS pulls that mix several layers) carry their own per-record
    ``source`` strings; we honour those over the registry-config default.

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
    postal = addr.get("postal_code") or lead.get("postal_code") or addr.get("zip")
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
        # Per-record source wins over the per-state default so multi-file
        # seeds (county-GIS, statewide L3 parcel mining, etc.) tag each row
        # with its actual provenance string.
        "source": (lead.get("source") or source),
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
        # Resolve to one or more lead files. ``leads`` is a single path;
        # ``leads_glob`` is a glob pattern relative to ROOT for multi-file
        # county-GIS imports.
        lead_paths: list[Path] = []
        if cfg.get("leads_glob"):
            lead_paths = sorted(ROOT.glob(cfg["leads_glob"]))
            lead_paths = [p for p in lead_paths if "REGISTRY_NOTES" not in p.name]
        elif cfg.get("leads"):
            if cfg["leads"].exists():
                lead_paths = [cfg["leads"]]
        if not lead_paths:
            print(f"{st}: no lead files found ({cfg.get('leads_glob') or cfg.get('leads')})",
                  file=sys.stderr)
            continue

        records: list[dict] = []
        seen: set[str] = set()
        for leads_path in lead_paths:
            file_records = 0
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
                    file_records += 1
            print(f"  [{st}] {leads_path.name}: +{file_records} records")

        print(f"[{st}] {len(records)} unique entities prepared "
              f"(from {len(lead_paths)} file{'s' if len(lead_paths) != 1 else ''})")

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
