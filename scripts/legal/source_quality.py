from __future__ import annotations

from urllib.parse import urlparse


OFFICIAL_HOST_ALLOWLIST = {
    "app.leg.wa.gov",
    "delcode.delaware.gov",
    "law.lis.virginia.gov",
    "statutes.capitol.texas.gov",
    "leginfo.legislature.ca.gov",
    "code.wvlegislature.gov",
    "www.gencourt.state.nh.us",
    "gc.nh.gov",
    "www.mainelegislature.org",
    "www.nysenate.gov",
    "legislation.nysenate.gov",
    "www.ncleg.net",
    "billstatus.ls.state.ms.us",
    "index.ls.state.ms.us",
    # Oklahoma State Courts Network — official OK legal publisher
    "www.oscn.net",
    "oscn.net",
    # Pennsylvania General Assembly
    "www.palegis.us",
    "palegis.us",
    # South Dakota Legislature
    "sdlegislature.gov",
    "www.sdlegislature.gov",
    # Wyoming Legislature
    "wyoleg.gov",
    "www.wyoleg.gov",
}

AGGREGATOR_HOST_TOKENS = (
    "justia.com",
    "lexisnexis.com",
    "westlaw.com",
    "findlaw.com",
    "casetext.com",
    "lawserver.com",
    "law.onecle.com",
)

PRIMARY_SOURCE_TYPES = {
    "statute",
    "regulation",
    "constitution",
    "session_law",
}


def host_for_url(source_url: str) -> str:
    try:
        return (urlparse(source_url).netloc or "").lower().strip()
    except Exception:
        return ""


def is_aggregator_host(host: str) -> bool:
    return any(token in host for token in AGGREGATOR_HOST_TOKENS)


def is_official_host(host: str) -> bool:
    if not host:
        return False
    if host in OFFICIAL_HOST_ALLOWLIST:
        return True
    if host.endswith(".gov"):
        return True
    # Common official legislature host patterns that are not always .gov.
    if ".state." in host and ("leg" in host or "law" in host or "code" in host):
        return True
    return False


def classify_source_quality(
    *,
    source_type: str | None,
    source_url: str | None,
) -> str:
    source_type_norm = str(source_type or "").strip().lower()
    source_url_norm = str(source_url or "").strip()
    host = host_for_url(source_url_norm)

    if source_type_norm == "secondary_aggregator" or is_aggregator_host(host):
        return "aggregator"
    if is_official_host(host):
        if source_type_norm in PRIMARY_SOURCE_TYPES:
            return "official_primary"
        return "official_secondary"
    return "unknown"


def extraction_allowed(*, source_quality: str, include_aggregators: bool = False) -> bool:
    quality = str(source_quality or "unknown").strip().lower()
    if quality in {"official_primary", "official_secondary"}:
        return True
    if quality == "aggregator":
        return bool(include_aggregators)
    return False
