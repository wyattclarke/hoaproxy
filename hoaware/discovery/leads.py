"""Lead dataclass for discovery."""

from dataclasses import dataclass


@dataclass
class Lead:
    """A potential HOA to probe.

    Minimal fields; discovery fills in address, geometry, website details.
    """

    name: str
    source: str  # "sos-ca", "aggregator-nc", "search-serper", etc.
    source_url: str
    state: str | None = None  # "CA", "NC", etc. or None if unknown
    city: str | None = None
    website: str | None = None
    county: str | None = None
