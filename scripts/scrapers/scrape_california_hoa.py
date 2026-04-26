"""Scrape california-homeowners-associations.com for enriched HOA data.

Enumerates HOAs by county, then fetches each detail page for board members,
management company, address, website, etc.

Usage:
    python scripts/scrapers/scrape_california_hoa.py              # scrape all ~40K
    python scripts/scrapers/scrape_california_hoa.py --test 10    # test with 10 detail pages
    python scripts/scrapers/scrape_california_hoa.py --county Orange  # one county only

Resumable: skips IDs already in hoa_details.jsonl on restart.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.california-homeowners-associations.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}
DATA_DIR = Path("data/california")
INDEX_PATH = DATA_DIR / "hoa_index.jsonl"
DETAILS_PATH = DATA_DIR / "hoa_details.jsonl"
DETAILS_CSV = DATA_DIR / "hoa_details.csv"
REQUEST_DELAY = 3.0  # base seconds between requests
JITTER = 2.0  # random 0–2s added to each delay
MAX_RETRIES = 5


def make_session() -> requests.Session:
    """Create a session with browser-like headers and seed it with cookies."""
    session = requests.Session()
    session.headers.update(HEADERS)
    print("  Establishing session...")
    resp = session.get(BASE_URL, timeout=30)
    print(f"  Homepage: {resp.status_code}, cookies: {len(session.cookies)}")
    time.sleep(2)
    return session


def polite_sleep():
    """Sleep with jitter to look less bot-like."""
    time.sleep(REQUEST_DELAY + random.uniform(0, JITTER))


def fetch(url: str, session: requests.Session) -> str | None:
    """Fetch a URL with retries and exponential backoff."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (429, 403):
                wait = 10 * (attempt + 1) + random.uniform(0, 5)
                print(f"  HTTP {resp.status_code}, backing off {wait:.0f}s... (attempt {attempt+1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            print(f"  HTTP {resp.status_code} for {url}")
            return None
        except requests.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(5)
            else:
                print(f"  Error fetching {url}: {e}")
                return None
    return None


# ── Phase 1: Enumerate HOA IDs by county ────────────────────────────────

def get_counties(session: requests.Session) -> list[str]:
    """Get all county names from the county listing page."""
    url = f"{BASE_URL}/california_counties_list.php?page=list"
    html = fetch(url, session)
    if not html:
        print("Error: Could not fetch county list.")
        sys.exit(1)
    soup = BeautifulSoup(html, "html.parser")
    counties = []
    for a in soup.find_all("a", href=re.compile(r"california-counties")):
        m = re.search(r"masterkey1=([^&]+)", a["href"])
        if m:
            county = m.group(1)
            if county not in counties:
                counties.append(county)
    return counties


def scrape_county_index(county: str, session: requests.Session) -> list[dict]:
    """Scrape all listing pages for a county, returning HOA entries with IDs."""
    entries = []
    page = 1
    while True:
        if page == 1:
            url = f"{BASE_URL}/california_hoa_p_list.php?mastertable=california-counties&masterkey1={county}"
        else:
            url = f"{BASE_URL}/california_hoa_p_list.php?goto={page}masterkey1={unquote(county)}&mastertable=california-counties"
        polite_sleep()
        html = fetch(url, session)
        if not html:
            break

        soup = BeautifulSoup(html, "html.parser")
        links = soup.find_all("a", href=re.compile(r"caboard_list"))
        if not links:
            break

        for link in links:
            m = re.search(r"masterkey1=(\w+)", link["href"])
            if not m:
                continue
            hoa_id = m.group(1)
            # Get HOA name from the same row
            row = link.find_parent("tr")
            name = ""
            if row:
                cells = row.find_all("td")
                if cells:
                    name = cells[0].get_text(strip=True)
            entries.append({
                "id": hoa_id,
                "name": name,
                "county": unquote(county),
            })

        # Check for next page
        next_links = soup.find_all("a", href=re.compile(r"goto="))
        page_nums = []
        for nl in next_links:
            pm = re.search(r"goto=(\d+)", nl["href"])
            if pm:
                page_nums.append(int(pm.group(1)))
        if page_nums and max(page_nums) > page:
            page += 1
        else:
            break

    return entries


def run_phase1(county_filter: str | None = None) -> list[dict]:
    """Enumerate all HOA IDs by county."""
    session = make_session()
    counties = get_counties(session)
    print(f"Found {len(counties)} counties")

    if county_filter:
        counties = [c for c in counties if unquote(c).lower() == county_filter.lower()]
        if not counties:
            print(f"Error: County '{county_filter}' not found.")
            sys.exit(1)

    # Load existing index for resumability
    done_counties: set[str] = set()
    existing: list[dict] = []
    if INDEX_PATH.exists():
        with open(INDEX_PATH) as f:
            for line in f:
                rec = json.loads(line)
                existing.append(rec)
                done_counties.add(rec.get("county", ""))
        print(f"  Loaded {len(existing)} existing index entries")

    all_entries = list(existing)

    with open(INDEX_PATH, "a") as out_f:
        for i, county in enumerate(counties, 1):
            county_name = unquote(county)
            if county_name in done_counties:
                print(f"  [{i}/{len(counties)}] {county_name}: already done, skipping")
                continue
            print(f"  [{i}/{len(counties)}] {county_name}...", end=" ", flush=True)
            entries = scrape_county_index(county, session)
            for e in entries:
                all_entries.append(e)
                out_f.write(json.dumps(e) + "\n")
            out_f.flush()
            print(f"{len(entries)} HOAs")
            done_counties.add(county_name)

    print(f"Phase 1 complete: {len(all_entries)} HOAs → {INDEX_PATH}")
    return all_entries


# ── Phase 2: Detail pages ───────────────────────────────────────────────

def parse_detail_page(html: str, hoa_id: str) -> dict:
    """Extract structured data from a detail page.

    The site has one table with columns: TYPE, NAME, ADDRESS, ADDRESS2, CITY, STATE, ZIP, WEBSITE.
    Rows are either 'Board Member' or 'Property Manager'.
    The legal name is in a <strong> tag.
    """
    soup = BeautifulSoup(html, "html.parser")
    record: dict = {"id": hoa_id}

    # -- Legal name from strong tag --
    for strong in soup.find_all("strong"):
        text = strong.get_text(strip=True)
        if text.startswith("HOA Legal Name:"):
            record["legal_name"] = text.replace("HOA Legal Name:", "").strip()
            break

    # -- Parse the data table --
    board_members = []
    table = soup.find("table")
    if table:
        headers = []
        for row in table.find_all("tr"):
            ths = row.find_all("th")
            if ths:
                headers = [th.get_text(strip=True).upper() for th in ths]
                continue
            cells = row.find_all("td")
            if not cells or not headers:
                continue
            vals = {h: cells[i].get_text(strip=True) if i < len(cells) else ""
                    for i, h in enumerate(headers)}
            row_type = vals.get("TYPE", "")
            if row_type == "Board Member":
                name = vals.get("NAME", "")
                if name:
                    board_members.append({"title": "Board Member", "name": name})
            elif row_type == "Property Manager":
                record["pm_company"] = vals.get("NAME", "")
                addr_parts = [vals.get("ADDRESS", ""), vals.get("ADDRESS2", "")]
                addr = " ".join(p for p in addr_parts if p).strip()
                city = vals.get("CITY", "")
                state = vals.get("STATE", "")
                zip_code = vals.get("ZIP", "")
                if city:
                    record["city"] = city
                full_addr = f"{addr} {city} {state} {zip_code}".strip()
                record["pm_address"] = full_addr
                record["pm_website"] = vals.get("WEBSITE", "")

    record["board_members"] = board_members

    # -- Title from page heading (has "HOA Name, City CA" format) --
    h1 = soup.find("h1") or soup.find("title")
    if h1:
        title_text = h1.get_text(strip=True)
        # Extract AKA from title: "Los Quatros Homeowners Association, Orange CA ..."
        m = re.match(r"^(.+?),\s*(\w[\w\s]*?)\s+CA", title_text)
        if m:
            record.setdefault("aka", m.group(1).strip())
            record.setdefault("city", m.group(2).strip())

    return record


def scrape_detail(hoa_id: str, session: requests.Session) -> dict | None:
    """Fetch and parse a single detail page."""
    url = f"{BASE_URL}/caboard_list.php?mastertable=california-hoa-p&masterkey1={hoa_id}"
    print(f"    fetching {hoa_id}...", end=" ", flush=True)
    polite_sleep()
    html = fetch(url, session)
    if not html:
        print("FAILED")
        return None
    print(f"OK ({len(html)} bytes)", flush=True)
    try:
        return parse_detail_page(html, hoa_id)
    except Exception as e:
        print(f"  Parse error for {hoa_id}: {e}")
        return None


def run_phase2(max_records: int | None = None) -> list[dict]:
    """Fetch detail pages for all HOAs in the index."""
    if not INDEX_PATH.exists():
        print("Error: Run phase 1 first to build the index.")
        sys.exit(1)

    # Load index
    index: list[dict] = []
    with open(INDEX_PATH) as f:
        for line in f:
            index.append(json.loads(line))
    print(f"Loaded {len(index)} HOAs from index")

    if max_records:
        index = index[:max_records]

    # Load existing details for resumability
    done_ids: set[str] = set()
    existing_details: list[dict] = []
    if DETAILS_PATH.exists():
        with open(DETAILS_PATH) as f:
            for line in f:
                rec = json.loads(line)
                done_ids.add(rec["id"])
                existing_details.append(rec)
        print(f"  Loaded {len(existing_details)} existing detail records")

    todo = [e for e in index if e["id"] not in done_ids]
    print(f"  {len(todo)} remaining to fetch")

    if not todo:
        print("  Nothing to do.")
        return existing_details

    all_details = list(existing_details)
    session = make_session()
    done_count = len(existing_details)

    with open(DETAILS_PATH, "a") as out_f:
        for i, entry in enumerate(todo, 1):
            try:
                detail = scrape_detail(entry["id"], session)
                if detail:
                    # Carry county from index
                    detail["county"] = entry.get("county", "")
                    all_details.append(detail)
                    done_count += 1
                    out_f.write(json.dumps(detail) + "\n")
                    out_f.flush()
                if i <= 3 or i % 100 == 0:
                    name = detail.get("legal_name", "?") if detail else "FAILED"
                    print(f"  [{done_count}/{len(index)}] {name}")
            except Exception as e:
                print(f"  {entry['id']} failed: {e}")

    print(f"Scrape complete: {len(all_details)} detail records → {DETAILS_PATH}")
    write_csv(all_details)
    return all_details


def write_csv(details: list[dict]) -> None:
    """Write details to CSV."""
    if not details:
        return
    fieldnames = [
        "id", "legal_name", "aka", "county", "city",
        "pm_company", "pm_address", "pm_website",
        "board_members_count", "board_members",
    ]
    with open(DETAILS_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for rec in details:
            row = dict(rec)
            bm = row.pop("board_members", [])
            row["board_members_count"] = len(bm)
            row["board_members"] = "; ".join(m["name"] for m in bm) if bm else ""
            writer.writerow(row)
    print(f"  CSV written → {DETAILS_CSV}")


# ── Main ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scrape california-homeowners-associations.com"
    )
    parser.add_argument("--phase", type=int, choices=[1, 2],
                        help="Run only phase 1 (index) or 2 (details)")
    parser.add_argument("--test", type=int,
                        help="Limit to N detail records for testing")
    parser.add_argument("--county", type=str,
                        help="Scrape only this county (e.g. 'Orange')")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if args.phase is None or args.phase == 1:
        run_phase1(county_filter=args.county)

    if args.phase is None or args.phase == 2:
        run_phase2(max_records=args.test)


if __name__ == "__main__":
    main()
