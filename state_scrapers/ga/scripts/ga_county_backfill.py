#!/usr/bin/env python3
"""Re-route banked GA `_unknown-county/...` manifests under the right county.

Walks every `gs://hoaproxy-bank/v1/GA/_unknown-county/<slug>/manifest.json`,
infers a Georgia county from (in order): manifest.address.county/city,
PDF first-page text, source URL host, and HOA name. If a county is
identified, copies all blobs under the old prefix to the new
`gs://hoaproxy-bank/v1/GA/<county-slug>/<slug>/...` prefix using server-side
GCS rewrite, rewrites the manifest's address.county, then deletes the old
prefix. No OpenRouter calls.

Skips manifests it can't confidently route — those stay under
`_unknown-county/` and a future pass can try harder.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from google.cloud import storage as gcs  # noqa: E402

from hoaware.bank import slugify  # noqa: E402

BUCKET_NAME = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")
STATE_PREFIX = "v1/GA"
UNKNOWN_PREFIX = f"{STATE_PREFIX}/_unknown-county/"

GA_COUNTIES = [
    "Appling", "Atkinson", "Bacon", "Baker", "Baldwin", "Banks", "Barrow",
    "Bartow", "Ben Hill", "Berrien", "Bibb", "Bleckley", "Brantley", "Brooks",
    "Bryan", "Bulloch", "Burke", "Butts", "Calhoun", "Camden", "Candler",
    "Carroll", "Catoosa", "Charlton", "Chatham", "Chattahoochee", "Chattooga",
    "Cherokee", "Clarke", "Clay", "Clayton", "Clinch", "Cobb", "Coffee",
    "Colquitt", "Columbia", "Cook", "Coweta", "Crawford", "Crisp", "Dade",
    "Dawson", "Decatur", "DeKalb", "Dodge", "Dooly", "Dougherty", "Douglas",
    "Early", "Echols", "Effingham", "Elbert", "Emanuel", "Evans", "Fannin",
    "Fayette", "Floyd", "Forsyth", "Franklin", "Fulton", "Gilmer", "Glascock",
    "Glynn", "Gordon", "Grady", "Greene", "Gwinnett", "Habersham", "Hall",
    "Hancock", "Haralson", "Harris", "Hart", "Heard", "Henry", "Houston",
    "Irwin", "Jackson", "Jasper", "Jeff Davis", "Jefferson", "Jenkins",
    "Johnson", "Jones", "Lamar", "Lanier", "Laurens", "Lee", "Liberty",
    "Lincoln", "Long", "Lowndes", "Lumpkin", "McDuffie", "McIntosh", "Macon",
    "Madison", "Marion", "Meriwether", "Miller", "Mitchell", "Monroe",
    "Montgomery", "Morgan", "Murray", "Muscogee", "Newton", "Oconee",
    "Oglethorpe", "Paulding", "Peach", "Pickens", "Pierce", "Pike", "Polk",
    "Pulaski", "Putnam", "Quitman", "Rabun", "Randolph", "Richmond",
    "Rockdale", "Schley", "Screven", "Seminole", "Spalding", "Stephens",
    "Stewart", "Sumter", "Talbot", "Taliaferro", "Tattnall", "Taylor",
    "Telfair", "Terrell", "Thomas", "Tift", "Toombs", "Towns", "Treutlen",
    "Troup", "Turner", "Twiggs", "Union", "Upson", "Walker", "Walton", "Ware",
    "Warren", "Washington", "Wayne", "Webster", "Wheeler", "White", "Whitfield",
    "Wilcox", "Wilkes", "Wilkinson", "Worth",
]

# City → County for the 80-ish biggest GA cities + a long tail of suburbs.
# Used as a backstop when PDF text doesn't include "X County, Georgia".
CITY_TO_COUNTY: dict[str, str] = {
    # Atlanta metro
    "atlanta": "Fulton", "sandy springs": "Fulton", "alpharetta": "Fulton",
    "johns creek": "Fulton", "milton": "Fulton", "roswell": "Fulton",
    "east point": "Fulton", "college park": "Fulton", "union city": "Fulton",
    "fairburn": "Fulton", "palmetto": "Fulton", "chattahoochee hills": "Fulton",
    "buckhead": "Fulton", "vinings": "Cobb",
    "lawrenceville": "Gwinnett", "duluth": "Gwinnett", "suwanee": "Gwinnett",
    "snellville": "Gwinnett", "peachtree corners": "Gwinnett",
    "lilburn": "Gwinnett", "norcross": "Gwinnett", "dacula": "Gwinnett",
    "buford": "Gwinnett", "grayson": "Gwinnett", "loganville": "Gwinnett",
    "sugar hill": "Gwinnett", "berkeley lake": "Gwinnett",
    "marietta": "Cobb", "smyrna": "Cobb", "kennesaw": "Cobb",
    "acworth": "Cobb", "powder springs": "Cobb", "austell": "Cobb",
    "mableton": "Cobb",
    "decatur": "DeKalb", "dunwoody": "DeKalb", "brookhaven": "DeKalb",
    "tucker": "DeKalb", "stone mountain": "DeKalb", "chamblee": "DeKalb",
    "doraville": "DeKalb", "lithonia": "DeKalb", "clarkston": "DeKalb",
    "avondale estates": "DeKalb", "pine lake": "DeKalb",
    "woodstock": "Cherokee", "canton": "Cherokee",
    "holly springs": "Cherokee", "ball ground": "Cherokee", "waleska": "Cherokee",
    "mcdonough": "Henry", "stockbridge": "Henry", "locust grove": "Henry",
    "hampton": "Henry",
    "peachtree city": "Fayette", "fayetteville": "Fayette", "tyrone": "Fayette",
    "newnan": "Coweta", "senoia": "Coweta", "sharpsburg": "Coweta",
    "douglasville": "Douglas", "lithia springs": "Douglas",
    "dallas": "Paulding", "hiram": "Paulding",
    "cumming": "Forsyth",
    "jonesboro": "Clayton", "riverdale": "Clayton", "morrow": "Clayton",
    "forest park": "Clayton",
    "gainesville": "Hall", "flowery branch": "Hall", "oakwood": "Hall",
    "savannah": "Chatham", "pooler": "Chatham", "garden city": "Chatham",
    "tybee island": "Chatham", "wilmington island": "Chatham",
    "skidaway": "Chatham",
    "augusta": "Richmond", "fort gordon": "Richmond",
    "evans": "Columbia", "martinez": "Columbia", "grovetown": "Columbia",
    "macon": "Bibb",
    "warner robins": "Houston", "perry": "Houston", "centerville": "Houston",
    "covington": "Newton", "oxford": "Newton",
    "monroe": "Walton", "social circle": "Walton",
    "carrollton": "Carroll", "villa rica": "Carroll", "bremen": "Carroll",
    "cartersville": "Bartow",
    "rome": "Floyd", "lindale": "Floyd",
    "calhoun": "Gordon",
    "dalton": "Whitfield", "tunnel hill": "Whitfield",
    "athens": "Clarke", "athens-clarke": "Clarke",
    "watkinsville": "Oconee", "bishop": "Oconee",
    "bogart": "Oconee",
    "madison": "Morgan",
    "dahlonega": "Lumpkin",
    "cleveland": "White", "helen": "White",
    "ellijay": "Gilmer",
    "blue ridge": "Fannin", "mccaysville": "Fannin",
    "blairsville": "Union",
    "hiawassee": "Towns",
    "clayton": "Rabun", "lake burton": "Rabun", "lake rabun": "Rabun",
    "sky valley": "Rabun",
    "cornelia": "Habersham", "demorest": "Habersham", "clarkesville": "Habersham",
    "jasper": "Pickens",
    "dawsonville": "Dawson",
    "big canoe": "Dawson",
    "valdosta": "Lowndes", "lake park": "Lowndes",
    "tifton": "Tift",
    "thomasville": "Thomas",
    "albany": "Dougherty",
    "americus": "Sumter",
    "moultrie": "Colquitt",
    "douglas": "Coffee",
    "waycross": "Ware",
    "jesup": "Wayne",
    "brunswick": "Glynn", "st. simons": "Glynn", "st simons": "Glynn",
    "sea island": "Glynn", "jekyll island": "Glynn",
    "st. marys": "Camden", "st marys": "Camden", "kingsland": "Camden",
    "hinesville": "Liberty", "midway": "Liberty",
    "richmond hill": "Bryan", "pembroke": "Bryan",
    "statesboro": "Bulloch",
    "vidalia": "Toombs", "lyons": "Toombs",
    "baxley": "Appling",
    "milledgeville": "Baldwin",
    "dublin": "Laurens",
    "sandersville": "Washington",
    "eatonton": "Putnam",
    "forsyth": "Monroe",
    "griffin": "Spalding",
    "thomaston": "Upson",
    "barnesville": "Lamar",
    "lagrange": "Troup", "west point": "Troup", "hogansville": "Troup",
    "columbus": "Muscogee",
    "fort benning": "Muscogee",
    "fort valley": "Peach",
    "elberton": "Elbert",
    "hartwell": "Hart",
    "toccoa": "Stephens",
    "lavonia": "Franklin",
    "commerce": "Jackson", "jefferson": "Jackson", "braselton": "Jackson",
    "winder": "Barrow", "auburn": "Barrow", "statham": "Barrow",
    "conyers": "Rockdale",
    "monticello": "Jasper",
    "greensboro": "Greene",
    "lake oconee": "Greene",
    "social circle": "Walton",
    "douglas": "Coffee",
}

NAME_HINT_RE = re.compile(
    r"\b("
    + "|".join(re.escape(c.lower()) for c in GA_COUNTIES)
    + r")\s*county\b",
    re.IGNORECASE,
)


def infer_county_from_text(text: str) -> str | None:
    """Find 'X County' references in PDF text and return the canonical county name."""
    if not text:
        return None
    counter: dict[str, int] = {}
    for m in NAME_HINT_RE.finditer(text):
        canon = m.group(1).strip().title()
        # Re-canonicalize per CITY case (DeKalb, McDuffie, etc.)
        for c in GA_COUNTIES:
            if c.lower() == canon.lower():
                canon = c
                break
        counter[canon] = counter.get(canon, 0) + 1
    if not counter:
        return None
    return max(counter.items(), key=lambda kv: kv[1])[0]


def infer_county_from_name_or_url(name: str, source_url: str | None) -> str | None:
    """City-based fallback. Looks for a known GA city in the HOA name or URL."""
    haystacks = [(name or "").lower(), (source_url or "").lower()]
    hay = " ".join(haystacks)
    # Check longer city names first to avoid "athens" matching "newathens".
    for city in sorted(CITY_TO_COUNTY.keys(), key=lambda s: -len(s)):
        if re.search(rf"\b{re.escape(city)}\b", hay):
            return CITY_TO_COUNTY[city]
    return None


def infer_county_from_city_in_text(text: str) -> str | None:
    """Look for a known GA city in PDF text. Counts matches and picks most-mentioned."""
    if not text:
        return None
    low = text.lower()
    counter: dict[str, int] = {}
    for city in sorted(CITY_TO_COUNTY.keys(), key=lambda s: -len(s)):
        n = len(re.findall(rf"\b{re.escape(city)}\b", low))
        if n:
            county = CITY_TO_COUNTY[city]
            counter[county] = counter.get(county, 0) + n
    if not counter:
        return None
    return max(counter.items(), key=lambda kv: kv[1])[0]


def extract_pdf_text(blob: gcs.Blob, max_bytes: int = 600_000) -> str:
    """Pull the first ~600KB of a PDF and extract first ~6 pages. Cheap-ish.

    Many GA declarations put the recording county on page 1 (cover) but
    others bury it in the witness/recording-clerk block on page 5+.
    """
    import pypdf

    try:
        data = blob.download_as_bytes(start=0, end=max_bytes - 1)
    except Exception:
        try:
            data = blob.download_as_bytes()
        except Exception:
            return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception:
        return ""
    parts: list[str] = []
    for i in range(min(6, len(reader.pages))):
        try:
            parts.append(reader.pages[i].extract_text() or "")
        except Exception:
            continue
    # Also grab the LAST few pages — recording info often lives there.
    if len(reader.pages) > 6:
        for i in range(max(6, len(reader.pages) - 3), len(reader.pages)):
            try:
                parts.append(reader.pages[i].extract_text() or "")
            except Exception:
                continue
    return "\n".join(parts)[:25000]


def list_unknown_county_manifests(client: gcs.Client) -> list[gcs.Blob]:
    bucket = client.bucket(BUCKET_NAME)
    out = []
    for blob in client.list_blobs(bucket, prefix=UNKNOWN_PREFIX):
        if blob.name.endswith("/manifest.json"):
            out.append(blob)
    return out


def copy_prefix(
    client: gcs.Client,
    old_prefix: str,
    new_prefix: str,
) -> list[str]:
    """Copy every blob under old_prefix to new_prefix. Returns list of new names."""
    bucket = client.bucket(BUCKET_NAME)
    new_names: list[str] = []
    for blob in client.list_blobs(bucket, prefix=old_prefix + "/"):
        rel = blob.name[len(old_prefix) + 1 :]
        new_name = f"{new_prefix}/{rel}"
        # GCS server-side copy (rewrite under the hood)
        bucket.copy_blob(blob, bucket, new_name=new_name)
        new_names.append(new_name)
    return new_names


def delete_prefix(client: gcs.Client, prefix: str) -> int:
    bucket = client.bucket(BUCKET_NAME)
    n = 0
    for blob in client.list_blobs(bucket, prefix=prefix + "/"):
        blob.delete()
        n += 1
    return n


def update_manifest_county(
    client: gcs.Client,
    new_prefix: str,
    county: str,
    new_gcs_paths: dict[str, str],
) -> None:
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(f"{new_prefix}/manifest.json")
    raw = blob.download_as_bytes()
    try:
        data = json.loads(raw)
    except Exception:
        return
    addr = data.setdefault("address", {})
    addr["county"] = county
    # Fix every document.gcs_path so it points at the new prefix.
    for doc in data.get("documents", []):
        old_path = doc.get("gcs_path")
        if old_path and old_path in new_gcs_paths:
            doc["gcs_path"] = new_gcs_paths[old_path]
    blob.upload_from_string(
        json.dumps(data, indent=2, sort_keys=True),
        content_type="application/json",
    )


def process_manifest(
    client: gcs.Client,
    manifest_blob: gcs.Blob,
    *,
    dry_run: bool,
) -> dict:
    bucket = client.bucket(BUCKET_NAME)
    name_parts = manifest_blob.name.split("/")
    # v1/GA/_unknown-county/<slug>/manifest.json
    if len(name_parts) < 5:
        return {"status": "skip_bad_path", "name": manifest_blob.name}
    hoa_slug = name_parts[3]
    old_prefix = "/".join(name_parts[:4])

    try:
        manifest = json.loads(manifest_blob.download_as_bytes())
    except Exception as exc:
        return {"status": "skip_bad_manifest", "name": manifest_blob.name, "error": str(exc)}

    name = manifest.get("name") or hoa_slug
    docs = manifest.get("documents") or []
    metadata_sources = manifest.get("metadata_sources") or []
    source_url = next(
        (s.get("source_url") for s in metadata_sources if s.get("source_url")),
        None,
    )

    # Strategy: PDF "X County" first, then city-in-name/URL, then city-in-PDF.
    county: str | None = None
    pdf_text = ""
    if docs:
        # Try every PDF until we find a county hit (some PDFs have no text).
        for doc in docs[:3]:
            gcs_path = doc.get("gcs_path", "")
            if not gcs_path.startswith(f"gs://{BUCKET_NAME}/"):
                continue
            doc_blob_name = gcs_path[len(f"gs://{BUCKET_NAME}/") :]
            doc_blob = bucket.blob(doc_blob_name)
            if not doc_blob.exists():
                continue
            pdf_text = extract_pdf_text(doc_blob)
            county = infer_county_from_text(pdf_text)
            if county:
                break

    if not county:
        county = infer_county_from_name_or_url(name, source_url)

    if not county and pdf_text:
        county = infer_county_from_city_in_text(pdf_text)

    if not county:
        return {"status": "no_county", "slug": hoa_slug, "name": name[:60]}

    county_slug = slugify(county)
    new_prefix = f"{STATE_PREFIX}/{county_slug}/{hoa_slug}"

    if new_prefix == old_prefix:
        return {"status": "already_routed", "slug": hoa_slug, "county": county}

    # Skip if the new prefix already has a manifest (real collision — needs
    # manual merge, not blind overwrite).
    new_manifest = bucket.blob(f"{new_prefix}/manifest.json")
    if new_manifest.exists():
        return {
            "status": "collision",
            "slug": hoa_slug,
            "county": county,
            "name": name[:60],
        }

    if dry_run:
        return {"status": "dry_would_move", "slug": hoa_slug, "county": county, "name": name[:60]}

    # Copy all blobs under old_prefix -> new_prefix.
    copied = copy_prefix(client, old_prefix, new_prefix)
    new_gcs_paths: dict[str, str] = {}
    for old_blob in client.list_blobs(bucket, prefix=old_prefix + "/"):
        if old_blob.name.endswith("/original.pdf"):
            old_uri = f"gs://{BUCKET_NAME}/{old_blob.name}"
            new_uri = f"gs://{BUCKET_NAME}/{new_prefix}/{old_blob.name[len(old_prefix) + 1:]}"
            new_gcs_paths[old_uri] = new_uri

    update_manifest_county(client, new_prefix, county, new_gcs_paths)
    deleted = delete_prefix(client, old_prefix)
    return {
        "status": "moved",
        "slug": hoa_slug,
        "county": county,
        "copied": len(copied),
        "deleted": deleted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill GA county routing")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="0 = no limit")
    args = parser.parse_args()

    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "hoaware"

    client = gcs.Client()
    manifests = list_unknown_county_manifests(client)
    if args.limit:
        manifests = manifests[: args.limit]
    print(f"Found {len(manifests)} _unknown-county manifests under v1/GA/", file=sys.stderr)

    summary: dict[str, int] = {}
    for i, blob in enumerate(manifests, 1):
        result = process_manifest(client, blob, dry_run=args.dry_run)
        summary[result["status"]] = summary.get(result["status"], 0) + 1
        print(json.dumps({"i": i, **result}))
    print(json.dumps({"summary": summary}, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
