#!/usr/bin/env python3
"""Driver D — CA DRE Subdivision Public Reports scraper.

CA-unique discovery driver. The CA Department of Real Estate (DRE) issues
a Public Report for every CA subdivision >5 units. These reports often
include the recorded CC&R URL or summary, and capture *new construction*
that lags 2 years behind the SI-CID biennial filing.

Status: STUB — first-session scaffold. Hardens in subsequent sessions.

Public lookup endpoint:
    https://www2.dre.ca.gov/PublicASP/SubdivisionsPub.aspx

Search interface accepts:
    - county name
    - city name
    - subdivision name
    - file number
    - zip code

Per-result page links to the public-report PDF (if filed) and lists
public-report metadata (project name, address, units, file date).

Strategy:
    1. For each CA county, POST search form with county name.
    2. Paginate through results; collect file numbers + project names.
    3. For each file number, fetch the public-report PDF URL (if any).
    4. Emit DRE_LEAD format compatible with the bank pipeline:
       {name, county, city, dre_file_no, dre_report_url}
    5. Hand off to discovery probe to bank manifest by SHA.

Implementation notes:
    - The page is ASP.NET WebForms with __VIEWSTATE / __EVENTVALIDATION
      hidden inputs. Use httpx.Client with cookie persistence.
    - Search is paginated by __EVENTTARGET=ctl00$.. on page-link clicks.
    - Reports are stored as PDFs at https://www2.dre.ca.gov/PUBSExt/...

This is a hard-mode scrape: WebForms postbacks + viewstate. Estimate
~2-3 hours to harden once we start. Stub avoids blocking other Phase 2
drivers.

Recommended invocation (when implemented):
    python state_scrapers/ca/scripts/dre_subdivision_scrape.py \\
        --county "Los Angeles" \\
        --output state_scrapers/ca/results/dre_los-angeles/leads.jsonl \\
        --max-results 5000

Per-county wall time expected: ~10-30 min depending on county size.
Spend: $0 (DRE public lookup is free).
"""
from __future__ import annotations

import argparse
import sys

DRE_SEARCH_URL = "https://www2.dre.ca.gov/PublicASP/SubdivisionsPub.aspx"
DRE_REPORT_BASE = "https://www2.dre.ca.gov/PUBSExt/"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", required=True, help="County display name (e.g. 'Los Angeles')")
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--max-results", type=int, default=5000)
    args = ap.parse_args()

    print(
        "STUB: DRE Subdivision Public Reports scraper not yet implemented.\n"
        f"  county: {args.county}\n"
        f"  output: {args.output}\n"
        f"  max-results: {args.max_results}\n"
        "\n"
        "To implement:\n"
        "  1. POST DRE_SEARCH_URL with viewstate + county filter.\n"
        "  2. Paginate result pages; collect file numbers + project names.\n"
        "  3. Fetch public-report PDF (DRE_REPORT_BASE/<file_no>.pdf).\n"
        "  4. Emit DRE_LEAD format compatible with bank pipeline.\n"
        "\n"
        f"DRE search URL: {DRE_SEARCH_URL}\n",
        file=sys.stderr,
    )
    return 78  # EX_CONFIG: stub not implemented


if __name__ == "__main__":
    raise SystemExit(main())
