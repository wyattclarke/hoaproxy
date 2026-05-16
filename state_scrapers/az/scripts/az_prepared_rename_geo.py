#!/usr/bin/env python3
"""GCS-first rename + location enricher using prepared-bundle OCR texts.

Reads OCR text JSON from gs://hoaproxy-ingest-ready/v1/AZ/<county>/<slug>/
<bundle_hash>/texts/<sha>.json — these are written by prepare_bank for EVERY
prepared HOA (one per document). Coverage is ~763 of 1,758 live AZ HOAs.

For each HOA: bundle.json → texts/<sha>.json → DeepSeek (extract canonical
name + AZ city/zip) → admin rename/backfill/delete.

Usage:
  source .venv/bin/activate
  set -a; source settings.env; set +a
  python state_scrapers/az/scripts/az_prepared_rename_geo.py --apply [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

INGEST_PREFIX = "gs://hoaproxy-ingest-ready/v1/AZ"
LIVE_BASE = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")
AZ_BBOX = (31.3, 37.0, -114.9, -109.0)
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek/deepseek-v4-flash"
HERE_GEOCODE = "https://geocode.search.hereapi.com/v1/geocode"

DIRTY_PATTERNS = [
    re.compile(r"\b(addressed to|whereas|hereby|therefore|now therefore)\b", re.I),
    re.compile(r"^\s*\d{4,}\b"),
    re.compile(r"\b(govgov|gov-gov|az\.gov|maricopa-az\.gov)\b", re.I),
    re.compile(r"\b(basis of|of and for|vis-a-vis|in re)\b", re.I),
    re.compile(r"\b(condominium homeowners?|townhouse townhomes?)\b", re.I),
    re.compile(r"^[A-Z]\.\s"),
    re.compile(r"\bproperties now known as\b", re.I),
    re.compile(r"\bwhich association'?s funds\b", re.I),
    re.compile(r"\b(usei|usef)\b"),
    re.compile(r"\b(a plat of|legal description|page \d+ of \d+|exhibit [a-z])\b", re.I),
    re.compile(r"\bblk \d+ lots? \d+\b", re.I),
    re.compile(r"\bResolution No\.", re.I),
    re.compile(r"\b(article [iv]+|article \d+)\b", re.I),
]


def is_dirty_name(name: str) -> tuple[bool, str]:
    if not name:
        return True, "empty"
    name = name.strip()
    if len(name) < 6: return True, "too_short"
    if len(name) > 110: return True, "too_long"
    if len(name.split()) > 10: return True, "too_many_words"
    for pat in DIRTY_PATTERNS:
        if pat.search(name):
            return True, f"matched:{pat.pattern[:40]}"
    return False, ""


def gcs_cat(uri: str) -> str | None:
    try:
        r = subprocess.run(["gsutil", "cat", uri], capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def gcs_ls(prefix: str) -> list[str]:
    try:
        r = subprocess.run(["gsutil", "ls", prefix], capture_output=True, text=True, timeout=60)
        return [l.strip() for l in (r.stdout or "").splitlines() if l.strip()] if r.returncode == 0 else []
    except Exception:
        return []


def list_prepared_bundles() -> list[dict]:
    """Bulk-list all prepared bundle.json paths via one gsutil ls -r per county."""
    print("Listing prepared bundles (bulk ls -r)...")
    bundles = []
    counties = gcs_ls(f"{INGEST_PREFIX}/")
    for county_uri in counties:
        if not county_uri.endswith("/"):
            continue
        county = county_uri.rstrip("/").rsplit("/", 1)[-1]
        r = subprocess.run(
            ["gsutil", "ls", "-r", county_uri],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.endswith("/bundle.json"):
                # Parse slug from path
                parts = line.split("/")
                # gs://hoaproxy-ingest-ready/v1/AZ/<county>/<slug>/<hash>/bundle.json
                try:
                    az_idx = parts.index("AZ")
                    slug = parts[az_idx + 2]
                    bundles.append({"county": county, "slug": slug, "bundle_uri": line})
                except (ValueError, IndexError):
                    continue
        print(f"  {county}: {sum(1 for b in bundles if b['county']==county)} bundles")
    print(f"Total: {len(bundles)} prepared bundles")
    return bundles


def llm_extract(text: str, hoa_name_hint: str) -> dict | None:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    prompt = f"""Extract structured data from OCR'd HOA governing document text.

Current HOA name: "{hoa_name_hint}"

OCR text (first 5000 chars):
---
{text[:5000]}
---

Return ONLY valid JSON:
{{
  "canonical_name": "Exact recorded HOA name from doc title (e.g., 'Lakebrook Villas II Homeowners Association') or null if doc doesn't clearly state one",
  "confidence_name": 0.0 to 1.0,
  "street": "Street address from doc, or null",
  "city": "AZ city if mentioned, else null",
  "postal_code": "AZ ZIP (85xxx/86xxx only) or null",
  "is_arizona": true if doc is clearly about AZ HOA, false if other state, null if unclear,
  "other_state": "Two-letter state if doc is clearly about another state (e.g., 'TX', 'NV'), or null"
}}"""
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 400,
    }
    req = Request(OPENROUTER_URL, method="POST",
                  data=json.dumps(body).encode("utf-8"),
                  headers={"Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=80) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        try:
            return json.loads(content)
        except Exception:
            m = re.search(r"\{.*\}", content, re.DOTALL)
            return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def here_geocode(query: str, here_key: str) -> dict | None:
    params = {"q": query, "in": "countryCode:USA", "limit": 3, "apiKey": here_key}
    req = Request(f"{HERE_GEOCODE}?{urlencode(params)}",
                  headers={"User-Agent": "hoaproxy/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        for item in data.get("items") or []:
            addr = item.get("address") or {}
            pos = item.get("position") or {}
            if (addr.get("stateCode") or "").upper() != "AZ":
                continue
            lat = pos.get("lat"); lon = pos.get("lng")
            if lat is None or lon is None: continue
            if not (AZ_BBOX[0] <= lat <= AZ_BBOX[1] and AZ_BBOX[2] <= lon <= AZ_BBOX[3]):
                continue
            qual = "address" if item.get("resultType") in ("houseNumber", "street") else "place_centroid"
            return {
                "latitude": lat, "longitude": lon,
                "street": addr.get("street") or addr.get("label"),
                "city": addr.get("city"), "state": "AZ",
                "postal_code": addr.get("postalCode"),
                "location_quality": qual,
            }
    except Exception:
        return None
    return None


def admin_post(path: str, body: dict, jwt: str) -> tuple[int, dict]:
    req = Request(f"{LIVE_BASE}{path}", method="POST",
                  data=json.dumps(body).encode("utf-8"),
                  headers={"Authorization": f"Bearer {jwt}",
                           "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, {"error": (e.read() or b"").decode("utf-8", errors="replace")[:200]}
    except Exception as e:
        return 0, {"error": str(e)[:200]}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--out", default="state_scrapers/az/results/az_prepared_rename_geo.jsonl")
    p.add_argument("--sleep", type=float, default=1.5)
    p.add_argument("--live-cache", default="/tmp/az_live_full.json")
    p.add_argument("--bundles-cache", default="state_scrapers/az/results/az_prepared_bundles.json")
    args = p.parse_args()

    jwt = os.environ.get("JWT_SECRET")
    here = os.environ.get("HERE_API_KEY")
    if not jwt or not here:
        print("JWT_SECRET + HERE_API_KEY required", file=sys.stderr)
        return 1

    live_path = Path(args.live_cache)
    if not live_path.exists():
        print(f"missing {live_path}", file=sys.stderr)
        return 1
    live = json.loads(live_path.read_text())
    live_by_name = {h["hoa"].lower(): h for h in live if h.get("hoa")}
    print(f"Live AZ HOAs: {len(live)}")

    # Cache prepared bundle list
    bundles_cache = Path(args.bundles_cache)
    if bundles_cache.exists():
        bundles = json.loads(bundles_cache.read_text())
        print(f"Loaded {len(bundles)} bundles from cache")
    else:
        bundles = list_prepared_bundles()
        bundles_cache.parent.mkdir(parents=True, exist_ok=True)
        bundles_cache.write_text(json.dumps(bundles))

    if args.limit:
        bundles = bundles[:args.limit]
        print(f"Capped to {len(bundles)}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    renamed = 0; geo_d = 0; deleted = 0; skipped = 0
    with open(out, "w") as fh:
        for i, b in enumerate(bundles):
            bundle = gcs_cat(b["bundle_uri"])
            if not bundle:
                skipped += 1; continue
            try:
                bdata = json.loads(bundle)
            except Exception:
                skipped += 1; continue
            hoa_name = bdata.get("hoa_name") or ""
            docs = bdata.get("documents") or []
            if not docs:
                skipped += 1; continue
            text_path = docs[0].get("text_gcs_path")
            if not text_path:
                skipped += 1; continue
            text_raw = gcs_cat(text_path)
            if not text_raw:
                skipped += 1; continue
            try:
                tdata = json.loads(text_raw)
            except Exception:
                skipped += 1; continue
            pages = tdata.get("pages") or []
            full_text = "\n".join((p.get("text") or "") for p in pages[:5])
            if not full_text.strip():
                skipped += 1; continue

            # Look up live HOA by bundle.hoa_name
            live_h = live_by_name.get(hoa_name.lower())
            dirty, dirty_reason = is_dirty_name(hoa_name)

            extracted = llm_extract(full_text, hoa_name)
            time.sleep(args.sleep)

            # If no live match by bundle name, try LLM's canonical
            if not live_h and extracted:
                cn = (extracted.get("canonical_name") or "").strip().lower()
                if cn and cn in live_by_name:
                    live_h = live_by_name[cn]
            # Try without leading junk
            if not live_h and extracted:
                cn = (extracted.get("canonical_name") or "").strip()
                # Try case-insensitive substring match in live names
                for lname, lhoa in live_by_name.items():
                    if cn and cn.lower() == lname:
                        live_h = lhoa; break

            hoa_id = live_h.get("hoa_id") if live_h else None
            current_live_name = live_h.get("hoa") if live_h else hoa_name
            old_lat = live_h.get("latitude") if live_h else None
            old_lon = live_h.get("longitude") if live_h else None

            entry = {"bundle": b, "hoa_name": hoa_name,
                     "live_name": current_live_name, "hoa_id": hoa_id,
                     "dirty": dirty, "extracted": extracted, "actions": []}

            if not extracted:
                fh.write(json.dumps(entry) + "\n"); continue

            # Cross-state? delete
            other_state = (extracted.get("other_state") or "").upper()
            if extracted.get("is_arizona") is False and other_state and other_state != "AZ" and hoa_id:
                if args.apply:
                    code, resp = admin_post("/admin/delete-hoa",
                                            {"hoa_ids": [hoa_id], "dry_run": False}, jwt)
                    entry["actions"].append({"type": "delete_cross_state",
                                             "other_state": other_state, "http": code})
                    if code == 200:
                        deleted += 1
                else:
                    entry["actions"].append({"type": "delete_cross_state",
                                             "other_state": other_state, "dry": True})
                fh.write(json.dumps(entry) + "\n")
                if (i+1) % 25 == 0:
                    print(f"  {i+1}/{len(bundles)} | rename={renamed} geo={geo_d} del={deleted}")
                continue

            # Rename if (live name is dirty OR LLM very confident) + the name differs
            new_name = (extracted.get("canonical_name") or "").strip()
            conf = float(extracted.get("confidence_name") or 0)
            current_for_geo = current_live_name
            live_dirty, _ = is_dirty_name(current_live_name)
            # Accept rename if live name is dirty OR LLM is very confident the proper name differs
            should_rename = (
                hoa_id
                and new_name
                and new_name.lower() != current_live_name.lower()
                and (
                    (live_dirty and conf >= 0.6)
                    or conf >= 0.9
                )
            )
            if should_rename:
                if args.apply:
                    code, resp = admin_post("/admin/rename-hoa",
                                            {"hoa_id": hoa_id, "new_name": new_name,
                                             "dry_run": False}, jwt)
                    entry["actions"].append({"type": "rename", "new_name": new_name,
                                             "http": code, "resp": resp})
                    if code == 200:
                        renamed += 1
                        current_for_geo = new_name
                else:
                    entry["actions"].append({"type": "rename", "new_name": new_name, "dry": True})
                time.sleep(0.5)

            # Geo if missing
            need_geo = (old_lat is None or old_lon is None) or (
                old_lat and old_lon and not (AZ_BBOX[0] <= old_lat <= AZ_BBOX[1]
                                              and AZ_BBOX[2] <= old_lon <= AZ_BBOX[3]))
            if need_geo:
                pc = (extracted.get("postal_code") or "").strip()
                city = (extracted.get("city") or "").strip()
                street = (extracted.get("street") or "").strip()
                if pc and (pc.startswith("85") or pc.startswith("86")):
                    if street and city:
                        q = f"{street}, {city}, AZ {pc}"
                    elif city:
                        q = f"{city}, AZ {pc}"
                    else:
                        q = f"{pc}, AZ"
                    geo = here_geocode(q, here)
                    if geo:
                        if args.apply:
                            code, resp = admin_post("/admin/backfill-locations",
                                                    {"records": [{"hoa": current_for_geo, **geo}]}, jwt)
                            entry["actions"].append({"type": "backfill", "http": code,
                                                     "geo": geo, "resp": resp})
                            if code == 200:
                                geo_d += 1
                        else:
                            entry["actions"].append({"type": "backfill", "geo": geo, "dry": True})

            fh.write(json.dumps(entry) + "\n")
            if (i+1) % 25 == 0:
                print(f"  {i+1}/{len(bundles)} | rename={renamed} geo={geo_d} del={deleted} skip={skipped}")

    print(f"\n=== Summary ===")
    print(f"Bundles processed: {len(bundles)}")
    print(f"Renamed:           {renamed}")
    print(f"Geocoded:          {geo_d}")
    print(f"Deleted (state):   {deleted}")
    print(f"Skipped:           {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
