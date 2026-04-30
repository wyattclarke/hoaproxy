"""Platform detection from homepage HTML.

Two-stage:
1. Structural platform — what the site is built on (WordPress, Squarespace, Wix, etc.).
   These are NOT walled even if they embed login widgets.
2. Walled-only platform — the entire site IS a portal (TownSq, FrontSteps, etc.). Only
   classified as walled if no structural platform is detected (so a WordPress site with
   a TownSq login link reads as 'wordpress', not 'townsq').
"""

import re
from typing import NamedTuple


class Platform(NamedTuple):
    name: str
    is_walled: bool


# Structural CMS / static-site platforms — content here is generally public.
_STRUCTURAL = [
    (r"wp-content|wp-includes|wp-json|/wp/v2/|wordpress", "wordpress"),
    (r"squarespace\.com|static1\.squarespace|squarespace-cdn", "squarespace"),
    (r"wixstatic\.com|wix\.com|static\.wix\.com", "wix"),
    (r"weebly\.com|weeblycloud", "weebly"),
    (r"webflow\.io|webflow\.com", "webflow"),
    (r"shopify\.com|cdn\.shopify", "shopify"),
    (r"hubspot|hsforms\.net", "hubspot"),
]

# Whole-site walled portals. Only authoritative if no structural platform found.
_WALLED = [
    (r"townsq\.com|townsq\.io|powered\s+by\s+townsq", "townsq"),
    (r"frontsteps\.com|frontsteps\.io|powered\s+by\s+frontsteps", "frontsteps"),
    (r"connectresident\.com|powered\s+by\s+connectresident", "connectresident"),
    (r"cinc\s*systems|cincsystems|cincweb|cinc\.io", "cinc"),
    (r"caliber\.cloud|calibersoftware|caliber\s+web\s+axis", "caliber"),
    (r"enumerate\s*engage|enumerateengage|tops\s*one", "enumerate"),
    (r"appfolio\.com|appfolioassets", "appfolio"),
    (r"buildium\.com", "buildium"),
    (r"vinteum\.com|neigbrs\.com", "vinteum"),
]


def _first_match(html_lower: str, patterns) -> str | None:
    for pat, name in patterns:
        if re.search(pat, html_lower, re.IGNORECASE):
            return name
    return None


def fingerprint(html: str) -> Platform:
    if not html:
        return Platform("unknown", False)
    h = html.lower()

    structural = _first_match(h, _STRUCTURAL)
    if structural:
        return Platform(structural, False)

    walled = _first_match(h, _WALLED)
    if walled:
        return Platform(walled, True)

    if len(html) > 1000 and "<" in html:
        return Platform("static", False)
    return Platform("unknown", False)
