#!/usr/bin/env python3
"""For each banked GA HOA without a usable owned website, do one Serper
search ("<HOA name> <county> Georgia HOA documents") and pick the first
organic hit whose host looks HOA-owned (not a CDN, not a management
portal, not a directory). Probe that website to harvest more PDFs.

Bank dedup is sha-keyed, so already-banked PDFs are no-ops; new ones
land under the existing HOA's manifest path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from google.cloud import storage as gcs  # noqa: E402

from hoaware.discovery.leads import Lead  # noqa: E402
from hoaware.discovery.probe import probe  # noqa: E402

BUCKET_NAME = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")
SERPER_ENDPOINT = "https://google.serper.dev/search"
USER_AGENT = (
    os.environ.get("HOA_DISCOVERY_USER_AGENT")
    or "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
)

BAD_HOST_RE = re.compile(
    r"(facebook|tiktok|instagram|reddit|nextdoor|"
    r"zillow|redfin|realtor|trulia|apartments|rent|"
    r"scribd|issuu|yumpu|dokumen|pdfcoffee|"
    r"hoaleader|propublica|irs|sec|bbb|yelp|indeed|glassdoor|"
    r"zoominfo|homeownersassociationdirectory|hoa-community|communitypay|"
    r"img1\.wsimg|nebula\.wsimg|static1\.squarespace|cdn\.|s3\.amazonaws|"
    r"dropbox|drive\.google|docs\.google|googleapis|"
    r"ecorp\.sos\.ga|legis\.ga\.gov|"
    r"luederlaw|attorney|associationvoice|gogladly|eneighbors|"
    r"realmanage|fsresidential|cmacommunities|associaonline|hoamanagement|"
    r"steadily|doorloop|nolo|rocketlawyer|avvo|findlaw|justia|caselaw|"
    r"trellis|lawinsider|uslegalforms|"
    r"hoa\.texas\.gov|sfmohcd)\.",
    re.IGNORECASE,
)
BAD_PATH_RE = re.compile(
    r"(/Legislation/|/AgendaCenter/|/Council/|/Planning/)",
    re.IGNORECASE,
)


def looks_owned(url: str, name_slug_words: set[str]) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not host:
        return False
    if BAD_HOST_RE.search(host):
        return False
    if BAD_PATH_RE.search(parsed.path):
        return False
    # Require the HOA's name to appear somewhere in host or path.
    hay = (host + parsed.path).lower()
    return any(w in hay for w in name_slug_words if len(w) >= 4)


def serper_search(query: str, *, num: int = 10) -> list[dict]:
    key = os.environ.get("SERPER_API_KEY")
    if not key:
        raise RuntimeError("SERPER_API_KEY required")
    response = requests.post(
        SERPER_ENDPOINT,
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        json={"q": query, "num": num, "gl": "us", "hl": "en"},
        timeout=20,
    )
    if response.status_code >= 400:
        return []
    return list(response.json().get("organic", []))


def list_ga_manifests(client: gcs.Client) -> list[gcs.Blob]:
    bucket = client.bucket(BUCKET_NAME)
    return [b for b in client.list_blobs(bucket, prefix="v1/GA/") if b.name.endswith("/manifest.json")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pdfs-already", type=int, default=2,
                        help="Skip manifests with at least this many PDFs already banked.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--per-lead-timeout", type=int, default=120)
    parser.add_argument("--probe-delay", type=float, default=1.0)
    parser.add_argument("--search-delay", type=float, default=0.4)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "hoaware"

    client = gcs.Client()
    manifests = list_ga_manifests(client)
    print(f"Found {len(manifests)} GA manifests", file=sys.stderr)

    summary: dict[str, int] = {}
    processed = 0
    for blob in manifests:
        if args.limit and processed >= args.limit:
            break
        try:
            data = json.loads(blob.download_as_bytes())
        except Exception:
            continue
        existing = len(data.get("documents") or [])
        if existing >= args.max_pdfs_already:
            summary["already_full"] = summary.get("already_full", 0) + 1
            continue
        name = data.get("name") or ""
        if len(name) < 6 or any(bad in name.lower() for bad in [
            "section ", " of ", "georgia non", "homeowners associations",
            "documents", "amendment to declaration",
        ]):
            summary["skip_bad_name"] = summary.get("skip_bad_name", 0) + 1
            continue
        addr = data.get("address") or {}
        county = addr.get("county")
        # Build a name-slug words set so we can require name match in URL/host.
        name_words = re.findall(r"[a-z0-9]+", name.lower())
        name_words = {w for w in name_words if w not in {
            "homeowners", "homeowner", "homes", "association", "hoa", "the",
            "of", "and", "inc", "incorporated", "llc", "co", "community",
            "property", "owners", "townhome", "townhomes", "condominium",
            "condo", "condos", "civic",
        }}
        if not name_words:
            summary["skip_no_distinctive_name"] = summary.get("skip_no_distinctive_name", 0) + 1
            continue

        processed += 1
        county_str = f"{county} County" if county else ""
        query = f'"{name}" {county_str} Georgia HOA documents'
        try:
            results = serper_search(query)
        except Exception as exc:
            summary["serper_error"] = summary.get("serper_error", 0) + 1
            print(json.dumps({"slug": blob.name, "status": "serper_error", "error": str(exc)[:200]}))
            time.sleep(args.search_delay)
            continue
        time.sleep(args.search_delay)

        chosen_url: str | None = None
        for row in results:
            link = (row.get("link") or "").split("#", 1)[0]
            if not link.startswith(("http://", "https://")):
                continue
            if looks_owned(link, name_words):
                chosen_url = link
                break
        if not chosen_url:
            summary["no_owned"] = summary.get("no_owned", 0) + 1
            print(json.dumps({"slug": blob.name, "status": "no_owned", "name": name[:60]}))
            continue

        # Use the homepage of the chosen URL so the probe crawls the site.
        parsed = urlparse(chosen_url)
        homepage = f"{parsed.scheme}://{parsed.netloc}/"

        if args.dry_run:
            summary["dry_would_probe"] = summary.get("dry_would_probe", 0) + 1
            print(json.dumps({
                "slug": blob.name, "status": "dry_would_probe",
                "name": name[:60], "homepage": homepage,
            }))
            continue

        lead = Lead(
            name=name,
            source="serper-find-owned-ga",
            source_url=homepage,
            state=addr.get("state") or "GA",
            county=county,
            city=addr.get("city"),
            website=homepage,
        )

        def _handler(signum, frame):
            raise TimeoutError("probe timed out")

        old = signal.signal(signal.SIGALRM, _handler)
        signal.alarm(args.per_lead_timeout)
        try:
            result = probe(lead, max_pdfs=10)
            summary["probed"] = summary.get("probed", 0) + 1
            print(json.dumps({
                "slug": blob.name, "status": "probed",
                "name": name[:60], "homepage": homepage,
                "banked": result.documents_banked,
                "skipped": result.documents_skipped,
            }))
        except TimeoutError:
            summary["timeout"] = summary.get("timeout", 0) + 1
            print(json.dumps({"slug": blob.name, "status": "timeout"}))
        except Exception as exc:
            summary["error"] = summary.get("error", 0) + 1
            print(json.dumps({"slug": blob.name, "status": "error", "error": str(exc)[:200]}))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        time.sleep(args.probe_delay)

    print(json.dumps({"summary": summary, "processed": processed}, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
