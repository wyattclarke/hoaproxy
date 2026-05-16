#!/usr/bin/env python3
"""GCS-first rename + location enricher for AZ live HOAs.

Reads OCR sidecars directly from gs://hoaproxy-bank/v1/AZ/<county>/<slug>/
doc-<sha>/sidecar.json (which the orchestrator's Phase 5 already wrote) and
uses one DeepSeek prompt per HOA to extract:

  1. Canonical HOA name (clean up junk slugs)
  2. Address (street, city, state, postal_code)

Then applies via the live admin API:
  - POST /admin/rename-hoa for canonical-name promotions
  - POST /admin/backfill-locations for missing/upgradable location

This avoids the Hetzner rate limit on /hoas/{name}/documents/searchable —
the OCR text comes straight from GCS.

Usage:
  source .venv/bin/activate
  set -a; source settings.env; set +a
  python state_scrapers/az/scripts/az_gcs_rename_geo.py --apply [--limit N]

Idempotent: skips HOAs whose live record already has clean-looking name AND
location_quality in {address, polygon, place_centroid}.
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
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

# Config
BANK_PREFIX = "gs://hoaproxy-bank/v1/AZ"
LIVE_BASE = os.environ.get("HOAPROXY_LIVE_BASE_URL", "https://hoaproxy.org")
AZ_BBOX = (31.3, 37.0, -114.9, -109.0)  # min_lat, max_lat, min_lon, max_lon

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek/deepseek-v4-flash"
HERE_GEOCODE = "https://geocode.search.hereapi.com/v1/geocode"

# Dirty-name heuristics. ALL-CAPS alone is FINE (legit recorded titles often
# are). Only flag clearly malformed names — sentence fragments, leading
# numeric stamps, gov-url junk, prose markers.
DIRTY_PATTERNS = [
    re.compile(r"\b(addressed to|whereas|hereby|therefore|now therefore)\b", re.I),
    re.compile(r"^\s*\d{4,}\b"),  # starts with 4+ digits (year/resolution num)
    re.compile(r"\b(govgov|gov-gov|az\.gov|maricopa-az\.gov|govt-cs)\b", re.I),
    re.compile(r"\b(basis of|of and for|vis-a-vis|in re)\b", re.I),
    re.compile(r"\b(condominium homeowners?|townhouse townhomes?|villas villas)\b", re.I),
    re.compile(r"^[A-Z]\.\s"),  # "A. The properties..."
    re.compile(r"\bproperties now known as\b", re.I),
    re.compile(r"\bwhich association'?s funds\b", re.I),
    re.compile(r"\b(usei|usel|usef)\b"),  # OCR mangling of 'used'
    re.compile(r"\b(a plat of|legal description|page \d+ of \d+|exhibit [a-z])\b", re.I),
    re.compile(r"\bblk \d+ lots? \d+\b", re.I),  # plat-style refs
    re.compile(r"\bResolution No\.", re.I),
]


def is_dirty_name(name: str) -> tuple[bool, str]:
    if not name:
        return True, "empty"
    name = name.strip()
    if len(name) < 6:
        return True, "too_short"
    if len(name) > 110:
        return True, "too_long"
    # Too many words (>10) suggests sentence fragment
    if len(name.split()) > 10:
        return True, "too_many_words"
    for pat in DIRTY_PATTERNS:
        if pat.search(name):
            return True, f"matched:{pat.pattern[:40]}"
    return False, ""


def gcs_cat(uri: str) -> str | None:
    try:
        r = subprocess.run(
            ["gsutil", "cat", uri], capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return None
        return r.stdout
    except Exception:
        return None


def gcs_ls(prefix: str) -> list[str]:
    try:
        r = subprocess.run(
            ["gsutil", "ls", prefix], capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return []
        return [l.strip() for l in r.stdout.splitlines() if l.strip()]
    except Exception:
        return []


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s


_COUNTIES = (
    "maricopa", "pima", "pinal", "yavapai", "mohave", "yuma", "coconino",
    "cochise", "gila", "la-paz", "apache", "navajo", "greenlee", "graham",
    "santa-cruz", "unknown-county", "unresolved-name",
)


def build_bank_index(cache_path: Path | None = None) -> dict[str, str]:
    """One-time walk of gs://hoaproxy-bank/v1/AZ/ to build name/alias/slug
    → sidecar URI. SHA-aware: when two manifests share a PDF (same SHA), only
    one has a sidecar (OCR dedup); we map BOTH manifests to that sidecar.
    """
    if cache_path and cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except Exception:
            pass

    print("Building bank → sidecar index (SHA-aware)...")
    index: dict[str, str] = {}
    # Pass 1: build SHA → sidecar URI map (from manifests that have sidecars).
    sha_to_sidecar: dict[str, str] = {}
    all_manifests: list[tuple[str, str]] = []  # (manifest_uri, county)

    for county in _COUNTIES:
        print(f"  ls -r {county} ...", flush=True)
        r = subprocess.run(
            ["gsutil", "ls", "-r", f"{BANK_PREFIX}/{county}/"],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            continue
        side_count = 0
        manifest_count = 0
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.endswith("/manifest.json"):
                all_manifests.append((line, county))
                manifest_count += 1
            elif line.endswith("/sidecar.json"):
                # Extract 12-char SHA from /doc-<sha>/sidecar.json
                m = re.search(r"/doc-([0-9a-f]{12})/sidecar\.json$", line)
                if m:
                    sha = m.group(1)
                    if sha not in sha_to_sidecar:
                        sha_to_sidecar[sha] = line
                        side_count += 1
        print(f"    {county}: {manifest_count} manifests, {side_count} sidecars")

    print(f"\nPass 1 done: {len(sha_to_sidecar)} unique SHA→sidecar, {len(all_manifests)} manifests total")

    # Pass 2: for each manifest, get its docs, find which doc SHA has a sidecar.
    print("Pass 2: linking manifests to sidecars via SHA...")
    linked = 0
    for i, (manifest_uri, county) in enumerate(all_manifests):
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(all_manifests)} ({linked} linked)", flush=True)
        raw = gcs_cat(manifest_uri)
        if not raw:
            continue
        try:
            m = json.loads(raw)
        except Exception:
            continue
        docs = m.get("documents") or []
        # find any doc with a sidecar
        chosen_sidecar = None
        for d in docs:
            sha_full = (d.get("sha256") or "").lower()
            if not sha_full:
                continue
            sha_short = sha_full[:12]
            if sha_short in sha_to_sidecar:
                chosen_sidecar = sha_to_sidecar[sha_short]
                break
        if not chosen_sidecar:
            continue
        # Register manifest name, aliases, slug
        slug = manifest_uri.split("/")[-2]
        index[slug.lower()] = chosen_sidecar
        name = (m.get("name") or "").strip()
        if name:
            index[name.lower()] = chosen_sidecar
        for a in (m.get("name_aliases") or []):
            if a:
                index[a.lower()] = chosen_sidecar
        linked += 1

    print(f"Bank index size: {len(index)} keys ({linked} manifests linked to sidecars)")
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(index))
    return index


def find_sidecar_for_hoa(hoa_name: str, index: dict[str, str] | None = None) -> str | None:
    """Look up sidecar via the prebuilt name/alias/slug index."""
    if not index or not hoa_name:
        return None
    key = hoa_name.lower().strip()
    if key in index:
        return index[key]
    # Try slug
    slug = slugify(hoa_name).lower()
    if slug in index:
        return index[slug]
    return None


def read_sidecar_text(sidecar_uri: str, max_chars: int = 5000) -> str | None:
    raw = gcs_cat(sidecar_uri)
    if not raw:
        return None
    try:
        side = json.loads(raw)
    except Exception:
        return None
    pages = side.get("pages") or []
    if not pages:
        return None
    out = []
    for p in pages[:5]:  # first 5 pages
        t = p.get("text") or ""
        out.append(t)
        if sum(len(x) for x in out) >= max_chars:
            break
    return ("\n".join(out))[:max_chars]


def llm_extract(text: str, hoa_name_hint: str) -> dict | None:
    """Send sidecar text to DeepSeek; ask for canonical_name + location."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    prompt = f"""You are extracting structured data from an OCR'd Arizona HOA governing document.

The current name we have for this HOA is: "{hoa_name_hint}"

OCR text (first ~5000 chars):
---
{text}
---

Extract the following fields. Return ONLY valid JSON, no other text:
{{
  "canonical_name": "The recorded HOA name as it appears in the document title — e.g., 'Lakebrook Villas II Homeowners Association' — or null if the document doesn't clearly state an HOA name",
  "confidence_name": 0.0 to 1.0,
  "street": "Street address if mentioned (e.g., '100 Main St'), or null",
  "city": "City name in Arizona (must be a real AZ city), or null",
  "state": "AZ only — never another state",
  "postal_code": "5-digit ZIP (must be 85xxx-86xxx for AZ), or null",
  "is_arizona": true if the document is clearly about an Arizona HOA, false if it's about another state, null if unclear
}}

Important rules:
- Only return canonical_name if you find a clear recorded title like "DECLARATION OF COVENANTS FOR THE FOO HOMEOWNERS ASSOCIATION" or "ARTICLES OF INCORPORATION OF FOO HOMEOWNERS ASSOCIATION".
- Don't invent names from generic text like "the homeowners association" or "the association".
- The current name may be junk; trust the document text over the hint.
- For ZIP: only return 85xxx or 86xxx (Arizona). Reject any other ZIP."""

    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 400,
    }
    req = Request(
        OPENROUTER_URL,
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=80) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        try:
            return json.loads(content)
        except Exception:
            # Try to find JSON within text
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                return json.loads(m.group(0))
            return None
    except Exception:
        return None


def here_geocode(query: str, here_key: str) -> dict | None:
    params = {
        "q": query,
        "in": "countryCode:USA",
        "limit": 3,
        "apiKey": here_key,
    }
    from urllib.parse import urlencode
    url = f"{HERE_GEOCODE}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "hoaproxy/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        items = data.get("items") or []
        for item in items:
            addr = item.get("address") or {}
            pos = item.get("position") or {}
            # Only accept AZ results
            if (addr.get("stateCode") or "").upper() != "AZ":
                continue
            lat = pos.get("lat")
            lon = pos.get("lng")
            if lat is None or lon is None:
                continue
            if not (AZ_BBOX[0] <= lat <= AZ_BBOX[1] and AZ_BBOX[2] <= lon <= AZ_BBOX[3]):
                continue
            result_type = item.get("resultType") or "place"
            quality = "address" if result_type in ("houseNumber", "street") else "place_centroid"
            return {
                "latitude": lat,
                "longitude": lon,
                "street": addr.get("street") or addr.get("label"),
                "city": addr.get("city"),
                "state": "AZ",
                "postal_code": addr.get("postalCode"),
                "location_quality": quality,
            }
    except Exception:
        return None
    return None


def admin_post(path: str, body: dict, jwt: str) -> tuple[int, dict]:
    url = f"{LIVE_BASE}{path}"
    req = Request(
        url,
        method="POST",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {jwt}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", errors="replace")}
    except URLError as e:
        return 0, {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually call admin endpoints (default: dry-run)")
    p.add_argument("--limit", type=int, default=0,
                   help="Process only first N HOAs (0 = all)")
    p.add_argument("--live-cache", default="/tmp/az_live_full.json",
                   help="Path to cached live HOA list (paginated /hoas/summary)")
    p.add_argument("--out", default="state_scrapers/az/results/az_gcs_rename_geo_results.jsonl",
                   help="Output ledger")
    p.add_argument("--sleep-llm", type=float, default=1.5,
                   help="Delay between LLM calls")
    p.add_argument("--only-dirty", action="store_true",
                   help="Only process HOAs with dirty-looking names")
    p.add_argument("--only-missing-geo", action="store_true",
                   help="Only process HOAs missing location")
    args = p.parse_args()

    jwt = os.environ.get("JWT_SECRET")
    if not jwt:
        print("JWT_SECRET not set", file=sys.stderr)
        return 1
    here_key = os.environ.get("HERE_API_KEY")
    if not here_key:
        print("HERE_API_KEY not set", file=sys.stderr)
        return 1

    # Load live HOAs
    live_path = Path(args.live_cache)
    if not live_path.exists():
        print(f"Missing live cache at {live_path}; run paginated /hoas/summary first.")
        return 1
    live = json.loads(live_path.read_text())
    print(f"Live AZ HOAs: {len(live)}")

    # Build bank index (cached)
    index_cache = Path("state_scrapers/az/results/az_bank_sidecar_index.json")
    bank_index = build_bank_index(index_cache)

    # Filter
    todo = []
    for h in live:
        name = h.get("hoa", "")
        lat = h.get("latitude")
        lon = h.get("longitude")
        dirty, _ = is_dirty_name(name)
        has_good_geo = lat is not None and lon is not None
        if args.only_dirty and not dirty:
            continue
        if args.only_missing_geo and has_good_geo:
            continue
        if not args.only_dirty and not args.only_missing_geo:
            # process if dirty OR missing geo
            if not dirty and has_good_geo:
                continue
        todo.append(h)
    print(f"To process: {len(todo)}")
    if args.limit:
        todo = todo[:args.limit]
        print(f"  capped to {len(todo)}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rename_count = 0
    geo_count = 0
    no_sidecar = 0
    no_extract = 0

    with open(out_path, "w") as fh:
        for i, h in enumerate(todo):
            hoa_id = h.get("hoa_id")
            name = h.get("hoa", "")
            old_lat = h.get("latitude")
            old_lon = h.get("longitude")
            dirty, dirty_reason = is_dirty_name(name)

            sidecar = find_sidecar_for_hoa(name, bank_index)
            if not sidecar:
                no_sidecar += 1
                fh.write(json.dumps({
                    "hoa_id": hoa_id, "name": name, "status": "no_sidecar",
                    "dirty": dirty, "reason": dirty_reason,
                }) + "\n")
                if (i + 1) % 25 == 0:
                    print(f"  {i+1}/{len(todo)} | renames={rename_count} geos={geo_count} no_sidecar={no_sidecar}")
                continue

            text = read_sidecar_text(sidecar)
            if not text:
                fh.write(json.dumps({
                    "hoa_id": hoa_id, "name": name, "status": "no_text",
                }) + "\n")
                continue

            extracted = llm_extract(text, name)
            time.sleep(args.sleep_llm)
            if not extracted:
                no_extract += 1
                fh.write(json.dumps({
                    "hoa_id": hoa_id, "name": name, "status": "llm_failed",
                }) + "\n")
                continue

            entry: dict[str, Any] = {
                "hoa_id": hoa_id, "name": name, "sidecar": sidecar,
                "dirty": dirty, "extracted": extracted,
                "actions": [],
            }

            # Rename if dirty + LLM gave confident canonical_name
            new_name = (extracted.get("canonical_name") or "").strip()
            conf = float(extracted.get("confidence_name") or 0)
            if dirty and new_name and conf >= 0.6 and new_name.lower() != name.lower():
                if args.apply:
                    code, resp = admin_post("/admin/rename-hoa",
                                            {"hoa_id": hoa_id, "new_name": new_name,
                                             "dry_run": False}, jwt)
                    entry["actions"].append({"type": "rename", "http": code,
                                             "new_name": new_name, "resp": resp})
                    if code == 200:
                        rename_count += 1
                else:
                    entry["actions"].append({"type": "rename", "new_name": new_name, "dry": True})

            # Geocode if missing geo (or dirty geo)
            has_good_geo = old_lat is not None and old_lon is not None
            need_geo = not has_good_geo
            # Or check if out-of-bbox (we deleted 19 but more may slip in)
            if has_good_geo and not (AZ_BBOX[0] <= old_lat <= AZ_BBOX[1]
                                     and AZ_BBOX[2] <= old_lon <= AZ_BBOX[3]):
                need_geo = True

            if need_geo and extracted.get("is_arizona") is not False:
                # Build geocode query
                street = (extracted.get("street") or "").strip()
                city = (extracted.get("city") or "").strip()
                postal = (extracted.get("postal_code") or "").strip()
                if postal and (postal.startswith("85") or postal.startswith("86")):
                    if street and city:
                        q = f"{street}, {city}, AZ {postal}"
                    elif city:
                        q = f"{city}, AZ {postal}"
                    else:
                        q = f"{postal}, AZ"
                    geo = here_geocode(q, here_key)
                    if geo:
                        location_body = {"records": [{
                            "hoa": (new_name if rename_count and new_name else name),
                            **geo,
                        }]}
                        if args.apply:
                            code, resp = admin_post("/admin/backfill-locations",
                                                    location_body, jwt)
                            entry["actions"].append({"type": "backfill_location",
                                                     "http": code, "geo": geo,
                                                     "resp": resp})
                            if code == 200:
                                geo_count += 1
                        else:
                            entry["actions"].append({"type": "backfill_location",
                                                     "geo": geo, "dry": True})

            fh.write(json.dumps(entry) + "\n")
            if (i + 1) % 25 == 0:
                print(f"  {i+1}/{len(todo)} | renames={rename_count} geos={geo_count}"
                      f" no_sidecar={no_sidecar} no_extract={no_extract}")

    print(f"\n=== Summary ===")
    print(f"Processed: {len(todo)}")
    print(f"Renamed:   {rename_count}")
    print(f"Geocoded:  {geo_count}")
    print(f"No sidecar:{no_sidecar}")
    print(f"LLM fail:  {no_extract}")
    print(f"Ledger:    {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
