"""For every live GA HOA whose lat/lng falls outside the GA bounding box,
infer the real US state from OCR text and post a correction.

Three patterns we expect to find:

  - Westbank-pattern:  doc OCR clearly names a non-GA state AND the
    existing lat/lng lies inside that state's bbox. Set state to that
    truth; leave coords alone.

  - Belvedere-pattern: doc OCR names a non-GA state but the existing
    lat/lng is somewhere else entirely. Set state to the OCR truth;
    demote location_quality to "city_only" so the wrong pin disappears.

  - Augusta-Woods-pattern: doc OCR points to GA (the HOA really is GA)
    but lat/lng was set by a wrong-Nominatim hit. Keep state=GA;
    demote location_quality to "city_only".

  - Ambiguous: OCR has no clear single-state signal. Conservative
    fallback: keep state=GA; demote location_quality so the bad pin
    goes away. Logged for audit.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

sys.path.insert(0, str(ROOT / "state_scrapers" / "ga" / "scripts"))
from clean_dirty_hoa_names import _live_admin_token  # noqa: E402

BASE_URL = "https://hoaproxy.org"

# US state bounding boxes (approximate, intentionally a touch generous to
# tolerate near-border points).
STATE_BBOX = {
    "AL": (30.1, 35.1, -88.5, -84.8),
    "AR": (33.0, 36.5, -94.7, -89.6),
    "AZ": (31.3, 37.1, -114.9, -109.0),
    "CA": (32.5, 42.1, -124.5, -114.0),
    "CO": (36.9, 41.1, -109.1, -101.9),
    "CT": (40.9, 42.1, -73.8, -71.7),
    "DC": (38.7, 39.0, -77.2, -76.8),
    "DE": (38.4, 39.9, -75.8, -74.9),
    "FL": (24.4, 31.1, -87.7, -79.9),
    "GA": (30.3, 35.05, -85.7, -80.7),
    "HI": (18.5, 22.5, -160.5, -154.5),
    "IA": (40.3, 43.6, -96.7, -90.0),
    "ID": (41.9, 49.1, -117.3, -111.0),
    "IL": (36.9, 42.6, -91.6, -87.4),
    "IN": (37.7, 41.9, -88.2, -84.7),
    "KS": (36.9, 40.1, -102.1, -94.5),
    "KY": (36.4, 39.2, -89.7, -81.9),
    "LA": (28.8, 33.1, -94.1, -88.7),
    "MA": (41.2, 42.9, -73.6, -69.8),
    "MD": (37.8, 39.8, -79.6, -75.0),
    "ME": (43.0, 47.6, -71.2, -66.9),
    "MI": (41.6, 48.4, -90.5, -82.3),
    "MN": (43.4, 49.5, -97.3, -89.4),
    "MO": (35.9, 40.7, -95.8, -89.0),
    "MS": (30.1, 35.1, -91.7, -88.0),
    "MT": (44.3, 49.1, -116.1, -104.0),
    "NC": (33.7, 36.7, -84.4, -75.3),
    "ND": (45.9, 49.1, -104.1, -96.5),
    "NE": (39.9, 43.1, -104.1, -95.2),
    "NH": (42.6, 45.4, -72.6, -70.5),
    "NJ": (38.8, 41.4, -75.6, -73.8),
    "NM": (31.2, 37.1, -109.1, -103.0),
    "NV": (35.0, 42.1, -120.1, -114.0),
    "NY": (40.4, 45.1, -79.8, -71.8),
    "OH": (38.3, 41.9, -84.9, -80.5),
    "OK": (33.6, 37.1, -103.1, -94.4),
    "OR": (41.9, 46.4, -124.7, -116.4),
    "PA": (39.6, 42.4, -80.6, -74.6),
    "RI": (41.1, 42.1, -71.9, -71.1),
    "SC": (32.0, 35.3, -83.4, -78.5),
    "SD": (42.4, 45.95, -104.1, -96.4),
    "TN": (34.9, 36.7, -90.4, -81.6),
    "TX": (25.8, 36.6, -106.7, -93.5),
    "UT": (36.9, 42.1, -114.1, -109.0),
    "VA": (36.5, 39.5, -83.7, -75.2),
    "VT": (42.7, 45.1, -73.5, -71.4),
    "WA": (45.5, 49.1, -124.8, -116.9),
    "WI": (42.4, 47.1, -92.9, -86.8),
    "WV": (37.2, 40.7, -82.7, -77.7),
    "WY": (40.9, 45.1, -111.1, -104.0),
}

STATE_NAME_TO_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}

_STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in STATE_NAME_TO_ABBR) + r")\b",
    re.I,
)
_STATE_ABBR_RE = re.compile(r"\b([A-Z]{2})\s+\d{5}\b")  # "GA 30303" form
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

# ZIP first-digit -> plausible state set. Used as a tiebreaker.
ZIP_FIRST_TO_STATES = {
    "0": {"CT", "MA", "ME", "NH", "NJ", "PR", "RI", "VT"},
    "1": {"DE", "NY", "PA"},
    "2": {"DC", "MD", "NC", "SC", "VA", "WV"},
    "3": {"AL", "FL", "GA", "MS", "TN"},
    "4": {"IN", "KY", "MI", "OH"},
    "5": {"IA", "MN", "MT", "ND", "SD", "WI"},
    "6": {"IL", "KS", "MO", "NE"},
    "7": {"AR", "LA", "OK", "TX"},
    "8": {"AZ", "CO", "ID", "NM", "NV", "UT", "WY"},
    "9": {"AK", "CA", "HI", "OR", "WA"},
}


def in_bbox(lat: float | None, lon: float | None, state: str) -> bool:
    bb = STATE_BBOX.get(state)
    if not bb or lat is None or lon is None:
        return False
    return bb[0] <= lat <= bb[1] and bb[2] <= lon <= bb[3]


def _fetch_doc_text(base_url: str, hoa: str, max_chars: int, max_docs: int) -> str:
    docs = requests.get(
        f"{base_url}/hoas/{requests.utils.quote(hoa, safe='')}/documents",
        timeout=60,
    )
    if not docs.ok:
        return ""
    paths = [
        d.get("relative_path") or d.get("path")
        for d in (docs.json() or [])
        if d.get("relative_path") or d.get("path")
    ][:max_docs]
    chunks = []
    total = 0
    for path in paths:
        rendered = requests.get(
            f"{base_url}/hoas/{requests.utils.quote(hoa, safe='')}/documents/searchable",
            params={"path": path},
            timeout=120,
        )
        if not rendered.ok:
            continue
        pre = re.findall(r"<pre>(.*?)</pre>", rendered.text, flags=re.S | re.I)
        text = "\n".join(html.unescape(re.sub(r"<[^>]+>", " ", part)) for part in pre)
        chunks.append(text[:max_chars])
        total += len(text)
        if total >= max_chars * 2:
            break
    return "\n".join(chunks)


def infer_state(text: str) -> tuple[str | None, dict[str, int], str]:
    """Return (best_state, vote_counts, reason)."""
    counts: Counter[str] = Counter()
    for m in _STATE_NAME_RE.finditer(text):
        counts[STATE_NAME_TO_ABBR[m.group(1).lower()]] += 3  # full names heavily weighted
    for m in _STATE_ABBR_RE.finditer(text):
        ab = m.group(1).upper()
        if ab in STATE_BBOX:
            counts[ab] += 2
    for m in _ZIP_RE.finditer(text):
        z = m.group(1)
        if z.startswith("0") and z != "00000":
            for st in ZIP_FIRST_TO_STATES["0"]:
                counts[st] += 0
            continue
        states = ZIP_FIRST_TO_STATES.get(z[0], set())
        for st in states:
            counts[st] += 1
    if not counts:
        return None, {}, "no_signals"
    best, n = counts.most_common(1)[0]
    second = counts.most_common(2)[1][1] if len(counts) > 1 else 0
    if n < 3:
        return None, dict(counts), "too_weak"
    if second and n < second * 1.5:
        return None, dict(counts), "too_close"
    return best, dict(counts), "ok"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default=BASE_URL)
    p.add_argument("--out", default="state_scrapers/ga/results/misrouted_state_corrections.jsonl")
    p.add_argument("--max-text-chars", type=int, default=4000)
    p.add_argument("--max-docs", type=int, default=2)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    offset = 0
    while True:
        r = requests.get(
            f"{args.base_url}/hoas/summary",
            params={"state": "GA", "limit": 500, "offset": offset},
            timeout=120,
        ).json()
        b = r.get("results") or []
        rows.extend(b)
        if len(rows) >= int(r.get("total") or 0) or not b:
            break
        offset += len(b)

    suspects = [
        r for r in rows
        if r.get("latitude") is not None
        and not in_bbox(r["latitude"], r["longitude"], "GA")
    ]
    print(f"live GA: {len(rows)}  suspects (mapped, lat/lng outside GA): {len(suspects)}", file=sys.stderr)

    if args.limit:
        suspects = suspects[: args.limit]

    decisions: list[dict] = []
    backfill_records: list[dict] = []
    pattern_counts: Counter[str] = Counter()

    for i, row in enumerate(suspects, 1):
        name = row["hoa"]
        text = _fetch_doc_text(args.base_url, name, args.max_text_chars, args.max_docs)
        truth, votes, reason = infer_state(text)
        lat, lon = row["latitude"], row["longitude"]

        if truth is None:
            # Conservative: if we can't tell, just hide the bad pin.
            pattern = "ambiguous_demote"
            backfill_records.append({"hoa": name, "location_quality": "city_only"})
        elif truth == "GA":
            # Augusta-Woods pattern: state right, coords wrong.
            pattern = "ga_demote"
            backfill_records.append({"hoa": name, "location_quality": "city_only"})
        elif in_bbox(lat, lon, truth):
            # Westbank pattern: state wrong, coords right.
            pattern = "state_only_fix"
            backfill_records.append({"hoa": name, "state": truth})
        else:
            # Belvedere pattern: state wrong, coords wrong.
            pattern = "state_fix_and_demote"
            backfill_records.append({"hoa": name, "state": truth, "location_quality": "city_only"})

        pattern_counts[pattern] += 1
        decisions.append({
            "hoa_id": row["hoa_id"],
            "hoa": name,
            "live_state": row.get("state"),
            "live_lat": lat,
            "live_lng": lon,
            "live_city": row.get("city"),
            "ocr_truth": truth,
            "ocr_votes": votes,
            "ocr_reason": reason,
            "pattern": pattern,
        })
        if i % 10 == 0:
            print(f"  {i}/{len(suspects)}: {dict(pattern_counts)}", file=sys.stderr)
            out_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))

    out_path.write_text("\n".join(json.dumps(d, sort_keys=True) for d in decisions))
    print(json.dumps({"suspects": len(suspects), "patterns": dict(pattern_counts)}, sort_keys=True))

    if not args.apply or not backfill_records:
        if not args.apply:
            print("dry-run only; pass --apply to post records", file=sys.stderr)
        return 0

    token = _live_admin_token()
    if not token:
        raise SystemExit("no admin token")

    # Backfill needs records grouped by quality so we can submit them all in
    # one POST without confusing the endpoint. The endpoint accepts one
    # records[] of mixed records, so just batch in chunks of 50.
    totals = {"matched": 0, "not_found": 0, "bad_quality": 0}
    for start in range(0, len(backfill_records), 50):
        chunk = backfill_records[start : start + 50]
        resp = requests.post(
            f"{args.base_url}/admin/backfill-locations",
            headers={"Authorization": f"Bearer {token}"},
            json={"records": chunk},
            timeout=180,
        )
        if not resp.ok:
            print(f"  chunk {start//50+1}: HTTP {resp.status_code} {resp.text[:200]}", file=sys.stderr)
            continue
        payload = resp.json()
        for k in totals:
            totals[k] += int(payload.get(k) or 0)
        print(
            f"  chunk {start//50+1}: matched={payload.get('matched')} "
            f"not_found={payload.get('not_found')} bad={payload.get('bad_quality')}",
            file=sys.stderr,
        )
    print(json.dumps({"applied": True, **totals}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
