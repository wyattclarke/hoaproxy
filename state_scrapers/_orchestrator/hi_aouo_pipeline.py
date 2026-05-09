#!/usr/bin/env python3
"""HI condo registry pull from DCCA AOUO Contact List PDF.

Source:
  https://cca.hawaii.gov/wp-content/uploads/2026/04/AOUO-Contact-List-4.29.26.pdf

The PDF lists ~1,668 registered Hawaii condominium associations with
REG#, AOUO name, officer contact, mailing address, managing agent, phone.
For our seed we keep:
  - AOUO name (canonical legal name from the registry)
  - REG # (registry id)
  - Managing agent (useful for later doc discovery)
  - ZIP / city when officer's mailing address is in HI (best-effort
    geo seed; many officers list off-island addresses so this is sparse)

Writes:
  state_scrapers/hi/leads/hi_aouo_seed.jsonl

Once written, run state_scrapers/_orchestrator/namelist_discover.py
against this seed.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

import pypdf

ROOT = Path(__file__).resolve().parents[2]

PDF_URL = "https://cca.hawaii.gov/wp-content/uploads/2026/04/AOUO-Contact-List-4.29.26.pdf"
PDF_LOCAL = ROOT / "state_scrapers/hi/leads/hi_aouo_2026_04.pdf"
SEED_OUT = ROOT / "state_scrapers/hi/leads/hi_aouo_seed.jsonl"
LOG_PATH = ROOT / "state_scrapers/_orchestrator/hi_aouo_pipeline.log"

# Standard 2-letter US state tokens for splitting addresses
STATE_TOKENS = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY",
    "LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND",
    "OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC",
}

REG_RE = re.compile(r"(\d{1,5})\s")
TITLE_RE = re.compile(r"\b(PRESIDENT|CONTACT|VICE PRESIDENT|VP|MGR|MANAGER|TREASURER|SECRETARY)\b")
ZIP_RE = re.compile(r"\b(9\d{4})(?:-\d{4})?\b")  # Hawaii ZIPs are 967xx, 968xx
PHONE_RE = re.compile(r"\b(\d{10})\b|\b(\d{3}[-. ]?\d{3}[-. ]?\d{4})\b")


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg, flush=True)


def fetch_pdf() -> Path:
    PDF_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    if PDF_LOCAL.exists() and PDF_LOCAL.stat().st_size > 100_000:
        log(f"Using cached {PDF_LOCAL}")
        return PDF_LOCAL
    log(f"Downloading {PDF_URL}")
    req = urllib.request.Request(PDF_URL, headers={"User-Agent": "HOAproxy public-document discovery (+https://hoaproxy.org)"})
    with urllib.request.urlopen(req, timeout=60) as resp, PDF_LOCAL.open("wb") as f:
        f.write(resp.read())
    log(f"Saved {PDF_LOCAL.stat().st_size} bytes to {PDF_LOCAL}")
    return PDF_LOCAL


def extract_pages(path: Path) -> list[str]:
    """Page-by-page extraction so we can keep records that span page breaks
    detectable."""
    reader = pypdf.PdfReader(str(path))
    out = []
    for page in reader.pages:
        try:
            out.append(page.extract_text() or "")
        except Exception:
            out.append("")
    return out


# --- Record splitting ----------------------------------------------------
# Records start with a REG# at the start of a line. Strategy: find every
# REG# in the text and split there. The header "REG # AOUO NAME" appears
# at the top of every page; skip lines that look like the header.

HEADER_LINE_RE = re.compile(r"^\s*REG\s*#\s*AOUO\s*NAME\b", re.IGNORECASE)


def parse_records(pages: list[str]) -> list[dict]:
    text = "\n".join(pages)
    # Drop header lines so they don't get parsed as records
    text = "\n".join(line for line in text.split("\n") if not HEADER_LINE_RE.match(line))

    # Split on TITLE keyword — every record contains exactly one of
    # PRESIDENT|CONTACT|VICE PRESIDENT|...|TREASURER. The piece BEFORE the
    # title is "[REG#] [AOUO NAME]". The piece AFTER is officer + address +
    # mgmt + phone, ending where the next record's REG# starts.
    chunks = TITLE_RE.split(text)
    # chunks layout: [pre0, title0, mid0, title1, mid1, ...]
    # Where pre0 is junk header noise, and from i=1 each (title_{(i-1)/2}, mid_{(i-1)/2})
    # pairs apply to record (i-1)/2 with the REG#+NAME read from the END of pre_{i-1}/mid_{i-1}.
    # Easier approach: take consecutive triples (prefix, title, suffix) where
    # the prefix ends with "[REG#] [NAME]" and the suffix continues until the
    # next REG#.

    records: list[dict] = []
    # Iterate over (TITLE token, suffix) pairs and pair them with the
    # preceding "REG# NAME" portion.
    i = 0
    pending_prefix = chunks[0] if chunks else ""
    while i + 1 < len(chunks):
        title = chunks[i + 1] if i + 1 < len(chunks) else ""
        suffix = chunks[i + 2] if i + 2 < len(chunks) else ""
        # Extract REG# + NAME from end of pending_prefix
        # Find LAST REG# match in pending_prefix; everything from there to end is "REG NAME"
        last_reg_match = None
        for m in REG_RE.finditer(pending_prefix):
            last_reg_match = m
        if last_reg_match:
            reg = last_reg_match.group(1)
            name_raw = pending_prefix[last_reg_match.end():].strip()
        else:
            reg = ""
            name_raw = pending_prefix.strip()

        # Extract address (street + city + ZIP) from the suffix.
        # Suffix shape: "OFFICER_NAME STREET CITY ST ZIP MGMT_CO PHONE [REG# NAME]"
        # The next record starts at the next REG# — but we already split chunks
        # at TITLE, so the next REG# in suffix marks the next record's start.
        # Trim suffix at the next "<digits> <text>" that looks like a REG#:
        # heuristic — first occurrence of a 1-5 digit number that is NOT part
        # of an address (i.e., not preceded by `#` or street-number-style).
        next_reg_pos = None
        for m in re.finditer(r"\b(\d{1,5})\s+[A-Z`\"']", suffix):
            # Skip if preceded by # (street-suite-like)
            if m.start() > 0 and suffix[m.start() - 1] == "#":
                continue
            # Skip if this looks like a street number (digit immediately after a state token + space + zip)
            # Most reg numbers follow a 10-digit phone number. Check 12-30 chars before
            window = suffix[max(0, m.start() - 30): m.start()]
            if re.search(r"\d{10}\s*$", window) or re.search(r"\d{3}[-. ]\d{3}[-. ]\d{4}\s*$", window):
                next_reg_pos = m.start()
                break
        record_suffix = suffix[:next_reg_pos] if next_reg_pos is not None else suffix
        new_pending_prefix = suffix[next_reg_pos:] if next_reg_pos is not None else ""

        record = {
            "reg": reg,
            "name_raw": name_raw,
            "title": title,
            "rest": record_suffix.strip(),
        }
        records.append(record)

        pending_prefix = new_pending_prefix
        i += 2

    return records


def normalize_aouo_name(raw: str) -> str:
    """The PDF stores names in ALL CAPS with various Hawaiian okinas (\`).
    Title-case them while preserving okinas and short connector words."""
    s = raw.strip().strip(",.-")
    # Some names get mashed across the page break with no space; fix common cases
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    # Title case but keep "OF", "AT", "ON", "THE" lowercase mid-name
    small = {"of", "at", "on", "the", "and", "in", "by", "fka", "aka"}
    parts = []
    for j, w in enumerate(s.split()):
        wl = w.lower()
        if j > 0 and wl in small:
            parts.append(wl)
        elif w.startswith("`") and len(w) > 1:
            # Hawaiian okina — preserve, capitalize next char
            parts.append("`" + w[1:].capitalize())
        else:
            parts.append(w.capitalize())
    return " ".join(parts).strip()


def extract_zip_and_mgmt(rest: str) -> dict:
    """From the suffix of a record, extract: HI ZIP if present, and managing
    agent (the all-caps phrase before the trailing 10-digit phone)."""
    out: dict[str, str | None] = {"postal_code": None, "city": None, "mgmt_co": None}
    # Find all HI-pattern ZIPs (96xxx)
    hi_zips = [m.group(1) for m in re.finditer(r"\b(96\d{3})\b", rest)]
    if hi_zips:
        out["postal_code"] = hi_zips[-1]  # last one = most likely the project's
    # Try to extract city: token immediately before " HI <zip>"
    m = re.search(r"\b([A-Z][A-Z `']{2,30})\s+HI\s+96\d{3}\b", rest)
    if m:
        out["city"] = m.group(1).title().strip()
    # Mgmt agent: typically the phrase right before the trailing phone
    m = re.search(r"\b96\d{3}(?:-\d{4})?\s+([A-Z][A-Z &.,'\-]+?)\s+\d{10}\b", rest)
    if m:
        out["mgmt_co"] = m.group(1).title().strip()
    return out


def main() -> int:
    pdf_path = fetch_pdf()
    pages = extract_pages(pdf_path)
    log(f"Extracted {len(pages)} pages")
    records = parse_records(pages)
    log(f"Parsed {len(records)} raw records")

    # Filter and normalize
    SEED_OUT.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    seen_names: set[str] = set()
    with SEED_OUT.open("w", encoding="utf-8") as f:
        for r in records:
            name_raw = r.get("name_raw") or ""
            # Strip trailing junk words and leading non-alpha
            name_raw = re.sub(r"^[^A-Za-z`]+", "", name_raw).strip()
            # The AOUO name often has trailing officer-name remnants like "JR" — leave as-is
            if len(name_raw) < 3:
                continue
            name = normalize_aouo_name(name_raw)
            if not name or len(name) < 3:
                continue
            key = name.lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            geo = extract_zip_and_mgmt(r.get("rest") or "")
            payload = {
                "name": name,
                "state": "HI",
                "county": guess_county_from_zip(geo.get("postal_code")) or "Honolulu",
                "metadata_type": "condo",
                "registry_id": int(r["reg"]) if r.get("reg", "").isdigit() else None,
                "source": "hi-dcca-aouo-contact-list",
                "source_url": PDF_URL,
                "address": {k: v for k, v in {
                    "state": "HI",
                    "city": geo.get("city"),
                    "postal_code": geo.get("postal_code"),
                }.items() if v},
                "mgmt_company": geo.get("mgmt_co"),
            }
            f.write(json.dumps(payload, sort_keys=True) + "\n")
            written += 1
    log(f"Wrote {written} unique HI condo seeds to {SEED_OUT}")
    return 0


def guess_county_from_zip(zipcode: str | None) -> str | None:
    """HI county lookup from ZIP prefix.  Honolulu (Oahu): 96701-96730, 96732-96792, 96801-96898;
    Maui: 96708-96790 mixed; Hawaii (Big Island): 96704-96785 mixed; Kauai: 96703-96796 mixed.
    Use the canonical Honolulu vs neighbor-island heuristic by prefix."""
    if not zipcode:
        return None
    z = zipcode[:5]
    # Honolulu (Oahu) covers most 968xx codes
    if z.startswith("968") or z in {"96701", "96706", "96707", "96717", "96720", "96731", "96734",
                                     "96744", "96759", "96762", "96782", "96786", "96789", "96791",
                                     "96792", "96795", "96797", "96819", "96825"}:
        if z.startswith("968"):
            return "Honolulu"
    # Hawaii (Big Island): 96704, 96710, 96719, 96720, 96721, 96725, 96726-96728, 96737, 96738, 96739,
    # 96740, 96743, 96749, 96750, 96755, 96760, 96764, 96771, 96772, 96773, 96774, 96776, 96777, 96778,
    # 96780, 96781, 96783, 96785
    bigisland = {"96704","96710","96719","96720","96721","96725","96726","96727","96728","96737",
                  "96738","96739","96740","96743","96749","96750","96755","96760","96764","96771",
                  "96772","96773","96774","96776","96777","96778","96780","96781","96783","96785"}
    if z in bigisland:
        return "Hawaii"
    # Maui: 96708, 96713, 96732, 96733, 96748, 96753, 96761, 96763, 96768, 96770, 96779, 96788, 96790, 96793
    maui = {"96708","96713","96732","96733","96748","96753","96761","96763","96768","96770",
             "96779","96788","96790","96793"}
    if z in maui:
        return "Maui"
    # Kauai: 96703, 96705, 96714, 96716, 96722, 96741, 96746, 96747, 96751, 96752, 96754, 96756, 96765, 96766, 96769, 96796
    kauai = {"96703","96705","96714","96716","96722","96741","96746","96747","96751","96752",
              "96754","96756","96765","96766","96769","96796"}
    if z in kauai:
        return "Kauai"
    if z.startswith("96"):
        return "Honolulu"
    return None


if __name__ == "__main__":
    raise SystemExit(main())
