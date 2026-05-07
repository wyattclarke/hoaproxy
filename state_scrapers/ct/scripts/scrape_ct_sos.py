#!/usr/bin/env python3
"""Scrape active CT HOA / condo / community-association entities from the
Connecticut Open Data Business Master dataset (the canonical SoS registry
export, dataset id ``n7gp-d28j``).

CT decommissioned the CONCORD ASP.NET search and migrated to a Salesforce
Lightning portal at service.ct.gov/business that is hostile to scraping.
The state, however, publishes the same registry as a public SODA dataset
with structured fields (billingstreet, billingcity, billingstate,
billingpostalcode, naics_code, mailing_address). That gives us the
canonical universe of CT entities without any HTML parsing.

Output JSONL is shape-compatible with the RI flow so the downstream
``enrich_*_leads_with_serper.py`` and ``probe_enriched_leads.py`` work
unchanged.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

DATASET = "n7gp-d28j"
ENDPOINT = f"https://data.ct.gov/resource/{DATASET}.json"
USER_AGENT = "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"

# 169 CT towns → traditional 8 counties. CT abolished county government in
# 1960 and the federal statistical equivalents were replaced by 9 COGs in
# 2022, but the bank layout still uses the traditional county labels for
# consistency with the rest of the corpus and with downstream tooling that
# expects state/county/slug.
CITY_COUNTY: dict[str, str] = {
    # Fairfield (23)
    "BETHEL": "Fairfield", "BRIDGEPORT": "Fairfield", "BROOKFIELD": "Fairfield",
    "DANBURY": "Fairfield", "DARIEN": "Fairfield", "EASTON": "Fairfield",
    "FAIRFIELD": "Fairfield", "GREENWICH": "Fairfield", "MONROE": "Fairfield",
    "NEW CANAAN": "Fairfield", "NEW FAIRFIELD": "Fairfield",
    "NEWTOWN": "Fairfield", "NORWALK": "Fairfield", "REDDING": "Fairfield",
    "RIDGEFIELD": "Fairfield", "SHELTON": "Fairfield", "SHERMAN": "Fairfield",
    "STAMFORD": "Fairfield", "STRATFORD": "Fairfield", "TRUMBULL": "Fairfield",
    "WESTON": "Fairfield", "WESTPORT": "Fairfield", "WILTON": "Fairfield",
    # Hartford (29)
    "AVON": "Hartford", "BERLIN": "Hartford", "BLOOMFIELD": "Hartford",
    "BRISTOL": "Hartford", "BURLINGTON": "Hartford", "CANTON": "Hartford",
    "EAST GRANBY": "Hartford", "EAST HARTFORD": "Hartford",
    "EAST WINDSOR": "Hartford", "ENFIELD": "Hartford", "FARMINGTON": "Hartford",
    "GLASTONBURY": "Hartford", "GRANBY": "Hartford", "HARTFORD": "Hartford",
    "HARTLAND": "Hartford", "MANCHESTER": "Hartford", "MARLBOROUGH": "Hartford",
    "NEW BRITAIN": "Hartford", "NEWINGTON": "Hartford", "PLAINVILLE": "Hartford",
    "ROCKY HILL": "Hartford", "SIMSBURY": "Hartford", "SOUTH WINDSOR": "Hartford",
    "SOUTHINGTON": "Hartford", "SUFFIELD": "Hartford", "WEST HARTFORD": "Hartford",
    "WETHERSFIELD": "Hartford", "WINDSOR": "Hartford", "WINDSOR LOCKS": "Hartford",
    # Litchfield (26)
    "BARKHAMSTED": "Litchfield", "BETHLEHEM": "Litchfield",
    "BRIDGEWATER": "Litchfield", "CANAAN": "Litchfield",
    "COLEBROOK": "Litchfield", "CORNWALL": "Litchfield", "GOSHEN": "Litchfield",
    "HARWINTON": "Litchfield", "KENT": "Litchfield", "LITCHFIELD": "Litchfield",
    "MORRIS": "Litchfield", "NEW HARTFORD": "Litchfield",
    "NEW MILFORD": "Litchfield", "NORFOLK": "Litchfield",
    "NORTH CANAAN": "Litchfield", "PLYMOUTH": "Litchfield",
    "ROXBURY": "Litchfield", "SALISBURY": "Litchfield", "SHARON": "Litchfield",
    "THOMASTON": "Litchfield", "TORRINGTON": "Litchfield", "WARREN": "Litchfield",
    "WASHINGTON": "Litchfield", "WATERTOWN": "Litchfield",
    "WINCHESTER": "Litchfield", "WOODBURY": "Litchfield",
    # Middlesex (15)
    "CHESTER": "Middlesex", "CLINTON": "Middlesex", "CROMWELL": "Middlesex",
    "DEEP RIVER": "Middlesex", "DURHAM": "Middlesex",
    "EAST HADDAM": "Middlesex", "EAST HAMPTON": "Middlesex",
    "ESSEX": "Middlesex", "HADDAM": "Middlesex", "KILLINGWORTH": "Middlesex",
    "MIDDLEFIELD": "Middlesex", "MIDDLETOWN": "Middlesex",
    "OLD SAYBROOK": "Middlesex", "PORTLAND": "Middlesex",
    "WESTBROOK": "Middlesex",
    # New Haven (27)
    "ANSONIA": "New Haven", "BEACON FALLS": "New Haven", "BETHANY": "New Haven",
    "BRANFORD": "New Haven", "CHESHIRE": "New Haven", "DERBY": "New Haven",
    "EAST HAVEN": "New Haven", "GUILFORD": "New Haven", "HAMDEN": "New Haven",
    "MADISON": "New Haven", "MERIDEN": "New Haven", "MIDDLEBURY": "New Haven",
    "MILFORD": "New Haven", "NAUGATUCK": "New Haven", "NEW HAVEN": "New Haven",
    "NORTH BRANFORD": "New Haven", "NORTH HAVEN": "New Haven",
    "ORANGE": "New Haven", "OXFORD": "New Haven", "PROSPECT": "New Haven",
    "SEYMOUR": "New Haven", "SOUTHBURY": "New Haven",
    "WALLINGFORD": "New Haven", "WATERBURY": "New Haven",
    "WEST HAVEN": "New Haven", "WOLCOTT": "New Haven", "WOODBRIDGE": "New Haven",
    # New London (21)
    "BOZRAH": "New London", "COLCHESTER": "New London",
    "EAST LYME": "New London", "FRANKLIN": "New London",
    "GRISWOLD": "New London", "GROTON": "New London", "LEBANON": "New London",
    "LEDYARD": "New London", "LISBON": "New London", "LYME": "New London",
    "MONTVILLE": "New London", "NEW LONDON": "New London",
    "NORTH STONINGTON": "New London", "NORWICH": "New London",
    "OLD LYME": "New London", "PRESTON": "New London", "SALEM": "New London",
    "SPRAGUE": "New London", "STONINGTON": "New London",
    "VOLUNTOWN": "New London", "WATERFORD": "New London",
    # Tolland (13)
    "ANDOVER": "Tolland", "BOLTON": "Tolland", "COLUMBIA": "Tolland",
    "COVENTRY": "Tolland", "ELLINGTON": "Tolland", "HEBRON": "Tolland",
    "MANSFIELD": "Tolland", "SOMERS": "Tolland", "STAFFORD": "Tolland",
    "TOLLAND": "Tolland", "UNION": "Tolland", "VERNON": "Tolland",
    "WILLINGTON": "Tolland",
    # Windham (15)
    "ASHFORD": "Windham", "BROOKLYN": "Windham", "CANTERBURY": "Windham",
    "CHAPLIN": "Windham", "EASTFORD": "Windham", "HAMPTON": "Windham",
    "KILLINGLY": "Windham", "PLAINFIELD": "Windham", "POMFRET": "Windham",
    "PUTNAM": "Windham", "SCOTLAND": "Windham", "STERLING": "Windham",
    "THOMPSON": "Windham", "WINDHAM": "Windham", "WOODSTOCK": "Windham",
    # Common postal-village → municipality remaps (CT has many of these)
    "MYSTIC": "New London",         # village in Stonington (and Groton)
    "PAWCATUCK": "New London",      # village in Stonington
    "GAYLORDSVILLE": "Litchfield",  # village in New Milford
    "ROWAYTON": "Fairfield",        # village in Norwalk
    "COS COB": "Fairfield",         # village in Greenwich
    "RIVERSIDE": "Fairfield",       # village in Greenwich
    "OLD GREENWICH": "Fairfield",   # village in Greenwich
    "BYRAM": "Fairfield",           # village in Greenwich
    "GLENBROOK": "Fairfield",       # neighborhood of Stamford
    "SPRINGDALE": "Fairfield",      # neighborhood of Stamford
    "GEORGETOWN": "Fairfield",      # village (Redding/Wilton/Weston/Ridgefield)
    "SANDY HOOK": "Fairfield",      # village in Newtown
    "BOTSFORD": "Fairfield",        # village in Newtown / Monroe
    "STEVENSON": "Fairfield",       # village in Monroe
    "STORRS": "Tolland",            # village in Mansfield
    "STORRS MANSFIELD": "Tolland",
    "HIGGANUM": "Middlesex",        # village in Haddam
    "MOODUS": "Middlesex",          # village in East Haddam
    "CENTERBROOK": "Middlesex",     # village in Essex
    "IVORYTON": "Middlesex",        # village in Essex
    "TARIFFVILLE": "Hartford",      # village in Simsbury
    "UNIONVILLE": "Hartford",       # village in Farmington
    "COLLINSVILLE": "Hartford",     # village in Canton
    "PLANTSVILLE": "Hartford",      # village in Southington
    "FORESTVILLE": "Hartford",      # village in Bristol
    "KENSINGTON": "Hartford",       # village in Berlin
    "BROAD BROOK": "Hartford",      # village in East Windsor
    "SOUTH GLASTONBURY": "Hartford",
    "WEST SIMSBURY": "Hartford",
    "WEST GRANBY": "Hartford",
    "WEST SUFFIELD": "Hartford",
    "EAST BERLIN": "Hartford",
    "SOUTH WINDHAM": "Windham",
    "NORTH WINDHAM": "Windham",
    "WILLIMANTIC": "Windham",       # consolidated city in Windham
    "DAYVILLE": "Windham",          # village in Killingly
    "DANIELSON": "Windham",         # village in Killingly
    "ROGERS": "Windham",            # village in Killingly
    "QUINEBAUG": "Windham",         # village in Thompson
    "NORTH GROSVENORDALE": "Windham",
    "GROSVENORDALE": "Windham",
    "WAUREGAN": "Windham",          # village in Plainfield
    "MOOSUP": "Windham",            # village in Plainfield
    "ONECO": "Windham",             # village in Sterling
    "JEWETT CITY": "New London",    # borough in Griswold
    "GLASGO": "New London",         # village in Griswold
    "TAFTVILLE": "New London",      # village in Norwich
    "YANTIC": "New London",         # village in Norwich
    "OCCUM": "New London",          # village in Norwich
    "BALTIC": "New London",         # village in Sprague
    "VERSAILLES": "New London",     # village in Sprague
    "UNCASVILLE": "New London",     # village in Montville
    "OAKDALE": "New London",        # village in Montville
    "QUAKER HILL": "New London",    # village in Waterford
    "NIANTIC": "New London",        # village in East Lyme
    "GALES FERRY": "New London",    # village in Ledyard
    "MASHANTUCKET": "New London",   # village in Ledyard
    "NORTHFORD": "New Haven",       # village in North Branford
    "MORRIS COVE": "New Haven",     # neighborhood of New Haven
    "WESTVILLE": "New Haven",       # neighborhood of New Haven
    "AMSTON": "Tolland",            # village in Hebron / Lebanon
    "ROCKVILLE": "Tolland",         # village in Vernon
    "STAFFORD SPRINGS": "Tolland",  # village in Stafford
    "CRYSTAL LAKE": "Tolland",      # village in Ellington
    "SOMERSVILLE": "Tolland",       # village in Somers
    "MELROSE": "Hartford",          # village in East Windsor
    "POQUONOCK": "Hartford",        # village in Windsor
    "WEATOGUE": "Hartford",         # village in Simsbury
    "BLOOMFIELD CENTER": "Hartford",
    "TERRYVILLE": "Litchfield",     # village in Plymouth
    "PEQUABUCK": "Litchfield",      # village in Plymouth
    "FALLS VILLAGE": "Litchfield",  # village in Canaan
    "EAST CANAAN": "Litchfield",
    "WEST CORNWALL": "Litchfield",
    "LAKEVILLE": "Litchfield",      # village in Salisbury
    "LIME ROCK": "Litchfield",      # village in Salisbury
    "TACONIC": "Litchfield",
    "WOODVILLE": "Litchfield",      # village in Washington
    "NEW PRESTON": "Litchfield",    # village in Washington
    "MARBLE DALE": "Litchfield",
    "BANTAM": "Litchfield",         # borough in Litchfield
    "NORTHFIELD": "Litchfield",     # village in Litchfield
    "WOODMONT": "New Haven",        # borough in Milford
    # Additional postal/USPS aliases & common typos seen in the SoS data
    "VERNON ROCKVILLE": "Tolland",        # USPS form for Vernon
    "SOUTHPORT": "Fairfield",             # village in Fairfield town
    "SO GLASTONBURY": "Hartford",
    "S GLASTONBURY": "Hartford",
    "S. GLASTONBURY": "Hartford",
    "WINSTED": "Litchfield",              # village in Winchester
    "OAKVILLE": "Litchfield",             # village in Watertown
    "WASHINGTON DEPOT": "Litchfield",     # village in Washington
    "NORTH GRANBY": "Hartford",           # village in Granby
    "S NORWALK": "Fairfield",
    "S. NORWALK": "Fairfield",
    "SOUTH NORWALK": "Fairfield",
    "MILLDALE": "Hartford",               # village in Southington
    "YALESVILLE": "New Haven",            # village in Wallingford
    "HUNTINGTON": "Fairfield",            # village in Shelton
    "MANSFIELD CENTER": "Tolland",        # village in Mansfield
    "MANSFIELD DEPOT": "Tolland",
    "ROCKFALL": "Middlesex",              # village in Middlefield
    "MIDDLE HADDAM": "Middlesex",         # village in East Hampton
    "MARION": "Hartford",                 # village in Southington
    "NEW PRESTON MARBLE DALE": "Litchfield",
    "MARBLE DALE": "Litchfield",
    "SOUTH LYME": "New London",           # postal village of Old Lyme
    "PINE MEADOW": "Litchfield",          # village in New Hartford
    "WEST CORNWALL": "Litchfield",
    "TRUMBULL CT": "Fairfield",
    # Common typos
    "GREENIWCH": "Fairfield",
    "BRIDEPORT": "Fairfield",
    "BRANDFORD": "New Haven",
    "DABURY": "Fairfield",
    "FAIFIELD": "Fairfield",
    "MYTSIC": "New London",
    "WET HARTFORD": "Hartford",
    "WEST HARTFOED": "Hartford",
    "SIMBSBURY": "Hartford",
}


# ZIP-prefix → county fallback. CT ZIPs cluster by region; this is best-effort
# only and used when a lead has no recognizable city. Edge cases at county
# borders (06410 Cheshire / 06416 Cromwell, 06770 Naugatuck / 06779 Watertown)
# do exist but the city map covers those when populated. The 3-digit prefixes
# below are the dominant county for that ZIP block.
ZIP3_COUNTY: dict[str, str] = {
    "060": "Hartford",   # central CT (Hartford metro core)
    "061": "Hartford",
    "062": "Hartford",   # mostly Hartford W/N suburbs; some Litchfield
    "063": "New London",
    "064": "New Haven",  # Meriden/Wallingford/Branford/Milford/etc.
    "065": "New Haven",  # New Haven proper
    "066": "Fairfield",  # Bridgeport area
    "067": "Litchfield", # NW corner; some Fairfield (Danbury 06810 actually)
    "068": "Fairfield",  # Stamford/Greenwich/Norwalk
    "069": "Fairfield",  # Bridgeport metro south
}


# Search patterns matched against CT business `name` (uppercased).
# SODA `like` is case-sensitive on the lhs; we wrap with upper(name).
SEARCH_PATTERNS = [
    "CONDOMINIUM",
    "HOMEOWNERS",
    "OWNERS ASSOCIATION",
    "PROPERTY OWNERS",
    "CIVIC ASSOCIATION",
    "COMMUNITY ASSOCIATION",
    "TOWNHOUSE",
    "TOWNHOME",
    "VILLAGE ASSOCIATION",
    "VILLAS",
    "ESTATES ASSOCIATION",
    "COMMONS ASSOCIATION",
    "PLANNED COMMUNITY",
    "MASTER ASSOCIATION",
    " HOA",
    " POA",
    "HOMES ASSOCIATION",
    "BEACH CLUB",
    "BEACH ASSOCIATION",
    "HOUSING COOPERATIVE",
    "MUTUAL HOUSING",
]

# Final HOA/condo name validator — catches the loose "VILLAGE" / "ESTATES"
# matches that the SoS registry treats as HOA-shaped names but really aren't
# (e.g. "BEACON VILLAGE LLC", "ESTATES OF GREENWICH REAL ESTATE").
HOA_NAME_RE = re.compile(
    r"\b(condominium|condo|homeowners?|home owners?|owners?\s+(association|assoc)|"
    r"property\s+owners?|civic\s+association|community\s+association|"
    r"master\s+association|townhouse|townhome|"
    r"villas?|village\s+association|commons\s+association|estates\s+association|"
    r"planned\s+community|hoa|poa|residences?\s+association|"
    r"beach\s+(club|association)|cooperative|housing\s+coop|"
    r"mutual\s+housing|co-?op\s+housing)\b",
    re.IGNORECASE,
)

# Tokens that disqualify a candidate even when one of the loose patterns
# matches (CT has lots of yacht/country/golf clubs and civic-improvement
# associations that look HOA-shaped but aren't).
NON_HOA_TOKENS_RE = re.compile(
    r"\b(yacht|country\s+club|golf\s+club|fishing|sportsmen|"
    r"chamber\s+of\s+commerce|civic\s+improvement|civic\s+initiatives|"
    r"realty|real\s+estate|management|broker(age)?|"
    r"church|temple|synagogue|mosque|baptist|methodist|catholic|jewish|"
    r"foundation|charity|charitable|scholarship|"
    r"daycare|montessori|preschool|academy|university|college|"
    r"insurance|agency|consulting|holdings|investments|investment\s+club|"
    r"law\s+(firm|office)|legal\s+services|attorneys?|"
    r"hospital|health|medical|dental|nursing|hospice)\b",
    re.IGNORECASE,
)


@dataclass
class Entity:
    name: str
    sos_id: str
    business_type: str | None
    naics: str | None
    status: str | None
    street: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    mailing_address_raw: str | None
    source_pattern: str

    def to_lead(self) -> dict:
        # Normalize city: collapse whitespace and uppercase before lookup.
        city_key = " ".join((self.city or "").upper().split())
        county = CITY_COUNTY.get(city_key) if city_key else None
        if not county and self.postal_code:
            county = ZIP3_COUNTY.get(self.postal_code[:3])
        return {
            "name": self.name,
            "source": "sos-ct",
            "source_url": (
                # CT does not expose a stable per-entity public summary URL on
                # the Salesforce portal, but the searchable business search
                # page works as a citation.
                f"https://service.ct.gov/business/s/onlinebusinesssearch?language=en_US&AccountNumber={self.sos_id}"
                if self.sos_id else "https://service.ct.gov/business/s/onlinebusinesssearch?language=en_US"
            ),
            "state": "CT",
            "city": self.city.title() if self.city else None,
            "county": county,
            "postal_code": self.postal_code,
        }


def _soda_get(session: requests.Session, params: dict[str, str], *, app_token: str | None) -> list[dict]:
    headers = {}
    if app_token:
        headers["X-App-Token"] = app_token
    r = session.get(ENDPOINT, params=params, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()


def _normalize_zip(z: str | None) -> str | None:
    if not z:
        return None
    z = z.strip()
    if not z:
        return None
    # Pull leading 5-digit ZIP
    m = re.match(r"^(\d{5})", z)
    return m.group(1) if m else None


def _extract_address(row: dict) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Best (street, city, state, zip, mailing_raw) from the row.

    The dataset has structured `billing*` fields populated for active
    entities, plus a free-form `mailing_address` like
    ``"55 byram terrace drive ,A ,greenwich ,CT ,United States ,06831"``
    that is comma-and-space-separated street, unit, city, state, country, zip.
    Prefer the structured fields, fall back to the parsed mailing_address.
    """
    street = (row.get("billingstreet") or "").strip() or None
    city = (row.get("billingcity") or "").strip() or None
    state = (row.get("billingstate") or "").strip().upper() or None
    postal = _normalize_zip(row.get("billingpostalcode"))
    mail_raw = (row.get("mailing_address") or "").strip() or None

    if not (city and state and postal) and mail_raw:
        # Comma-split with whitespace tolerance. Empty entries yield "".
        parts = [p.strip() for p in mail_raw.split(",")]
        # Expect [street, unit, city, state, country, zip]
        if len(parts) >= 6:
            street = street or parts[0] or None
            city = city or parts[2] or None
            state = state or (parts[3].upper() or None)
            postal = postal or _normalize_zip(parts[5])
    return street, city, state, postal, mail_raw


def _row_to_entity(row: dict, *, pattern: str) -> Entity | None:
    name = (row.get("name") or "").strip()
    if not name:
        return None
    street, city, state, postal, mail_raw = _extract_address(row)
    return Entity(
        name=name,
        sos_id=(row.get("accountnumber") or row.get("id") or "").strip(),
        business_type=(row.get("business_type") or None),
        naics=(row.get("naics_code") or None),
        status=(row.get("status") or None),
        street=street,
        city=city,
        state=state,
        postal_code=postal,
        mailing_address_raw=mail_raw,
        source_pattern=pattern,
    )


def scrape_pattern(
    session: requests.Session,
    pattern: str,
    *,
    app_token: str | None,
    page_size: int = 1000,
    polite: float = 0.25,
) -> Iterable[Entity]:
    """Yield Entity objects for one name-pattern. SODA paginates with
    $offset; loop until a short page comes back."""
    offset = 0
    fields = (
        "id,name,status,business_type,accountnumber,naics_code,"
        "billingstreet,billingcity,billingstate,billingpostalcode,"
        "mailing_address"
    )
    while True:
        params = {
            "$select": fields,
            "$where": f"upper(name) like '%{pattern}%' AND status='Active'",
            "$order": "accountnumber",
            "$limit": str(page_size),
            "$offset": str(offset),
        }
        rows = _soda_get(session, params, app_token=app_token)
        if not rows:
            return
        for row in rows:
            ent = _row_to_entity(row, pattern=pattern)
            if ent is not None:
                yield ent
        if len(rows) < page_size:
            return
        offset += page_size
        time.sleep(polite)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "state_scrapers/ct/leads/ct_sos_associations.jsonl"))
    parser.add_argument("--polite-delay", type=float, default=0.25)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--patterns", action="append", default=None,
                        help="Override patterns; repeatable. Default: built-in list")
    parser.add_argument("--include-out-of-state", action="store_true",
                        help="Keep entities whose mailing address is outside CT")
    parser.add_argument("--app-token", default=None,
                        help="Optional SODA app token (X-App-Token); raises rate limit")
    args = parser.parse_args()

    patterns = args.patterns or SEARCH_PATTERNS
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    seen_keys: set[str] = set()
    written = 0
    skipped_oos = 0
    skipped_non_hoa_name = 0
    skipped_non_hoa_token = 0
    by_pattern: dict[str, int] = {p: 0 for p in patterns}

    with out_path.open("w", encoding="utf-8") as f:
        for pat in patterns:
            print(f"[ct-sos] pattern={pat!r}", file=sys.stderr)
            for ent in scrape_pattern(
                session, pat,
                app_token=args.app_token,
                page_size=args.page_size,
                polite=args.polite_delay,
            ):
                if not args.include_out_of_state and (ent.state and ent.state != "CT"):
                    skipped_oos += 1
                    continue
                if not HOA_NAME_RE.search(ent.name):
                    skipped_non_hoa_name += 1
                    continue
                if NON_HOA_TOKENS_RE.search(ent.name):
                    skipped_non_hoa_token += 1
                    continue
                key = ent.sos_id or f"name:{ent.name.lower()}|city:{(ent.city or '').lower()}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                lead = ent.to_lead()
                lead["sos_id"] = ent.sos_id
                lead["sos_business_type"] = ent.business_type
                lead["sos_naics"] = ent.naics
                lead["sos_pattern"] = pat
                lead["sos_address_raw"] = ent.mailing_address_raw
                lead["sos_street"] = ent.street
                f.write(json.dumps(lead, sort_keys=True) + "\n")
                written += 1
                by_pattern[pat] += 1

    summary = {
        "output": str(out_path),
        "written_leads": written,
        "skipped_out_of_state": skipped_oos,
        "skipped_non_hoa_name": skipped_non_hoa_name,
        "skipped_non_hoa_token": skipped_non_hoa_token,
        "by_pattern": by_pattern,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
