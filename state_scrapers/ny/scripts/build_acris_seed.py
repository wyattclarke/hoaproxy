#!/usr/bin/env python3
"""Build the NYC ACRIS condo-declaration seed (Driver E, Phase 1b).

Pulls every ACRIS Real Property Master row where ``doc_type='DECL'`` from
``data.cityofnewyork.us`` (Socrata, no auth), joins each document against
the Real Property Legals (one or more BBLs per declaration) and Real Property
Parties (the grantor — usually the condo association name or sponsor /
developer), filters to residential property types, and emits a per-building
seed for downstream banking and ingest.

There are ~18,911 DECL records total: Manhattan 9,212 / Brooklyn 4,105 /
Queens 3,980 / Bronx 1,614. Staten Island/Richmond County is **not** in
ACRIS — that county uses a separate clerk and must be served by Driver A.

Outputs (both JSONL, one record per line)
-----------------------------------------
1. ``state_scrapers/ny/leads/ny_acris_seed.jsonl`` — full record with all the
   metadata downstream needs (BBLs, recorded date, property type, etc.). Not
   loadable by ``probe-batch`` because it carries fields that aren't on the
   ``Lead`` dataclass.

2. ``state_scrapers/ny/leads/ny_acris_leads.jsonl`` — strict Lead-shape only
   (name / state / city / county / source / source_url / website), suitable
   for ``python -m hoaware.discovery probe-batch ...``.

Usage
-----

    # Smoke test: pull 50 Master rows, fully join, emit both files.
    python state_scrapers/ny/scripts/build_acris_seed.py --limit 50

    # Resume an interrupted full run.
    python state_scrapers/ny/scripts/build_acris_seed.py --resume

    # Full pull.
    python state_scrapers/ny/scripts/build_acris_seed.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[3]
LEADS_DIR = ROOT / "state_scrapers" / "ny" / "leads"
SEED_OUT = LEADS_DIR / "ny_acris_seed.jsonl"
LEADS_OUT = LEADS_DIR / "ny_acris_leads.jsonl"
PROGRESS = LEADS_DIR / "ny_acris_seed_progress.json"

MASTER_URL = "https://data.cityofnewyork.us/resource/bnx9-e6tj.json"
LEGALS_URL = "https://data.cityofnewyork.us/resource/8h5j-fqxa.json"
PARTIES_URL = "https://data.cityofnewyork.us/resource/636b-3b5g.json"

PAGE_SIZE = 1000  # Socrata default cap is 1000 when paginating
SLEEP_S = 0.2  # 5 req/sec polite cap

# ACRIS property-type codes. D-codes are condo/elevator-condo, R-codes are
# standard residential, RR/RP/RG are misc residential. Anything else (CR,
# offices, vacant land, etc.) is rejected because it's not the residential
# common-interest stock we're after.
RESIDENTIAL_PROPERTY_TYPES = frozenset(
    [
        "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D0",  # condos
        "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R0",  # residential
        "RR", "RP", "RG",  # misc residential
    ]
)

# Borough code -> (county name for our manifest, display city name).
# 5 / Richmond / Staten Island is included for completeness but ACRIS rarely
# carries it.
BOROUGH_META = {
    "1": ("New York", "Manhattan"),
    "2": ("Bronx", "Bronx"),
    "3": ("Kings", "Brooklyn"),
    "4": ("Queens", "Queens"),
    "5": ("Richmond", "Staten Island"),
}

SOURCE_LABEL = "ny-acris-decl-2026-05"
ACRIS_DOC_URL_FMT = "https://a836-acris.nyc.gov/CP/LookUp/Index?docId={doc_id}"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _socrata_get(url: str, params: dict) -> list[dict]:
    """GET a Socrata endpoint with retries and a polite sleep."""
    qs = urlencode(params, safe="(),'") + "&"  # don't quote SoQL punctuation
    full = f"{url}?{qs}"
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            req = Request(
                full,
                headers={
                    "User-Agent": "hoaproxy-ny-acris-seed/1.0",
                    "Accept": "application/json",
                },
            )
            with urlopen(req, timeout=120) as resp:
                data = resp.read()
            return json.loads(data.decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
            wait = (2 ** attempt) * 0.5
            print(
                f"  retry {attempt + 1}/4 after {wait:.1f}s ({type(exc).__name__}: {exc})",
                file=sys.stderr,
            )
            time.sleep(wait)
    assert last_err is not None
    raise last_err


def fetch_master_decls(start_offset: int, limit_total: int | None) -> Iterator[dict]:
    """Yield every DECL row from the Master, paginated."""
    offset = start_offset
    fetched_this_run = 0
    while True:
        if limit_total is not None and fetched_this_run >= limit_total:
            return
        page_size = PAGE_SIZE
        if limit_total is not None:
            page_size = min(PAGE_SIZE, limit_total - fetched_this_run)
        params = {
            "$where": "doc_type='DECL'",
            "$select": (
                "document_id,crfn,recorded_borough,doc_type,recorded_datetime"
            ),
            "$order": "document_id",  # stable pagination
            "$limit": str(page_size),
            "$offset": str(offset),
        }
        rows = _socrata_get(MASTER_URL, params)
        if not rows:
            return
        for row in rows:
            yield row
            fetched_this_run += 1
            if limit_total is not None and fetched_this_run >= limit_total:
                return
        if len(rows) < page_size:
            return
        offset += page_size
        time.sleep(SLEEP_S)


def fetch_legals_for(doc_ids: list[str]) -> list[dict]:
    """Fetch every Legals row for the given doc_ids."""
    if not doc_ids:
        return []
    quoted = ",".join(f"'{d}'" for d in doc_ids)
    out: list[dict] = []
    offset = 0
    while True:
        params = {
            "$where": f"document_id in ({quoted})",
            "$select": (
                "document_id,borough,block,lot,street_number,street_name,"
                "unit,property_type"
            ),
            "$order": "document_id,lot",
            "$limit": str(PAGE_SIZE),
            "$offset": str(offset),
        }
        rows = _socrata_get(LEGALS_URL, params)
        if not rows:
            break
        out.extend(rows)
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(SLEEP_S)
    return out


def fetch_parties_for(doc_ids: list[str]) -> list[dict]:
    """Fetch every party_type='1' (grantor) row for the given doc_ids."""
    if not doc_ids:
        return []
    quoted = ",".join(f"'{d}'" for d in doc_ids)
    out: list[dict] = []
    offset = 0
    while True:
        params = {
            "$where": f"document_id in ({quoted}) AND party_type='1'",
            "$select": (
                "document_id,party_type,name,address_1,country,city,state,zip"
            ),
            "$order": "document_id",
            "$limit": str(PAGE_SIZE),
            "$offset": str(offset),
        }
        rows = _socrata_get(PARTIES_URL, params)
        if not rows:
            break
        out.extend(rows)
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(SLEEP_S)
    return out


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------


def _safe_int(v: object) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 10**9  # push unparseable lots to the end


def _bbl(borough: object, block: object, lot: object) -> str | None:
    b = str(borough or "").strip()
    bk = str(block or "").strip()
    lt = str(lot or "").strip()
    if not (b and bk and lt):
        return None
    return f"{b}-{bk}-{lt}"


def _street_address(num: object, name: object) -> str:
    parts = []
    if num is not None and str(num).strip():
        parts.append(str(num).strip())
    if name is not None and str(name).strip():
        parts.append(str(name).strip())
    return " ".join(parts).strip()


def _recorded_date(raw: object) -> str | None:
    s = str(raw or "").strip()
    if not s:
        return None
    return s[:10]  # YYYY-MM-DD


def assemble_records(
    master_rows: list[dict],
    legals_by_doc: dict[str, list[dict]],
    parties_by_doc: dict[str, list[dict]],
) -> Iterator[tuple[dict, dict]]:
    """For each accepted Master row, yield (full_record, lead_only) pairs.

    Filters:
      - require at least one residential Legals row (D-/R-/RR/RP/RG codes)
      - require at least one party_type='1' with a non-empty name
    """
    for m in master_rows:
        doc_id = m.get("document_id")
        if not doc_id:
            continue
        legals = legals_by_doc.get(doc_id, [])
        # Keep only residential legals.
        residential = [
            l for l in legals
            if str(l.get("property_type", "")).strip().upper()
            in RESIDENTIAL_PROPERTY_TYPES
        ]
        if not residential:
            continue
        # Primary = lowest lot number on a residential row.
        residential.sort(
            key=lambda r: (
                _safe_int(r.get("lot")),
                _safe_int(r.get("block")),
            )
        )
        primary = residential[0]

        parties = parties_by_doc.get(doc_id, [])
        grantor = next(
            (p for p in parties if (p.get("name") or "").strip()),
            None,
        )
        if grantor is None:
            continue
        name = (grantor.get("name") or "").strip()
        if not name:
            continue

        borough = str(primary.get("borough") or m.get("recorded_borough") or "").strip()
        county_name, city_name = BOROUGH_META.get(borough, (None, None))

        bbl_primary = _bbl(primary.get("borough"), primary.get("block"), primary.get("lot"))
        bbls_all: list[str] = []
        for l in residential:
            bbl = _bbl(l.get("borough"), l.get("block"), l.get("lot"))
            if bbl and bbl not in bbls_all:
                bbls_all.append(bbl)

        street = _street_address(primary.get("street_number"), primary.get("street_name"))
        postal_code = (grantor.get("zip") or "").strip()[:5] or None
        property_type = (primary.get("property_type") or "").strip().upper()
        recorded_date = _recorded_date(m.get("recorded_datetime"))

        source_url = ACRIS_DOC_URL_FMT.format(doc_id=doc_id)

        # Strict Lead-shape — only the fields hoaware.discovery.Lead accepts.
        lead_only = {
            "name": name,
            "state": "NY",
            "city": city_name,
            "county": county_name,
            "source": SOURCE_LABEL,
            "source_url": source_url,
            "website": None,
        }
        # Full seed record carrying everything downstream needs.
        full = dict(lead_only)
        full.update(
            {
                "document_id": doc_id,
                "crfn": (m.get("crfn") or "").strip() or None,
                "bbl_primary": bbl_primary,
                "bbls_all": bbls_all,
                "street_address": street or None,
                "postal_code": postal_code,
                "recorded_date": recorded_date,
                "property_type": property_type or None,
            }
        )
        yield full, lead_only


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def _dedup_key(rec: dict) -> tuple[str, str]:
    """A single building keyed by (party name, primary BBL)."""
    return (
        (rec.get("name") or "").strip().upper(),
        (rec.get("bbl_primary") or "").strip(),
    )


def _is_older(a: dict, b: dict) -> bool:
    """Return True if record ``a`` is older than ``b`` (or b has no date)."""
    da = a.get("recorded_date") or ""
    db = b.get("recorded_date") or ""
    if da and db:
        return da < db
    # Prefer the one that has a date.
    if da and not db:
        return True
    return False


# ---------------------------------------------------------------------------
# Progress + I/O
# ---------------------------------------------------------------------------


def _load_progress() -> dict:
    if not PROGRESS.exists():
        return {}
    try:
        return json.loads(PROGRESS.read_text())
    except Exception:
        return {}


def _save_progress(p: dict) -> None:
    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(p, sort_keys=True, indent=2))
    tmp.replace(PROGRESS)


def _load_existing_full(path: Path) -> dict[tuple[str, str], dict]:
    """Re-read existing seed file (for resume) so dedup state is preserved."""
    out: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return out
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            out[_dedup_key(rec)] = rec
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--resume",
        action="store_true",
        help="Pick up from the last saved master_offset in the progress file.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap on Master rows to fetch this run (smoke test). Default: no cap.",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=SEED_OUT,
        help=f"Output path for the full seed (default: {SEED_OUT}).",
    )
    p.add_argument(
        "--leads-out",
        type=Path,
        default=LEADS_OUT,
        help=f"Output path for the lead-only file (default: {LEADS_OUT}).",
    )
    p.add_argument(
        "--master-batch",
        type=int,
        default=200,
        help=(
            "How many Master doc_ids to join against Legals/Parties per round. "
            "Default 200 (URL length is the constraint)."
        ),
    )
    args = p.parse_args(argv)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.leads_out.parent.mkdir(parents=True, exist_ok=True)

    progress = _load_progress() if args.resume else {}
    start_offset = int(progress.get("master_offset", 0)) if args.resume else 0
    counters = {
        "master_fetched": int(progress.get("master_fetched", 0)),
        "with_residential_legals": int(progress.get("with_residential_legals", 0)),
        "with_grantor": int(progress.get("with_grantor", 0)),
        "leads_emitted": int(progress.get("leads_emitted", 0)),
        "duplicates_collapsed": int(progress.get("duplicates_collapsed", 0)),
    }

    # On a non-resume run we start fresh; on resume we keep existing dedup state.
    if args.resume:
        full_by_key = _load_existing_full(args.out)
    else:
        full_by_key = {}

    print(
        f"start_offset={start_offset} limit={args.limit} "
        f"existing_records={len(full_by_key)}",
        file=sys.stderr,
    )

    batch: list[dict] = []
    last_logged = 0

    def _flush_batch() -> None:
        nonlocal batch
        if not batch:
            return
        doc_ids = [r["document_id"] for r in batch if r.get("document_id")]
        legals = fetch_legals_for(doc_ids)
        parties = fetch_parties_for(doc_ids)
        legals_by_doc: dict[str, list[dict]] = {}
        for l in legals:
            legals_by_doc.setdefault(l["document_id"], []).append(l)
        parties_by_doc: dict[str, list[dict]] = {}
        for pa in parties:
            parties_by_doc.setdefault(pa["document_id"], []).append(pa)

        # Stats
        for r in batch:
            did = r.get("document_id")
            if not did:
                continue
            has_res = any(
                str(l.get("property_type", "")).strip().upper()
                in RESIDENTIAL_PROPERTY_TYPES
                for l in legals_by_doc.get(did, [])
            )
            if has_res:
                counters["with_residential_legals"] += 1
            if any((p.get("name") or "").strip() for p in parties_by_doc.get(did, [])):
                counters["with_grantor"] += 1

        for full, _lead in assemble_records(batch, legals_by_doc, parties_by_doc):
            key = _dedup_key(full)
            existing = full_by_key.get(key)
            if existing is None:
                full_by_key[key] = full
            else:
                # Prefer the older record (original declaration).
                counters["duplicates_collapsed"] += 1
                if _is_older(full, existing):
                    full_by_key[key] = full
        batch = []

    try:
        for master_row in fetch_master_decls(start_offset, args.limit):
            batch.append(master_row)
            counters["master_fetched"] += 1
            if len(batch) >= args.master_batch:
                _flush_batch()
            if counters["master_fetched"] - last_logged >= 1000:
                last_logged = counters["master_fetched"]
                print(
                    f"  master_fetched={counters['master_fetched']} "
                    f"residential={counters['with_residential_legals']} "
                    f"grantor={counters['with_grantor']} "
                    f"unique={len(full_by_key)} "
                    f"dups_collapsed={counters['duplicates_collapsed']}",
                    file=sys.stderr,
                )
                # Persist progress to allow resume mid-run.
                _save_progress(
                    {
                        "master_offset": start_offset + counters["master_fetched"],
                        **counters,
                        "leads_emitted": len(full_by_key),
                    }
                )
        # Flush trailing batch.
        _flush_batch()
    except KeyboardInterrupt:
        print("interrupted; flushing partial batch...", file=sys.stderr)
        _flush_batch()

    # Write outputs (full overwrite — dedup state is in memory).
    counters["leads_emitted"] = len(full_by_key)
    with args.out.open("w") as f_full, args.leads_out.open("w") as f_lead:
        for rec in sorted(
            full_by_key.values(),
            key=lambda r: (r.get("bbl_primary") or "", r.get("name") or ""),
        ):
            f_full.write(json.dumps(rec, sort_keys=True) + "\n")
            lead_only = {
                "name": rec.get("name"),
                "state": rec.get("state"),
                "city": rec.get("city"),
                "county": rec.get("county"),
                "source": rec.get("source"),
                "source_url": rec.get("source_url"),
                "website": rec.get("website"),
            }
            f_lead.write(json.dumps(lead_only, sort_keys=True) + "\n")

    _save_progress(
        {
            "master_offset": start_offset + counters["master_fetched"],
            **counters,
        }
    )

    print(
        json.dumps(
            {
                "master_fetched": counters["master_fetched"],
                "with_residential_legals": counters["with_residential_legals"],
                "with_grantor": counters["with_grantor"],
                "duplicates_collapsed": counters["duplicates_collapsed"],
                "leads_emitted": counters["leads_emitted"],
                "out": str(args.out),
                "leads_out": str(args.leads_out),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
