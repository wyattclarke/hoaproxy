#!/usr/bin/env python3
"""Strict post-filter on Chicagoland seed JSONL + merge in existing-bank names.

Reads:
  - state_scrapers/il/leads/il_chicagoland_seed.jsonl (raw harvest)
  - existing v1/IL/{cook,dupage,lake,will,kane,mchenry,kendall}/ bank manifests

Writes:
  - state_scrapers/il/leads/il_chicagoland_seed_clean.jsonl

Drops entities that:
  - Don't end with a recognized entity suffix
  - Have <2 alpha words before suffix (or <1 alpha + a digit prefix)
  - Start with a generic article/verb (A, An, The, About, Top, Your, Our, ...)
  - Contain "...", "…", pipes, bullets
  - Match a generic-geo + suffix pattern (Chicago Condominium, etc.)
  - Contain a sentence-boundary period mid-name
  - Contain a recognized mgmt-co name inside the entity name proper
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

ENTITY_SUFFIX_RE = re.compile(
    r"\b("
    r"condominium\s+association(?:,?\s+inc\.?)?|"
    r"condominium(?:s)?(?:,?\s+inc\.?)?|"
    r"homeowners?\s+association(?:,?\s+inc\.?)?|"
    r"home\s+owners?\s+association(?:,?\s+inc\.?)?|"
    r"property\s+owners?\s+association(?:,?\s+inc\.?)?|"
    r"townhome(?:s)?\s+association(?:,?\s+inc\.?)?|"
    r"townhouse(?:s)?\s+association(?:,?\s+inc\.?)?|"
    r"unit\s+owners?\s+association(?:,?\s+inc\.?)?|"
    r"community\s+association(?:,?\s+inc\.?)?|"
    r"master\s+association(?:,?\s+inc\.?)?|"
    r"cooperative(?:,?\s+inc\.?)?|"
    r"co-?op\s+association|"
    r"owners?\s+association(?:,?\s+inc\.?)?"
    r")\s*\.?$",
    re.IGNORECASE,
)

GENERIC_GEO_PREFIX_RE = re.compile(
    r"^("
    r"chicago(?:land|[-\s]area)?|illinois|il|cook|cook\s+county|"
    r"gold\s+coast|loop|west\s+loop|south\s+loop|north\s+side|"
    r"lincoln\s+park|lakeview|streeterville|downtown|midwest|evanston|"
    r"oak\s+park|naperville|schaumburg|"
    r"a|an|the|your|our|are|is|to|from|with|by|of|that|this|about|top|"
    r"master[-\s]planned|sub|new|expanded|expanding|expands?|"
    r"premier|select|home\s+type|adding|add|expand|expanded\s+local|"
    r"local\s+client\s+portfolio"
    r")\s+("
    r"condominium(?:\s+association)?|"
    r"homeowners?\s+association|"
    r"home\s+owners?\s+association|"
    r"community\s+association|"
    r"property\s+owners?\s+association|"
    r"townhome(?:s)?\s+association|"
    r"townhouse(?:s)?\s+association|"
    r"unit\s+owners?\s+association|"
    r"master\s+association|"
    r"owners?\s+association|"
    r"condo\s+association"
    r")$",
    re.IGNORECASE,
)

LEADING_BAD_RE = re.compile(
    r"^(a|an|the|your|our|are|is|to|from|with|by|of|that|this|"
    r"about|top|expand(?:ed)?|adding|add|new|recent(?:ly)?|"
    r"applicant|respondent|petitioner|plaintiff|defendant)\s+",
    re.IGNORECASE,
)

MGMT_CO_INSIDE_RE = re.compile(
    r"\b("
    r"firstservice|foster\s+premier|associa|lieberman|vanguard|"
    r"habitat\s+company|acm\s+community|inland\s+residential|klein|"
    r"wolin[-\s]?levin|heil|draper\s+and\s+kramer|hoa[-\s]?usa|"
    r"property\s+specialists|realmanage|commonwealth\s+edison|"
    r"jason\s+wolin"
    r")\b",
    re.IGNORECASE,
)


def is_clean(name: str) -> tuple[bool, str]:
    """Return (ok, reason). reason is empty if ok=True."""
    s = (name or "").strip()
    if not s:
        return False, "empty"
    if "..." in s or "…" in s:
        return False, "ellipsis"
    if any(ch in s for ch in "|·•\\"):
        return False, "list_garbage"
    # Sentence-boundary period mid-name
    if re.search(r"\w\.\s+[A-Z]", s) and "Inc." not in s and "No." not in s and "Co." not in s and "Pkwy." not in s:
        # but allow trailing periods on suffix (.Inc / .Co)
        # heuristic: if the period appears > 2 chars before end, treat as sentence break
        idx = next((m.start() for m in re.finditer(r"\.", s) if m.start() < len(s) - 3), -1)
        if idx >= 0:
            tail = s[idx + 1:].lstrip()
            if tail and tail[0].isupper():
                return False, "sentence_break"
    if LEADING_BAD_RE.match(s):
        return False, "leading_bad"
    m = ENTITY_SUFFIX_RE.search(s)
    if not m:
        return False, "no_suffix"
    head = s[: m.start()].strip()
    if not head:
        return False, "no_head"
    head_words = head.split()
    alpha_words = [w for w in head_words if re.match(r"^[A-Za-z][A-Za-z'&\-]*$", w)]
    digit_words = [w for w in head_words if re.match(r"^\d", w)]
    # Need at least 2 alpha words OR (1 digit word + 1 alpha word) OR 2 hyphenated words
    if len(alpha_words) < 2 and not (digit_words and alpha_words):
        return False, "too_few_proper"
    if GENERIC_GEO_PREFIX_RE.match(s):
        return False, "geo_only"
    if MGMT_CO_INSIDE_RE.search(s):
        return False, "mgmt_co_inside"
    if len(s) > 110:
        return False, "too_long"
    if len(s) < 10:
        return False, "too_short"
    return True, ""


def normalize_for_dedup(name: str) -> str:
    s = name.lower()
    # collapse "no. 1" / "no 1" / "number 1"
    s = re.sub(r"\bnumber\b", "no", s)
    s = re.sub(r"\bno\.\s*", "no ", s)
    # strip Inc., LLC, Co., etc.
    s = re.sub(r"\b(inc\.?|llc|co\.|corp\.?|ltd\.?)\b", "", s)
    # strip suffix
    s = ENTITY_SUFFIX_RE.sub("", s)
    # collapse whitespace and remove non-alphanumeric
    s = re.sub(r"[^a-z0-9]+", "", s)
    # strip directional/abbrev variants
    s = re.sub(r"^(n|s|e|w|north|south|east|west)", "", s)
    return s


def load_existing_bank_names(state: str = "IL", chicagoland_only: bool = True) -> list[dict]:
    """Pull existing bank manifests' names for the Chicagoland counties.

    Uses google.cloud.storage with parallel fetches; gsutil cat per file is too slow.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from google.cloud import storage

    counties = ["cook", "dupage", "lake", "will", "kane", "mchenry", "kendall"] if chicagoland_only else None

    client = storage.Client()
    bucket = client.bucket("hoaproxy-bank")
    paths: list[str] = []
    if counties:
        for c in counties:
            for blob in client.list_blobs(bucket, prefix=f"v1/{state}/{c}/"):
                if blob.name.endswith("/manifest.json"):
                    paths.append(blob.name)
    else:
        for blob in client.list_blobs(bucket, prefix=f"v1/{state}/"):
            if blob.name.endswith("/manifest.json"):
                paths.append(blob.name)

    def fetch(p: str) -> dict | None:
        try:
            data = bucket.blob(p).download_as_bytes(timeout=30)
            m = json.loads(data)
            name = (m.get("name") or m.get("hoa_name") or "").strip()
            if not name:
                return None
            county_slug = p.split(f"/{state}/")[1].split("/")[0]
            return {
                "name": name,
                "state": state,
                "county": county_slug.replace("-", " ").title()
                    if county_slug not in ("_unknown-county", "unresolved-name") else None,
                "metadata_type": m.get("metadata_type") or ("condo" if "condominium" in name.lower() else "hoa"),
                "address": m.get("address") or {"state": state},
                "source": "il-existing-bank-manifest",
                "source_url": f"gs://hoaproxy-bank/{p}",
                "discovery_pattern": "name-list-first-from-bank",
            }
        except Exception:
            return None

    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for fut in as_completed([pool.submit(fetch, p) for p in paths]):
            r = fut.result()
            if r:
                out.append(r)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", default=str(ROOT / "state_scrapers/il/leads/il_chicagoland_seed.jsonl"))
    p.add_argument("--output", default=str(ROOT / "state_scrapers/il/leads/il_chicagoland_seed_clean.jsonl"))
    p.add_argument("--include-bank", action="store_true",
                   help="Also include names from existing v1/IL/{chicagoland-county}/ bank manifests")
    p.add_argument("--show-rejects", action="store_true")
    args = p.parse_args()

    raw = []
    with open(args.input, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw.append(json.loads(line))
            except Exception:
                continue

    if args.include_bank:
        bank_names = load_existing_bank_names()
        print(f"loaded {len(bank_names)} names from Chicagoland bank manifests")
        raw.extend(bank_names)

    kept: dict[str, dict] = {}
    rejects: dict[str, int] = {}
    for ent in raw:
        name = ent.get("name") or ""
        ok, reason = is_clean(name)
        if not ok:
            rejects[reason] = rejects.get(reason, 0) + 1
            if args.show_rejects:
                print(f"REJECT [{reason:14s}]  {name}")
            continue
        key = normalize_for_dedup(name)
        if not key or key in kept:
            continue
        kept[key] = ent

    out = list(kept.values())
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        for ent in out:
            f.write(json.dumps(ent, sort_keys=True) + "\n")

    print(f"\ninput: {len(raw)}  kept: {len(out)}  unique by dedup")
    print("rejects:")
    for r, n in sorted(rejects.items(), key=lambda kv: -kv[1]):
        print(f"  {r:18s}  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
