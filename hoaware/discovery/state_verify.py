"""State verification from PDF page-1 text."""

import re
from typing import NamedTuple


class StateMatch(NamedTuple):
    state: str | None  # "CA", "NC", etc.
    confidence: str  # "high", "medium", "low"
    evidence: str  # Why we matched it


_STATE_ABBR = {
    "california": "CA",
    "north carolina": "NC",
    "south carolina": "SC",
    "virginia": "VA",
    "georgia": "GA",
    "texas": "TX",
    "florida": "FL",
    "new york": "NY",
    "pennsylvania": "PA",
    "maryland": "MD",
    "new jersey": "NJ",
    "connecticut": "CT",
    "massachusetts": "MA",
    "rhode island": "RI",
    "vermont": "VT",
    "new hampshire": "NH",
    "maine": "ME",
    "delaware": "DE",
    "colorado": "CO",
    "utah": "UT",
    "arizona": "AZ",
    "nevada": "NV",
    "washington": "WA",
    "oregon": "OR",
    "idaho": "ID",
    "montana": "MT",
    "wyoming": "WY",
    "new mexico": "NM",
    "illinois": "IL",
    "indiana": "IN",
    "ohio": "OH",
    "michigan": "MI",
    "wisconsin": "WI",
    "minnesota": "MN",
    "iowa": "IA",
    "missouri": "MO",
    "kansas": "KS",
    "nebraska": "NE",
    "south dakota": "SD",
    "north dakota": "ND",
    "oklahoma": "OK",
    "arkansas": "AR",
    "louisiana": "LA",
    "mississippi": "MS",
    "alabama": "AL",
    "tennessee": "TN",
    "kentucky": "KY",
    "west virginia": "WV",
    "hawaii": "HI",
    "alaska": "AK",
}

_STATE_ABBR_PATTERN = "|".join(
    f"(?:{k}|{v})" for k, v in _STATE_ABBR.items()
)


def verify_state(text: str, expected_state: str | None) -> StateMatch:
    """Extract state from page-1 text of a PDF.

    If expected_state is provided, bump confidence if it's found.
    """
    if not text or len(text) < 10:
        return StateMatch(None, "low", "page too short")

    text_lower = text.lower()

    # Look for explicit state names or abbreviations
    for full_name, abbr in _STATE_ABBR.items():
        if full_name in text_lower or abbr in text_lower.split():
            # If we have an expected state, verify match
            if expected_state and abbr != expected_state:
                continue
            conf = "high" if expected_state and abbr == expected_state else "medium"
            return StateMatch(abbr, conf, f"found '{full_name}' or '{abbr}'")

    # Fallback: if expected_state was provided, assume it's correct
    if expected_state:
        return StateMatch(expected_state, "low", "assumed from lead")

    return StateMatch(None, "low", "no state found in text")
