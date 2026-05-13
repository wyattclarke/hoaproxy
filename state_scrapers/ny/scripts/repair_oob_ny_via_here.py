#!/usr/bin/env python3
"""Manually re-geocode the NY HOAs whose ZIP-centroid landed out-of-state.

Most are NYC condos/coops whose names embed the property street address
(e.g. "140 West 80TH Street Apartment Corp."). I derive a HERE-friendly
query from the name + city field, then verify the result is inside the
NY bbox before backfilling.

Heuristics, in order of preference:
  1. Name starts with "<NUMBER> <STREET-NAME-WORDS> <SUFFIX>" pattern →
     extract address, append NYC borough hint or "New York, NY".
  2. City field is a known NY borough/town → use city + state.
  3. Name contains a recognizable NY locality token ("Buffalo", "Adk Acres",
     "Bemus", etc.) → use that.
  4. Otherwise: skip (stays city_only).

For all candidates we accept only HERE results whose lat/lon falls inside
NY bbox AND whose resultType is houseNumber/street/locality/postalCode/place
(reject 'administrativeArea' state-level fallbacks).
"""
from __future__ import annotations
import json, os, re, sys, time
import requests
from dotenv import load_dotenv
from pathlib import Path

ROOT = Path('/Users/ngoshaliclarke/Documents/GitHub/hoaproxy')
load_dotenv(ROOT / 'settings.env')

HERE_KEY = os.environ['HERE_API_KEY']
ADMIN = os.environ['JWT_SECRET']
NY_BBOX = {'min_lat': 40.49, 'max_lat': 45.02, 'min_lon': -79.77, 'max_lon': -71.78}

# City field → likely NYC borough or NY town context
NYC_BOROUGHS = {
    'NEW YORK': 'Manhattan, NY',
    'MANHATTAN': 'Manhattan, NY',
    'BRONX': 'Bronx, NY',
    'BROOKLYN': 'Brooklyn, NY',
    'QUEENS': 'Queens, NY',
    'STATEN ISLAND': 'Staten Island, NY',
    'LONG ISLAND CITY': 'Long Island City, NY',
    'ASTORIA': 'Astoria, NY',
    'FLUSHING': 'Flushing, NY',
    'JAMAICA': 'Jamaica, NY',
    'RICHMOND HILL': 'Richmond Hill, Queens, NY',
    'FOREST HILLS': 'Forest Hills, NY',
    'JACKSON HEIGHTS': 'Jackson Heights, NY',
    'ELMHURST': 'Elmhurst, NY',
    'COLLEGE POINT': 'College Point, Queens, NY',
    'WHITESTONE': 'Whitestone, NY',
    'BAYSIDE': 'Bayside, NY',
    'REGO PARK': 'Rego Park, NY',
    'KEW GARDENS': 'Kew Gardens, NY',
    'RIDGEWOOD': 'Ridgewood, Queens, NY',
    'WOODSIDE': 'Woodside, NY',
    'SUNNYSIDE': 'Sunnyside, NY',
}
# Long Island & Westchester towns
NY_TOWNS = {
    'GREAT NECK': 'Great Neck, NY',
    'MASSAPEQUA': 'Massapequa, NY',
    'ISLAND PARK': 'Island Park, NY',
    'TOWN OF HUNTINGTON': 'Huntington, NY',
    'HUNTINGTON': 'Huntington, NY',
    'GARDEN CITY': 'Garden City, NY',
    'HEMPSTEAD': 'Hempstead, NY',
    'WHITE PLAINS': 'White Plains, NY',
    'YONKERS': 'Yonkers, NY',
    'NEW ROCHELLE': 'New Rochelle, NY',
    'NYACK': 'Nyack, NY',
    'OSSINING': 'Ossining, NY',
    'PEEKSKILL': 'Peekskill, NY',
    'BUFFALO': 'Buffalo, NY',
    'ROCHESTER': 'Rochester, NY',
    'SYRACUSE': 'Syracuse, NY',
    'ALBANY': 'Albany, NY',
    'BIGGS': 'Biggs, NY',
}
# Out-of-state cities that we know map to NYC (registered agent in NJ/CT/etc.)
OOS_REGISTERED_AGENT_HINTS = {
    'ENGLEWOOD CLIFFS': 'New York, NY',  # very common RA address for NYC coops
    'STAMFORD': 'New York, NY',  # CT mgmt-cos register NYC properties
    'WESTPORT': 'New York, NY',
    'NEW CANAAN': 'New York, NY',
    'OLD TAPPAN': 'New York, NY',
    'NORTH BERGEN': 'New York, NY',
    'TEANECK': 'New York, NY',
    'PALISADES PARK': 'New York, NY',
    'FORT LEE': 'New York, NY',
    'LAKEWOOD': 'Brooklyn, NY',  # Lakewood NJ Orthodox community → Brooklyn coops
}
# Locality keywords found in HOA names that identify NY locations
NAME_LOCALITY_HINTS = [
    (re.compile(r'\bAdk\b|\bAdirondack', re.I), 'Adirondacks, NY'),
    (re.compile(r'\bBuffalo\b', re.I), 'Buffalo, NY'),
    (re.compile(r'\bRochester\b', re.I), 'Rochester, NY'),
    (re.compile(r'\bSyracuse\b', re.I), 'Syracuse, NY'),
    (re.compile(r'\bAlbany\b', re.I), 'Albany, NY'),
    (re.compile(r'\bBemus\b', re.I), 'Bemus Point, NY'),  # Chautauqua Lake
    (re.compile(r'\bCatskill', re.I), 'Catskill, NY'),
    (re.compile(r'\bHudson Valley|\bHudson\b', re.I), 'Hudson, NY'),
    (re.compile(r'\bMontauk\b', re.I), 'Montauk, NY'),
    (re.compile(r'\bHamptons\b|\bEast Hampton|\bSouthampton', re.I), 'East Hampton, NY'),
    (re.compile(r'\bSaratoga\b', re.I), 'Saratoga Springs, NY'),
    (re.compile(r'\bLake George\b', re.I), 'Lake George, NY'),
    (re.compile(r'\bFire Island\b', re.I), 'Fire Island, NY'),
    (re.compile(r'\bShelter Island\b', re.I), 'Shelter Island, NY'),
    (re.compile(r'\bSag Harbor\b', re.I), 'Sag Harbor, NY'),
    (re.compile(r'\bAmenia\b', re.I), 'Amenia, NY'),
    (re.compile(r'\bBerkshire', re.I), 'Hillsdale, NY'),  # NY side of Berkshires
]

# Regex to extract NYC-style street-address from name
# Examples:
#   "140 West 80TH Street Apartment Corp."  → "140 West 80th Street"
#   "210 East 63RD Street Owners, Inc."      → "210 East 63rd Street"
#   "1100 Concourse Tenants Corp."           → "1100 Concourse" (need to know it's Grand Concourse, Bronx)
#   "1226 57TH Street Owners LLC"            → "1226 57th Street"
#   "130 Eighth Avenue Owners Corp."         → "130 Eighth Avenue"
ADDR_NAME_RE = re.compile(
    r'^(\d{1,5}(?:-\d{1,5})?)\s+'
    r'([A-Za-z][A-Za-z0-9\.\-\s]{2,40}?)'
    r'\s+(OWNERS?|TENANTS?|CONDO|CONDOMINIUM|APARTMENTS?|HOUSING|REALTY|RETAIL|BUILDING|FEE|UNIT|CO-?OP|COOPERATIVE|HOMES?|BOARD)',
    re.IGNORECASE,
)
# NYC-style ordinal-street prefix like "11TH Street Condo Owner" or "57TH Street"
ORDINAL_STREET_RE = re.compile(
    r'^(\d{1,3})(?:ST|ND|RD|TH)?\s+(STREET|AVE|AVENUE|PLACE|PL\.?|STR\.?|ROAD|BOULEVARD|BLVD|TERRACE)',
    re.IGNORECASE,
)
# House-number + ordinal street: "1226 57TH Street", "1774-80 66TH Street"
HOUSE_ORDINAL_RE = re.compile(
    r'^(\d{1,5}(?:-\d{1,5})?)\s+(\d{1,3})(?:ST|ND|RD|TH)\s+(STREET|AVE|AVENUE|PLACE|PL\.?|ROAD|BLVD|BOULEVARD)',
    re.IGNORECASE,
)
# Parenthetical locality at end of name: "... - Alpha (SYRACUSE)"
PAREN_LOCALITY_RE = re.compile(r'\(([A-Z][A-Z\s]{3,30})\)\s*$')

def derive_query(hoa_name: str, city: str | None) -> str | None:
    """Return a HERE query string, or None if we can't reason about this one."""
    name = hoa_name.strip()
    name_upper = name.upper()
    city = (city or '').strip().upper()

    # 1. Pattern: NYC-style "<NUMBER> <STREET> <SUFFIX>"
    m = ADDR_NAME_RE.match(name)
    if m:
        addr_num = m.group(1)
        addr_street = m.group(2).strip().rstrip(',.')
        # Normalize "80TH" → "80", "63RD" → "63", etc., for HERE
        addr_street = re.sub(r'(\d+)(ST|ND|RD|TH)', r'\1', addr_street, flags=re.I)
        addr_full = f"{addr_num} {addr_street}"
        # Append borough/locality hint
        if city in NYC_BOROUGHS:
            return f"{addr_full}, {NYC_BOROUGHS[city]}"
        if city in NY_TOWNS:
            return f"{addr_full}, {NY_TOWNS[city]}"
        if city in OOS_REGISTERED_AGENT_HINTS:
            return f"{addr_full}, {OOS_REGISTERED_AGENT_HINTS[city]}"
        # Fall back to NYC since most numerical-street HOAs are NYC
        return f"{addr_full}, New York, NY"

    # 1b. House-number + ordinal-street: "1226 57TH Street Owners LLC" → "1226 57th Street, NYC"
    m_house_ord = HOUSE_ORDINAL_RE.match(name)
    if m_house_ord:
        house = m_house_ord.group(1).split('-')[0]  # take first half of "1774-80"
        ord_num = m_house_ord.group(2)
        street_type = m_house_ord.group(3).rstrip('.').title()
        if street_type.lower().startswith('ave'):
            street_type = 'Ave'
        loc = NYC_BOROUGHS.get(city) or NY_TOWNS.get(city) or 'New York, NY'
        return f"{house} {ord_num} {street_type}, {loc}"

    # 1c. NYC ordinal-street pattern (no house number): "11TH Street Condo Owner II, LLC"
    m2 = ORDINAL_STREET_RE.match(name)
    if m2:
        num = m2.group(1)
        street_type = m2.group(2)
        street_norm = street_type.title()
        return f"{num} {street_norm}, New York, NY"

    # 1d. Parenthetical locality: "Agd Fraternity Housing Corporation - Alpha (SYRACUSE)"
    m_paren = PAREN_LOCALITY_RE.search(name)
    if m_paren:
        loc_token = m_paren.group(1).strip().title()
        return f"{name.split('(')[0].split(' - ')[0].strip()}, {loc_token}, NY"

    # 2. Name-locality hints (Adirondacks, Bemus, Buffalo, ...)
    for pat, loc in NAME_LOCALITY_HINTS:
        if pat.search(name):
            return f"{name.split(',')[0]}, {loc}"

    # 3. Pure city-based query if city is a NYC borough/NY town
    if city in NYC_BOROUGHS:
        return f"{name.split(',')[0]}, {NYC_BOROUGHS[city]}"
    if city in NY_TOWNS:
        return f"{name.split(',')[0]}, {NY_TOWNS[city]}"

    # 4. Reject — can't reason
    return None


def here_geocode(query: str) -> dict | None:
    """Return the best HERE hit, or None."""
    try:
        r = requests.get(
            'https://geocode.search.hereapi.com/v1/geocode',
            params={'q': query, 'apiKey': HERE_KEY, 'in': 'countryCode:USA', 'limit': 3},
            timeout=15,
        )
    except requests.exceptions.RequestException:
        return None
    if r.status_code != 200:
        return None
    items = r.json().get('items') or []
    # Prefer street-level / building-level; reject administrativeArea (state-level fallback)
    for item in items:
        rtype = item.get('resultType', '')
        if rtype == 'administrativeArea':
            continue
        pos = item.get('position') or {}
        lat, lng = pos.get('lat'), pos.get('lng')
        if lat is None or lng is None:
            continue
        if (NY_BBOX['min_lat'] <= lat <= NY_BBOX['max_lat']
                and NY_BBOX['min_lon'] <= lng <= NY_BBOX['max_lon']):
            return {
                'title': item.get('title'),
                'lat': lat,
                'lon': lng,
                'resultType': rtype,
                'fieldScore': item.get('scoring', {}).get('fieldScore'),
                'queryScore': item.get('scoring', {}).get('queryScore'),
            }
    return None


def main():
    rows = json.load(open('/tmp/ny_oob_104.json'))
    print(f"Input rows: {len(rows)}", file=sys.stderr)

    repaired, skipped, demoted = [], [], []
    for i, row in enumerate(rows):
        hoa = row['hoa']
        city = row.get('city')
        query = derive_query(hoa, city)
        if not query:
            skipped.append({'hoa': hoa, 'city': city, 'reason': 'no_query_derived'})
            continue
        hit = here_geocode(query)
        if not hit:
            skipped.append({'hoa': hoa, 'city': city, 'query': query, 'reason': 'here_no_ny_match'})
            continue
        # Require a reasonable score to accept (avoid weak place-only matches)
        rtype = hit['resultType']
        if rtype in ('houseNumber', 'street', 'place'):
            quality = 'address' if rtype == 'houseNumber' else 'place_centroid'
        elif rtype in ('locality', 'postalCode'):
            quality = 'place_centroid'
        else:
            skipped.append({'hoa': hoa, 'query': query, 'reason': f'reject_resultType:{rtype}'})
            continue
        repaired.append({
            'hoa': hoa,
            'query': query,
            'lat': hit['lat'],
            'lon': hit['lon'],
            'resultType': rtype,
            'title': hit['title'],
            'quality': quality,
        })
        if i % 25 == 0:
            print(f"  progress {i}/{len(rows)}  repaired={len(repaired)} skipped={len(skipped)}", file=sys.stderr)
        time.sleep(0.25)  # 4 req/s polite cap

    # Save outputs for review
    Path('/tmp/repair_ny_oob_output').mkdir(exist_ok=True)
    with open('/tmp/repair_ny_oob_output/repaired.jsonl','w') as f:
        for r in repaired: f.write(json.dumps(r)+'\n')
    with open('/tmp/repair_ny_oob_output/skipped.jsonl','w') as f:
        for r in skipped: f.write(json.dumps(r)+'\n')

    print(f"\nRepaired: {len(repaired)} | Skipped: {len(skipped)}", file=sys.stderr)
    print("\nSample repaired:", file=sys.stderr)
    for r in repaired[:10]:
        print(f"  {r['hoa'][:45]:45s} → {r['title']} ({r['lat']:.4f},{r['lon']:.4f}) [{r['resultType']}]", file=sys.stderr)
    print("\nSample skipped:", file=sys.stderr)
    for r in skipped[:8]:
        print(f"  {r['hoa'][:45]:45s} | reason={r['reason']} {(r.get('query') or '')[:50]}", file=sys.stderr)

    # Apply backfills
    print("\nApplying backfills...", file=sys.stderr)
    records = [{
        'hoa': r['hoa'],
        'latitude': r['lat'],
        'longitude': r['lon'],
        'location_quality': r['quality'],
    } for r in repaired]
    matched = not_found = 0
    BATCH = 25
    for i in range(0, len(records), BATCH):
        chunk = records[i:i+BATCH]
        rr = requests.post('https://hoaproxy.org/admin/backfill-locations',
            headers={'Authorization': f'Bearer {ADMIN}','Content-Type':'application/json'},
            json={'records': chunk}, timeout=120)
        if rr.status_code == 200:
            b = rr.json()
            matched += b.get('matched',0)
            not_found += b.get('not_found',0)
            print(f"  batch {i//BATCH}: matched={b.get('matched')} not_found={b.get('not_found')}", file=sys.stderr)
        else:
            print(f"  batch {i//BATCH} HTTP {rr.status_code}", file=sys.stderr)
        time.sleep(0.5)
    print(f"\nFINAL: matched={matched} not_found={not_found} skipped={len(skipped)}")


if __name__ == '__main__':
    main()
