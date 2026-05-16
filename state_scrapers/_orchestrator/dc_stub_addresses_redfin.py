#!/usr/bin/env python3
"""Redfin-via-Serper address backfill — second pass after the Nominatim
backfill. For DC HOAs that still don't have a street address (typically
named buildings like "The Watergate" where Nominatim's reverse-geocode
of the polygon centroid landed on a trail / neighboring street), search
Redfin/Zillow/Trulia listings for the building name and extract the
address from listing URLs.

Redfin listing URL pattern:
  https://www.redfin.com/DC/Washington/<DASH-SEP-ADDR>-<ZIP>/unit-N/home/N
The dash-separated address segment encodes street_num + street + ZIP,
which we parse without scraping the listing page itself (so we stay on
the public SERP layer and don't trigger Redfin anti-bot).

Zillow URL pattern:
  https://www.zillow.com/homedetails/<DASH-SEP-ADDR>-<ZIP>/<id>_zpid/
Trulia:
  https://www.trulia.com/p/dc/washington/<DASH-SEP-ADDR>-<ZIP>--<id>

For each condo:
  1. Serper query: `"<name>" "Washington" "DC" site:redfin.com OR site:zillow.com OR site:trulia.com`
  2. Parse the first result URL for address
  3. Optionally verify via the result snippet (mentions the condo name)
  4. POST update via /admin/create-stub-hoas
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
SERPER_ENDPOINT = "https://google.serper.dev/search"

DC_BBOX = {"min_lat": 38.79, "max_lat": 39.00, "min_lon": -77.12, "max_lon": -76.91}

REDFIN_URL_RE = re.compile(
    r"redfin\.com/(?:DC|MD|VA)/[^/]+/([A-Za-z0-9-]+?)-(\d{5})(?:/|\b)",
    re.IGNORECASE,
)
ZILLOW_URL_RE = re.compile(
    r"zillow\.com/(?:homedetails|b)/([A-Za-z0-9-]+?)-(\d{5})/",
    re.IGNORECASE,
)
TRULIA_URL_RE = re.compile(
    r"trulia\.com/p/(?:dc|md|va)/[^/]+/([A-Za-z0-9-]+?)-(\d{5})--",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def live_admin_token() -> str | None:
    # Explicit override wins; otherwise pull from settings.env.
    # Render env-vars fallback removed 2026-05-16 (Hetzner cutover).
    return os.environ.get("HOAPROXY_ADMIN_BEARER") or os.environ.get("JWT_SECRET")


def parse_listing_url(url: str) -> dict | None:
    """Returns {street, postal_code} or None."""
    for rgx in (REDFIN_URL_RE, ZILLOW_URL_RE, TRULIA_URL_RE):
        m = rgx.search(url)
        if not m:
            continue
        slug, zip_ = m.group(1), m.group(2)
        # Slug looks like "1906-Biltmore-St-NW" → street: "1906 Biltmore St NW"
        parts = slug.split("-")
        if not parts or not parts[0].isdigit():
            continue
        # Title-case street components, preserve all-caps quadrants
        street_parts = [parts[0]]
        for p in parts[1:]:
            if p.upper() in {"NW", "NE", "SW", "SE", "N", "S", "E", "W"}:
                street_parts.append(p.upper())
            elif p.lower() in {"st", "ave", "rd", "blvd", "ln", "dr", "ct", "pl", "way", "pkwy", "ter"}:
                # Standard street suffix abbreviations — capitalize and add period? Just title-case
                street_parts.append(p.title())
            else:
                street_parts.append(p.title())
        return {"street": " ".join(street_parts), "postal_code": zip_}
    return None


def serper_search(query: str, api_key: str, *, num: int = 5) -> list[dict]:
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {"q": query, "num": num, "gl": "us", "hl": "en"}
    r = requests.post(SERPER_ENDPOINT, headers=headers, json=payload, timeout=20)
    if r.status_code >= 400:
        return []
    return list(r.json().get("organic", []))


def fetch_dc_hoas_no_street(base_url: str) -> list[dict]:
    """Fetch DC HOAs whose location_quality is 'city_only' OR whose summary
    row implies no street is set. Since /hoas/summary doesn't return the
    street directly, we fetch all DC HOAs and let the caller decide."""
    out: list[dict] = []
    offset = 0
    while True:
        r = requests.get(f"{base_url}/hoas/summary", params={
            "state": "DC", "limit": 500, "offset": offset,
        }, timeout=60)
        if r.status_code != 200:
            break
        body = r.json()
        results = body.get("results") or []
        if not results:
            break
        out.extend(results)
        if len(results) < 500:
            break
        offset += 500
        if offset > 10000:
            break
    return out


def post_update(records: list[dict], base_url: str, token: str) -> dict:
    try:
        r = requests.post(
            f"{base_url}/admin/create-stub-hoas",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"records": records},
            timeout=300,
        )
        return {"status": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:500]}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org"))
    parser.add_argument("--ledger", default=str(ROOT / f"state_scrapers/dc/results/dc_stub_addresses_redfin_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jsonl"))
    parser.add_argument("--limit", type=int, default=0, help="0 = all")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delay", type=float, default=0.2, help="Per-Serper-query delay")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--only-without-street-clue",
                        action="store_true",
                        help="Skip HOAs whose name already contains a street number (those are the Nominatim pass's job)")
    args = parser.parse_args()

    token = live_admin_token()
    if not token:
        print("FATAL: no admin token", file=sys.stderr)
        return 2
    serper_key = os.environ.get("SERPER_API_KEY")
    if not serper_key:
        print("FATAL: SERPER_API_KEY missing", file=sys.stderr)
        return 2

    ledger_path = Path(args.ledger)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    rows = fetch_dc_hoas_no_street(args.base)
    print(f"Fetched {len(rows)} DC HOAs", file=sys.stderr)

    # Filter: only the ones whose names DON'T contain a street number prefix
    # (Nominatim pass already covered those well)
    needing: list[dict] = []
    name_starts_with_num_re = re.compile(r"^[#]?\s*\d+\s+")
    for r in rows:
        name = (r.get("hoa") or "").strip()
        if not name:
            continue
        if args.only_without_street_clue and name_starts_with_num_re.match(name):
            continue
        needing.append(r)
    if args.limit:
        needing = needing[: args.limit]
    print(f"Will attempt Redfin/Zillow/Trulia geocode on {len(needing)} HOAs", file=sys.stderr)

    pending: list[dict] = []
    counts = {"hits": 0, "misses": 0, "no_listing_url": 0, "updated": 0, "errors": 0}

    t0 = time.time()
    last_report = t0
    for i, r in enumerate(needing):
        name = (r.get("hoa") or "").strip()
        # Try a few query variants
        queries = [
            f'"{name}" "Washington" "DC" (site:redfin.com OR site:zillow.com OR site:trulia.com)',
            f'"{name}" "Washington DC" condominium site:redfin.com',
        ]
        addr_match: dict | None = None
        snippet_evidence = ""
        for q in queries:
            results = serper_search(q, serper_key, num=5)
            time.sleep(args.delay)
            for hit in results:
                link = hit.get("link") or ""
                snippet = hit.get("snippet") or ""
                title = hit.get("title") or ""
                parsed = parse_listing_url(link)
                if not parsed:
                    continue
                # Light verification: snippet/title should mention some name substring
                # (sanity check — listings often say "Building Name: <CONDO>")
                anchor_words = [w for w in re.findall(r"[A-Z][A-Za-z]{3,}", name) if w.lower() not in {"condo","condominium","association","unit","owners"}]
                if anchor_words:
                    hay = (snippet + " " + title).lower()
                    if any(w.lower() in hay for w in anchor_words):
                        addr_match = parsed
                        snippet_evidence = (title + " | " + snippet)[:200]
                        break
                else:
                    addr_match = parsed
                    snippet_evidence = (title + " | " + snippet)[:200]
                    break
            if addr_match:
                break

        decision = "miss"
        if addr_match:
            counts["hits"] += 1
            decision = "hit"
            record = {
                "name": name,
                "metadata_type": (r.get("metadata_type") or "condo"),
                "city": "Washington",
                "state": "DC",
                "street": addr_match["street"],
                "postal_code": addr_match["postal_code"],
                "source": "real-estate-listing-via-serper",
            }
            # Promote location_quality to address only if existing centroid is preserved
            if r.get("latitude") and r.get("longitude"):
                record["location_quality"] = "address"
            pending.append(record)
            with ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"name": name, "decision": decision, "addr": addr_match, "evidence": snippet_evidence}, sort_keys=True) + "\n")
        else:
            counts["misses"] += 1
            with ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"name": name, "decision": decision}, sort_keys=True) + "\n")

        if len(pending) >= args.batch_size:
            if args.apply:
                result = post_update(pending, args.base, token)
                with ledger_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"event": "batch", "size": len(pending), "result": result}, sort_keys=True) + "\n")
                if result.get("status") != 200:
                    counts["errors"] += 1
                else:
                    counts["updated"] += result.get("body", {}).get("updated", 0)
            pending = []

        now = time.time()
        if now - last_report >= 30:
            last_report = now
            rate = (i + 1) / max(0.001, now - t0)
            eta_min = (len(needing) - i - 1) / max(0.001, rate) / 60
            print(
                f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                f"{i+1}/{len(needing)} ({rate:.2f}/s ETA {eta_min:.1f}m) "
                f"hits={counts['hits']} misses={counts['misses']} updated={counts['updated']}",
                file=sys.stderr, flush=True,
            )

    if pending and args.apply:
        result = post_update(pending, args.base, token)
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "batch_final", "size": len(pending), "result": result}, sort_keys=True) + "\n")
        counts["updated"] += result.get("body", {}).get("updated", 0)

    summary = {
        "started_at": now_iso(),
        "wall_seconds": round(time.time() - t0, 1),
        "counts": counts,
        "applied": args.apply,
        "ledger": str(ledger_path),
    }
    with ledger_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "summary", **summary}, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
