#!/usr/bin/env python3
"""FL bank OCR enrichment: audit, smoke-test, and full pass.

Phases:
  audit  — walk all FL manifests; classify PDFs; estimate DocAI cost (no OCR calls).
  smoke  — run pipeline on 10 HOAs from one county; save sidecars; repair names/geo.
  run    — full pass with hard budget cap (only after smoke approval).

Usage:
  source .venv/bin/activate
  set -a; source settings.env 2>/dev/null; set +a
  export GOOGLE_CLOUD_PROJECT=hoaware

  # Phase A
  python state_scrapers/az/scripts/az_bank_ocr_enrich.py audit \\
      --out data/az_ocr_audit.json

  # Phase B (smoke)
  python state_scrapers/az/scripts/az_bank_ocr_enrich.py smoke \\
      --county sumter --limit 10

  # Phase C (full — requires explicit confirmation)
  python state_scrapers/az/scripts/az_bank_ocr_enrich.py run \\
      --max-cost-usd 396

Idempotent: re-runs skip HOAs whose sidecar already exists and whose manifest
already carries a name_repair / geo_repair audit.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "hoaware")

from google.cloud import storage as gcs

from hoaware.bank import DEFAULT_BUCKET as DEFAULT_BANK_BUCKET
from hoaware.config import load_settings
from hoaware.cost_tracker import COST_DOCAI_PER_PAGE
from hoaware.doc_classifier import REJECT_JUNK, REJECT_PII
from hoaware.pdf_utils import MAX_PAGES_FOR_OCR, MAX_PAGES_FOR_OCR_SCANNED

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("az_bank_ocr")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE = "AZ"
BANK_PREFIX = f"v1/{STATE}/"
SIDECAR_NAME = "sidecar.json"
SIDECAR_MAX_BYTES = 10 * 1024 * 1024  # 10 MB

# Document priority order for OCR selection (lower index = higher priority)
PRIORITY_ORDER = [
    "ccr", "declaration", "bylaws", "articles", "restrictions",
    "master_deed", "amendment", "rules", "resolution",
    "minutes", "financial", "insurance",
    "other",
]

# Hard-reject categories (before any OCR)
HARD_REJECT_CATS = REJECT_PII | REJECT_JUNK

# Arizona-specific patterns
AZ_BBOX = (31.3, 37.0, -114.9, -109.0)  # min_lat max_lat min_lon max_lon

# Name extraction patterns (first 3000 chars of page text).
#
# Every pattern MUST capture a name containing a recognized HOA/POA/condo
# suffix or a subdivision marker -- bare ALL-CAPS lines are NOT acceptable
# (the smoke run promoted "RECITALS", "BLUFFS", "Association Use Only" to
# canonical names, which is worse than the original bank-stage slug).
#
# Group 1 captures the candidate; the suffix is part of the match but
# excluded from the captured group via lookahead so the cleaner can
# normalize suffix tokens consistently.
NAME_PATTERNS = [
    # "Declaration of Covenants ... for FOO BAR HOMEOWNERS ASSOCIATION".
    # Tolerates newlines in the preamble ("DECLARATION OF COVENANTS,\n
    # CONDITIONS AND RESTRICTIONS\nFOR\nTHE BLUFFS OF WEBSTER HOA").
    re.compile(
        r"declaration\s+of\s+(?:covenants|restrictions|conditions)"
        r"[\s,A-Za-z]{0,120}?"
        r"\b(?:for|of)\s+"
        r"((?:The\s+)?[A-Z][A-Za-z0-9 &'.,\-]{3,70}?\s+"
        r"(?:homeowners?\s+association|home\s*owners?\s+association|"
        r"property\s+owners?\s+association|community\s+association|"
        r"condominium\s+association))\b",
        re.IGNORECASE,
    ),
    # "Bylaws of FOO BAR HOMEOWNERS ASSOCIATION, INC."
    re.compile(
        r"\bbylaws\s+(?:of|for)\s+"
        r"([A-Z][A-Za-z0-9 &'.,\-]{3,70}?\s+"
        r"(?:homeowners?\s+association|property\s+owners?\s+association|"
        r"community\s+association|condominium\s+association))\b",
        re.IGNORECASE,
    ),
    # "Articles of Incorporation of FOO BAR HOA, INC."
    re.compile(
        r"\barticles\s+of\s+(?:incorporation|organization)\s+of\s+"
        r"([A-Z][A-Za-z0-9 &'.,\-]{3,70}?\s+"
        r"(?:homeowners?\s+association|property\s+owners?\s+association|"
        r"community\s+association|condominium\s+association))\b",
        re.IGNORECASE,
    ),
    # Free-form "FOO BAR SUBDIVISION HOMEOWNERS ASSOCIATION". Requires the
    # candidate to start at a sentence/line boundary so we don't
    # accidentally capture "Shannon Schell President FOO HOA" prose. The
    # window is ≥1 and ≤7 capitalized words before the suffix.
    # Free-form pattern. Case-sensitive on the name tokens — every word
    # before the suffix MUST be Title-Case or ALL-CAPS, with no lowercase
    # filler like "at", "of", "the", "and" mid-name (those would be
    # prose, not a recorded title). Suffix is case-insensitive only via
    # explicit alternation.
    re.compile(
        r"(?:^|\.\s+|\n\s*)"
        r"((?:The\s+)?[A-Z][A-Za-z0-9'.\-]+(?:\s+[A-Z][A-Za-z0-9'.\-&]+){1,5}\s+"
        r"(?:Homeowners?\s+Association|HOMEOWNERS?\s+ASSOCIATION|"
        r"Property\s+Owners?\s+Association|PROPERTY\s+OWNERS?\s+ASSOCIATION|"
        r"Community\s+Association|COMMUNITY\s+ASSOCIATION|"
        r"Condominium\s+Association|CONDOMINIUM\s+ASSOCIATION))\b",
        re.MULTILINE,
    ),
]

# Geo extraction patterns
ADDRESS_RE = re.compile(
    r"\b([A-Z][A-Za-z\s.'-]{2,30}),\s*(Arizona|FL)\s+(\d{5}(?:-\d{4})?)\b",
    re.IGNORECASE,
)
# Capture the immediate token(s) before "County, Arizona". We allow up
# to 2 capitalized words; the cleaner below filters out recorder-boilerplate
# captures like "Records Of Sumter" / "Public Records Of Sumter" since
# Python's `re` doesn't support variable-width lookbehinds.
COUNTY_RE = re.compile(
    r"\b([A-Z][a-zA-Z]{2,20}(?:\s+[A-Z][a-zA-Z]{2,20})?)\s+County,\s+Arizona\b",
)
# Match the longer "Of [Name] County, Arizona" / "Records Of [Name] County, Arizona"
# preamble so we can skip those captures.
COUNTY_BOILERPLATE_RE = re.compile(
    r"\b(?:public\s+)?records\s+of\s+(?:the\s+)?[A-Z][a-zA-Z]{2,20}\s+County,\s+Arizona",
    re.IGNORECASE,
)
LEGAL_DESC_RE = re.compile(
    r"(?:Section|Sec\.?)\s+(\d+)[,\s]+Township\s+(\d+[NS]),?\s*Range\s+(\d+[EW])",
    re.IGNORECASE,
)
ZIP_RE = re.compile(r"\b(8[5-6]\d{3})(?:-\d{4})?\b")  # FL ZIPs: 32000-34999

# State mismatch patterns
OTHER_STATES = {
    "Alabama", "Georgia", "North Carolina", "South Carolina",
    "Tennessee", "Virginia", "Texas", "Mississippi",
}
ARIZONA_RE = re.compile(r"\bArizona\b|\bFL\s+\d{5}\b", re.IGNORECASE)
OTHER_STATE_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in OTHER_STATES) + r")\b"
)

STOP_NAME_WORDS = {
    "declaration", "declarations", "covenants", "covenant", "conditions",
    "restrictions", "restriction", "bylaws", "bylaw", "articles", "article",
    "amendment", "resolution", "final", "plat", "map", "page", "book",
    "county", "deeds", "recorder", "register", "state", "florida",
}

SUFFIX_RE = re.compile(
    r"\b(?:homeowners association|homeowners|home owners association|"
    r"homes association|property owners association|owners association|"
    r"community association|condominium association|association|hoa|inc\.?|homes)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_manifest(bucket, blob_name: str) -> dict[str, Any] | None:
    try:
        return json.loads(bucket.blob(blob_name).download_as_bytes())
    except Exception as exc:
        log.warning("Failed to load manifest %s: %s", blob_name, exc)
        return None


def _save_manifest(bucket, blob_name: str, manifest: dict[str, Any]) -> None:
    bucket.blob(blob_name).upload_from_string(
        json.dumps(manifest, indent=2, sort_keys=True),
        content_type="application/json",
    )


def _sidecar_blob_name(doc: dict[str, Any]) -> str | None:
    gcs_path = doc.get("gcs_path") or ""
    if not gcs_path.startswith("gs://"):
        return None
    # gs://hoaproxy-bank/v1/AZ/{county}/{slug}/doc-{sha}/original.pdf
    # sidecar goes at: v1/AZ/{county}/{slug}/doc-{sha}/sidecar.json
    raw = gcs_path.split("/", 3)[-1]  # strip gs://bucket/
    parent = str(Path(raw).parent)
    return f"{parent}/{SIDECAR_NAME}"


def _doc_priority(doc: dict[str, Any]) -> int:
    cat = str(doc.get("category_hint") or doc.get("suggested_category") or "other").lower()
    try:
        return PRIORITY_ORDER.index(cat)
    except ValueError:
        return len(PRIORITY_ORDER)


def _classify_doc(doc: dict[str, Any], seen_shas: set[str]) -> tuple[str, str]:
    """Return (classification, reason)."""
    sha = str(doc.get("sha256") or "").lower()
    te = doc.get("text_extractable")
    pc = doc.get("page_count") or 0
    cat = str(doc.get("category_hint") or doc.get("suggested_category") or "").lower().strip()

    if sha and sha in seen_shas:
        return "skip_duplicate_sha", f"duplicate_sha:{sha[:12]}"
    if sha:
        seen_shas.add(sha)

    if cat in HARD_REJECT_CATS:
        return "skip_hard_reject", f"hard_reject_category:{cat}"
    if pc > MAX_PAGES_FOR_OCR:
        return "skip_page_cap_200", f"page_cap:{pc}"

    if te is True:
        return "pypdf_ok", "text_extractable"

    # te is False or None — treat as potentially scanned
    if te is False:
        if pc > MAX_PAGES_FOR_OCR_SCANNED:
            return "skip_page_cap_scanned", f"page_cap_scanned:{pc}"
        return "docai_candidate", f"scanned,pages={pc}"
    # te is None: unknown — check page count
    if pc > MAX_PAGES_FOR_OCR_SCANNED:
        return "skip_page_cap_scanned", f"page_cap_scanned:{pc}"
    return "docai_candidate", f"unknown_extractable,pages={pc}"


def _select_docs_for_hoa(docs: list[dict[str, Any]], seen_shas: set[str]) -> list[dict[str, Any]]:
    """Select up to 2 docs per HOA: 1 primary + 1 fallback, in priority order."""
    classified = []
    for doc in docs:
        cls, reason = _classify_doc(doc, seen_shas)
        classified.append((doc, cls, reason))
    # Sort by priority (category rank)
    classified.sort(key=lambda x: _doc_priority(x[0]))
    selected = []
    for doc, cls, reason in classified:
        if cls in ("pypdf_ok", "docai_candidate"):
            selected.append((doc, cls, reason))
            if len(selected) >= 2:
                break
    return selected


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------


def _pypdf_pages(pdf_bytes: bytes) -> list[dict[str, Any]]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for idx, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        pages.append({"number": idx, "text": text})
    return pages


def _docai_pages(pdf_bytes: bytes, settings, page_count: int) -> tuple[list[dict[str, Any]], int]:
    """Return (pages, docai_pages_billed)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = Path(tmp.name)
    try:
        from hoaware.docai import extract_with_document_ai
        results = extract_with_document_ai(
            tmp_path,
            project_id=settings.docai_project_id,
            location=settings.docai_location,
            processor_id=settings.docai_processor_id,
            endpoint=settings.docai_endpoint,
            max_pages_per_call=settings.docai_chunk_pages,
        )
        pages = [{"number": p.number, "text": p.text} for p in results]
        return pages, page_count
    finally:
        tmp_path.unlink(missing_ok=True)


def _truncate_sidecar_at_page_boundary(sidecar: dict[str, Any]) -> dict[str, Any]:
    """Trim pages from the end until the JSON fits within 10 MB."""
    pages = sidecar.get("pages", [])
    while pages:
        candidate = {**sidecar, "pages": pages}
        if len(json.dumps(candidate).encode()) <= SIDECAR_MAX_BYTES:
            return candidate
        pages = pages[:-1]
    return {**sidecar, "pages": []}


def _sidecar_exists(bucket, sidecar_blob: str) -> bool:
    try:
        return bucket.blob(sidecar_blob).exists()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Name repair
# ---------------------------------------------------------------------------


def _clean_candidate(value: str) -> str | None:
    value = re.sub(r"\s+", " ", value).strip(" .,:;-'\"")
    # Strip declaration-header preludes that pattern 4 occasionally
    # captures: "CONDITIONS AND RESTRICTIONS FOR THE Foo HOA",
    # "ARTICLES OF INCORPORATION OF Foo HOA", etc. Apply each prefix
    # strip in a loop so multiple preludes peel off cleanly.
    _prefix_patterns = [
        re.compile(r"^conditions(?:\s+and\s+(?:restrictions|covenants))?\s+(?:for|of)\s+", re.I),
        re.compile(r"^declarations?(?:\s+of\s+(?:covenants|conditions|restrictions))?\s+(?:for|of)\s+", re.I),
        re.compile(r"^amendment(?:s)?\s+to\s+(?:the\s+)?declarations?\s+(?:for|of)\s+", re.I),
        re.compile(r"^bylaws\s+(?:for|of)\s+", re.I),
        re.compile(r"^articles?\s+of\s+(?:incorporation|organization)\s+(?:for|of)\s+", re.I),
        re.compile(r"^restrictive\s+covenants?\s+(?:for|of)\s+", re.I),
        re.compile(r"^(?:for|of|affecting|the)\s+", re.I),
    ]
    for _ in range(5):  # cap iterations; preludes are short
        before = value
        for pat in _prefix_patterns:
            value = pat.sub("", value)
        if value == before:
            break
    if len(value) < 8 or len(value) > 80:
        return None
    words_raw = value.split()
    words = [w.casefold().strip(".,") for w in words_raw]
    if len(words) < 3:
        # Need at least 3 words: a substantive name (>=1 word) + the
        # HOA suffix (>=2 words like "Homeowners Association").
        return None
    if all(w in STOP_NAME_WORDS for w in words):
        return None
    # Reject prose-style captures: the FIRST token must look like a proper
    # noun (Title-cased or ALL-CAPS), not a lowercase preposition or verb
    # like "documents", "your", "officers".
    first = words_raw[0]
    if not (first[0].isupper() or first.isupper()):
        return None
    # Reject obvious prose markers that prove this isn't a recorded title.
    if re.search(
        r"\b(documents?\s+were|prepared\s+by|officers?\s+of|your|our|these|"
        r"this\s+(?:document|declaration)|attached\s+hereto|set\s+forth|"
        r"hereby|herein|whereof)\b",
        value,
        re.I,
    ):
        return None
    # Must contain a real HOA/POA/condo suffix to be a valid candidate.
    if not re.search(
        r"\b(homeowners?\s+association|property\s+owners?\s+association|"
        r"community\s+association|condominium\s+association)\b",
        value,
        re.I,
    ):
        return None
    if re.search(
        r"\b(page|book|instrument|notary|public|secretary|recorded|recorder|"
        r"recitals?|witnesseth|whereas|now\s+therefore|p\.?\s*o\.?\s*box|"
        r"street\s+lighting|exhibit|attachment|schedule|"
        r"public\s+records?\s+of)\b",
        value,
        re.I,
    ):
        return None
    return value


def _extract_hoa_name(text: str) -> str | None:
    head = text[:3000]
    candidates: list[str] = []
    for pat in NAME_PATTERNS:
        for m in pat.finditer(head):
            c = _clean_candidate(m.group(1))
            if c:
                candidates.append(c)
    if not candidates:
        return None
    # Prefer candidates with more substantive (non-suffix) words. A name
    # like "FOO BAR HOMEOWNERS ASSOCIATION" beats "PROPERTY OWNERS
    # ASSOCIATION, INC". Tie-break by frequency (most-cited wins) then
    # by shortest length.
    from collections import Counter
    counts = Counter(candidates)

    def _score(s: str) -> tuple[int, int, int]:
        core_words = len(SUFFIX_RE.sub("", s).split())
        # Higher core_words = better. Higher count = better. Shorter = better.
        return (-core_words, -counts[s], len(s))

    return min(candidates, key=_score)


# ---------------------------------------------------------------------------
# Geo extraction
# ---------------------------------------------------------------------------


def _extract_geo(text: str) -> dict[str, Any]:
    head = text[:3000]
    result: dict[str, Any] = {}

    m = ADDRESS_RE.search(head)
    if m:
        city = m.group(1).strip()
        # Reject multi-line / overly long city captures.
        if "\n" not in city and len(city) <= 40 and len(city.split()) <= 4:
            result["city"] = city.title()
            result["state"] = "AZ"
            result["postal_code"] = m.group(3)

    # Find first non-boilerplate "X County, Arizona" mention.
    boilerplate_spans = [m.span() for m in COUNTY_BOILERPLATE_RE.finditer(head)]
    for m in COUNTY_RE.finditer(head):
        # Skip if this match falls inside a "records of X County, Arizona" span.
        if any(s <= m.start() and m.end() <= e for s, e in boilerplate_spans):
            continue
        county = m.group(1).strip()
        if (
            len(county) <= 40
            and "\n" not in county
            and not re.search(r"\b(records|public|of)\b", county, re.I)
        ):
            result["county"] = county.title()
            break

    m = LEGAL_DESC_RE.search(head)
    if m:
        result["legal_description"] = f"Section {m.group(1)}, Township {m.group(2)}, Range {m.group(3)}"

    # ZIP fallback if no city found
    if "postal_code" not in result:
        zips = ZIP_RE.findall(head)
        if zips:
            from collections import Counter
            most_common = Counter(zips).most_common(1)[0][0]
            result["postal_code"] = most_common

    return result


# ---------------------------------------------------------------------------
# State mismatch detection
# ---------------------------------------------------------------------------


def _detect_state_mismatch(text: str) -> tuple[str | None, str]:
    head = text[:3000]
    has_az = bool(ARIZONA_RE.search(head))
    other_matches = OTHER_STATE_RE.findall(head)
    if other_matches and not has_az:
        from collections import Counter
        top = Counter(other_matches).most_common(1)[0]
        if top[1] >= 2:
            return top[0], f"name_repeat:{top[1]}"
    return None, ""


# ---------------------------------------------------------------------------
# Phase A — Audit
# ---------------------------------------------------------------------------


def cmd_audit(args: argparse.Namespace) -> int:
    client = gcs.Client()
    bucket = client.bucket(args.bank_bucket)
    seen_shas: set[str] = set()

    stats: dict[str, Any] = {
        "total_manifests": 0,
        "zero_doc_manifests": 0,
        "pdfs_by_class": {},
        "docai_pages_total": 0,
        "pypdf_pages_total": 0,
        "selected_docai_pages": 0,
        "selected_pypdf_docs": 0,
        "hoas_with_any_selection": 0,
        "hoas_with_docai_selection": 0,
        "estimated_cost_usd": 0.0,
        "county_breakdown": {},
    }
    per_hoa: list[dict[str, Any]] = []

    log.info("Scanning gs://%s/%s ...", args.bank_bucket, BANK_PREFIX)
    all_blobs = [b.name for b in bucket.list_blobs(prefix=BANK_PREFIX) if b.name.endswith("/manifest.json")]
    log.info("Found %d manifest blobs", len(all_blobs))

    for i, blob_name in enumerate(all_blobs):
        if i % 500 == 0:
            log.info("Progress: %d/%d manifests processed", i, len(all_blobs))

        manifest = _load_manifest(bucket, blob_name)
        if manifest is None:
            continue

        parts = blob_name.split("/")
        # parts: v1 / FL / {county} / {slug} / manifest.json
        county_slug = parts[2] if len(parts) >= 5 else "_unknown"
        hoa_slug = parts[3] if len(parts) >= 5 else "_unknown"

        stats["total_manifests"] += 1
        docs = manifest.get("documents") or []
        if not docs:
            stats["zero_doc_manifests"] += 1
            continue

        selected = _select_docs_for_hoa(docs, seen_shas)
        hoa_entry: dict[str, Any] = {
            "manifest": blob_name,
            "hoa_name": manifest.get("name", hoa_slug),
            "county": county_slug,
            "docs_total": len(docs),
            "selected": [],
        }

        hoa_docai_pages = 0
        hoa_pypdf = 0

        for doc, cls, reason in selected:
            pc = doc.get("page_count") or 0
            entry = {
                "sha256": str(doc.get("sha256") or "")[:16],
                "class": cls,
                "reason": reason,
                "page_count": pc,
                "category_hint": doc.get("category_hint"),
            }
            hoa_entry["selected"].append(entry)
            stats["pdfs_by_class"][cls] = stats["pdfs_by_class"].get(cls, 0) + 1
            if cls == "docai_candidate":
                hoa_docai_pages += pc
                stats["selected_docai_pages"] += pc
            elif cls == "pypdf_ok":
                hoa_pypdf += 1
                stats["selected_pypdf_docs"] += 1

        # Also count all non-selected skipped docs
        all_classified = [(doc, *_classify_doc(doc, set())) for doc in docs]
        for doc, cls, _ in all_classified:
            if cls.startswith("skip_"):
                stats["pdfs_by_class"][cls] = stats["pdfs_by_class"].get(cls, 0) + 1

        if selected:
            stats["hoas_with_any_selection"] += 1
        if hoa_docai_pages > 0:
            stats["hoas_with_docai_selection"] += 1
        county_key = county_slug
        if county_key not in stats["county_breakdown"]:
            stats["county_breakdown"][county_key] = {"manifests": 0, "docai_pages": 0}
        stats["county_breakdown"][county_key]["manifests"] += 1
        stats["county_breakdown"][county_key]["docai_pages"] += hoa_docai_pages

        hoa_entry["docai_pages"] = hoa_docai_pages
        hoa_entry["pypdf_docs"] = hoa_pypdf
        per_hoa.append(hoa_entry)

    stats["docai_pages_total"] = stats["selected_docai_pages"]
    stats["estimated_cost_usd"] = round(stats["selected_docai_pages"] * COST_DOCAI_PER_PAGE, 2)

    output = {
        "generated_at": _now_iso(),
        "summary": stats,
        "per_hoa": per_hoa,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    log.info("Audit written to %s", out_path)

    cost = stats["estimated_cost_usd"]
    print(f"\n=== Phase A Audit Summary ===")
    print(f"Total manifests scanned:        {stats['total_manifests']:,}")
    print(f"Zero-doc manifests:             {stats['zero_doc_manifests']:,}")
    print(f"HOAs with any selection:        {stats['hoas_with_any_selection']:,}")
    print(f"HOAs with DocAI selection:      {stats['hoas_with_docai_selection']:,}")
    print(f"Selected DocAI pages:           {stats['selected_docai_pages']:,}")
    print(f"Selected PyPDF docs (free):     {stats['selected_pypdf_docs']:,}")
    print(f"Estimated DocAI cost:           ${cost:.2f}")
    print(f"\nPDF classifications:")
    for cls, cnt in sorted(stats["pdfs_by_class"].items()):
        print(f"  {cls}: {cnt}")
    if cost <= 396:
        print(f"\nRECOMMENDATION: Cost ${cost:.2f} is within budget. Proceed with Phase B smoke test.")
    else:
        print(f"\nWARNING: Cost ${cost:.2f} exceeds $396 budget. Consider tighter page cap or 1-doc-per-HOA.")
    return 0


# ---------------------------------------------------------------------------
# Phase B — Smoke test
# ---------------------------------------------------------------------------


def cmd_smoke(args: argparse.Namespace) -> int:
    settings = load_settings()
    client = gcs.Client()
    bucket = client.bucket(args.bank_bucket)

    county_prefix = BANK_PREFIX + f"{args.county}/"
    log.info("Smoke: scanning %s for up to %d HOAs", county_prefix, args.limit)

    # Collect manifests
    blob_names = [
        b.name for b in bucket.list_blobs(prefix=county_prefix)
        if b.name.endswith("/manifest.json")
    ][:args.limit]

    log.info("Smoke: selected %d manifests from county=%s", len(blob_names), args.county)

    seen_shas: set[str] = set()
    results = []
    total_docai_pages = 0
    total_cost = 0.0

    for blob_name in blob_names:
        manifest = _load_manifest(bucket, blob_name)
        if manifest is None:
            continue

        parts = blob_name.split("/")
        county_slug = parts[2]
        hoa_slug = parts[3]
        hoa_name = manifest.get("name", hoa_slug)
        docs = manifest.get("documents") or []

        selected = _select_docs_for_hoa(docs, seen_shas)
        if not selected:
            results.append({"hoa": hoa_slug, "outcome": "no_eligible_docs"})
            continue

        hoa_result: dict[str, Any] = {
            "hoa": hoa_slug,
            "hoa_name": hoa_name,
            "docs_processed": [],
            "name_repair": None,
            "geo_extraction": None,
            "state_mismatch": None,
        }

        all_page_text = []

        for doc, cls, reason in selected:
            sha = str(doc.get("sha256") or "")
            pc = doc.get("page_count") or 0
            doc_result: dict[str, Any] = {"sha": sha[:16], "class": cls, "reason": reason, "pages": pc}

            sidecar_blob = _sidecar_blob_name(doc)
            if sidecar_blob and _sidecar_exists(bucket, sidecar_blob):
                # Load existing sidecar (idempotent)
                try:
                    sidecar = json.loads(bucket.blob(sidecar_blob).download_as_bytes())
                    doc_result["outcome"] = "sidecar_exists"
                    doc_result["docai_pages"] = sidecar.get("docai_pages", 0)
                    all_page_text.extend(p.get("text", "") for p in sidecar.get("pages", []))
                    log.info("Sidecar already exists: %s", sidecar_blob)
                except Exception as exc:
                    doc_result["outcome"] = f"sidecar_load_error:{exc}"
                hoa_result["docs_processed"].append(doc_result)
                continue

            # Download PDF
            gcs_path = doc.get("gcs_path") or ""
            if not gcs_path.startswith("gs://"):
                doc_result["outcome"] = "no_gcs_path"
                hoa_result["docs_processed"].append(doc_result)
                continue

            raw_blob = gcs_path.split("/", 3)[-1]
            try:
                pdf_bytes = bucket.blob(raw_blob).download_as_bytes()
            except Exception as exc:
                doc_result["outcome"] = f"download_error:{exc}"
                hoa_result["docs_processed"].append(doc_result)
                continue

            # Extract text
            if cls == "pypdf_ok":
                pages = _pypdf_pages(pdf_bytes)
                docai_pages_billed = 0
                doc_result["outcome"] = "pypdf"
            else:  # docai_candidate
                if args.dry_run:
                    doc_result["outcome"] = "dry_run_skip"
                    hoa_result["docs_processed"].append(doc_result)
                    continue
                try:
                    pages, docai_pages_billed = _docai_pages(pdf_bytes, settings, pc)
                    cost = docai_pages_billed * COST_DOCAI_PER_PAGE
                    total_docai_pages += docai_pages_billed
                    total_cost += cost
                    doc_result["outcome"] = "docai"
                    doc_result["docai_pages"] = docai_pages_billed
                    doc_result["cost_usd"] = round(cost, 4)
                except Exception as exc:
                    doc_result["outcome"] = f"docai_error:{exc}"
                    hoa_result["docs_processed"].append(doc_result)
                    continue

            # Build and save sidecar
            sidecar: dict[str, Any] = {
                "pages": pages,
                "docai_pages": docai_pages_billed if cls != "pypdf_ok" else 0,
                "generated_at": _now_iso(),
                "source": cls,
            }
            sidecar = _truncate_sidecar_at_page_boundary(sidecar)

            if sidecar_blob:
                try:
                    bucket.blob(sidecar_blob).upload_from_string(
                        json.dumps(sidecar, indent=2, sort_keys=True),
                        content_type="application/json",
                    )
                    doc_result["sidecar_written"] = sidecar_blob
                    log.info("Sidecar written: %s", sidecar_blob)
                except Exception as exc:
                    doc_result["sidecar_error"] = str(exc)

            all_page_text.extend(p.get("text", "") for p in pages)
            hoa_result["docs_processed"].append(doc_result)

        # Post-OCR enrichment
        full_text = "\n".join(t for t in all_page_text if t)

        if full_text:
            # State mismatch check
            mismatch_state, mismatch_evidence = _detect_state_mismatch(full_text)
            if mismatch_state:
                hoa_result["state_mismatch"] = {
                    "suspect_state": mismatch_state,
                    "evidence": mismatch_evidence,
                }
                log.warning("State mismatch for %s: suspect=%s evidence=%s", hoa_slug, mismatch_state, mismatch_evidence)

            # Name repair
            extracted_name = _extract_hoa_name(full_text)
            if extracted_name and not _names_match(extracted_name, hoa_name):
                repair = {
                    "old": hoa_name,
                    "new": extracted_name,
                    "source": "ocr",
                    "repaired_at": _now_iso(),
                }
                hoa_result["name_repair"] = repair
                if not args.dry_run:
                    _apply_name_repair(bucket, blob_name, manifest, repair)

            # Geo extraction
            geo = _extract_geo(full_text)
            if geo:
                hoa_result["geo_extraction"] = geo
                if not args.dry_run:
                    _apply_geo_extraction(bucket, blob_name, manifest, geo)

        results.append(hoa_result)

    # Report
    print(f"\n=== Phase B Smoke Results ===")
    print(f"HOAs processed: {len(results)}")
    print(f"DocAI pages used: {total_docai_pages}")
    print(f"Smoke cost: ${total_cost:.4f}")
    for r in results:
        print(f"\n  [{r['hoa']}]")
        for dp in r.get("docs_processed", []):
            print(f"    {dp.get('class','?')} → {dp.get('outcome','?')} ({dp.get('pages',0)}p)")
        if r.get("name_repair"):
            nr = r["name_repair"]
            print(f"    NAME REPAIR: '{nr['old']}' → '{nr['new']}'")
        if r.get("geo_extraction"):
            print(f"    GEO: {r['geo_extraction']}")
        if r.get("state_mismatch"):
            print(f"    STATE MISMATCH: {r['state_mismatch']}")
        if r.get("outcome"):
            print(f"    outcome: {r['outcome']}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"results": results, "total_cost_usd": total_cost}, indent=2), encoding="utf-8")
    log.info("Smoke results written to %s", out_path)
    return 0


def _names_match(a: str, b: str) -> bool:
    """True if the two names are effectively the same after normalization."""
    def norm(s: str) -> str:
        s = SUFFIX_RE.sub("", s.lower())
        return re.sub(r"\s+", " ", s).strip()
    return norm(a) == norm(b)


def _apply_name_repair(bucket, blob_name: str, manifest: dict[str, Any], repair: dict[str, Any]) -> None:
    manifest = dict(manifest)
    if "name_repair_audit" not in manifest:
        manifest["name_repair_audit"] = []
    manifest["name_repair_audit"].append(repair)
    old_name = repair["old"]
    new_name = repair["new"]
    if old_name not in manifest.get("name_aliases", []):
        manifest.setdefault("name_aliases", []).append(old_name)
    manifest["name"] = new_name
    _save_manifest(bucket, blob_name, manifest)
    log.info("Name repair saved for %s: '%s' → '%s'", blob_name, old_name, new_name)


def _apply_geo_extraction(bucket, blob_name: str, manifest: dict[str, Any], geo: dict[str, Any]) -> None:
    manifest = dict(manifest)
    address = dict(manifest.get("address") or {})
    changed = False
    for field in ("city", "county", "postal_code", "state"):
        if geo.get(field) and not address.get(field):
            address[field] = geo[field]
            changed = True
    if geo.get("legal_description") and not manifest.get("legal_description"):
        manifest["legal_description"] = geo["legal_description"]
        changed = True
    if changed:
        manifest["address"] = address
        if "geo_repair_audit" not in manifest:
            manifest["geo_repair_audit"] = []
        manifest["geo_repair_audit"].append({
            "extracted": geo,
            "repaired_at": _now_iso(),
            "source": "ocr",
        })
        _save_manifest(bucket, blob_name, manifest)
        log.info("Geo repair saved for %s: %s", blob_name, geo)


# ---------------------------------------------------------------------------
# Phase C — Full run
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings()
    client = gcs.Client()
    bucket = client.bucket(args.bank_bucket)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(getattr(args, "output_dir", None) or (ROOT / "state_scrapers" / "az" / "results"))
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = output_dir / f"az_ocr_run_{timestamp}.jsonl"
    summary_path = output_dir / "az_ocr_run_summary.json"

    log.info("Full AZ OCR pass. Budget: $%.2f. Ledger: %s", args.max_cost_usd, ledger_path)
    log.info("Scanning gs://%s/%s ...", args.bank_bucket, BANK_PREFIX)

    all_blobs = [b.name for b in bucket.list_blobs(prefix=BANK_PREFIX) if b.name.endswith("/manifest.json")]
    log.info("Found %d manifest blobs", len(all_blobs))

    seen_shas: set[str] = set()
    total_docai_pages = 0
    total_cost = 0.0
    manifests_touched = 0
    manifests_seen = 0
    slug_repairs = 0
    slug_repair_examples: list[dict[str, str]] = []
    address_fills = 0
    state_mismatches = 0
    budget_deferred = 0
    skip_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    budget_hit = False

    raw_county = (args.county or "").strip().lower()
    if raw_county in ("", "all"):
        county_filter = None
    else:
        county_filter = raw_county.replace(" ", "-")

    limit = getattr(args, "limit", 0) or 0
    dry_run = getattr(args, "dry_run", False)

    def _bump(d: dict[str, int], k: str) -> None:
        d[k] = d.get(k, 0) + 1

    def _append_ledger(record: dict[str, Any]) -> None:
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    for i, blob_name in enumerate(all_blobs):
        if i % 100 == 0:
            log.info("Progress: %d/%d manifests, cost=$%.4f, touched=%d, deferred=%d",
                     i, len(all_blobs), total_cost, manifests_touched, budget_deferred)

        if county_filter:
            parts = blob_name.split("/")
            if len(parts) >= 3 and parts[2] != county_filter:
                continue

        if limit and manifests_seen >= limit:
            break

        manifest = _load_manifest(bucket, blob_name)
        if manifest is None:
            _bump(error_counts, "manifest_load_error")
            _append_ledger({"manifest": blob_name, "outcome": "manifest_load_error"})
            continue

        manifests_seen += 1
        parts = blob_name.split("/")
        hoa_slug = parts[3] if len(parts) >= 5 else "_unknown"
        hoa_name = manifest.get("name", hoa_slug)
        docs = manifest.get("documents") or []

        selected = _select_docs_for_hoa(docs, seen_shas)
        if not selected:
            _bump(skip_counts, "no_eligible_docs")
            _append_ledger({"manifest": blob_name, "hoa": hoa_slug, "outcome": "no_eligible_docs"})
            continue

        all_page_text = []
        hoa_touched = False

        for doc, cls, reason in selected:
            sha = str(doc.get("sha256") or "")
            pc = doc.get("page_count") or 0

            sidecar_blob = _sidecar_blob_name(doc)
            if sidecar_blob and _sidecar_exists(bucket, sidecar_blob):
                # Load existing sidecar for enrichment (idempotent)
                try:
                    sidecar = json.loads(bucket.blob(sidecar_blob).download_as_bytes())
                    all_page_text.extend(p.get("text", "") for p in sidecar.get("pages", []))
                    _bump(skip_counts, "sidecar_exists")
                    _append_ledger({"manifest": blob_name, "sha": sha[:16], "outcome": "sidecar_exists"})
                except Exception:
                    pass
                continue

            gcs_path = doc.get("gcs_path") or ""
            if not gcs_path.startswith("gs://"):
                _bump(error_counts, "no_gcs_path")
                _append_ledger({"manifest": blob_name, "sha": sha[:16], "outcome": "no_gcs_path"})
                continue

            raw_blob = gcs_path.split("/", 3)[-1]

            # Budget check for DocAI: hard kill switch.
            # Once the cap is reached, ALL remaining DocAI candidates are
            # marked budget_deferred (no further OCR calls). PyPDF/text-only
            # work continues since it's free.
            if cls == "docai_candidate":
                projected = pc * COST_DOCAI_PER_PAGE
                if budget_hit or (total_cost + projected > args.max_cost_usd):
                    if not budget_hit:
                        log.warning(
                            "BUDGET CAP HIT: spent=$%.4f, would-add=$%.4f, cap=$%.2f. "
                            "Switching to budget_deferred mode.",
                            total_cost, projected, args.max_cost_usd,
                        )
                        budget_hit = True
                    budget_deferred += 1
                    _append_ledger({
                        "manifest": blob_name,
                        "sha": sha[:16],
                        "outcome": "budget_deferred",
                        "projected_cost": projected,
                        "budget_remaining": args.max_cost_usd - total_cost,
                    })
                    continue

            if dry_run and cls == "docai_candidate":
                _bump(skip_counts, "dry_run_skip")
                _append_ledger({"manifest": blob_name, "sha": sha[:16], "outcome": "dry_run_skip"})
                continue

            try:
                pdf_bytes = bucket.blob(raw_blob).download_as_bytes()
            except Exception as exc:
                _bump(error_counts, "download_error")
                _append_ledger({"manifest": blob_name, "sha": sha[:16], "outcome": f"download_error:{exc}"})
                continue

            if cls == "pypdf_ok":
                pages = _pypdf_pages(pdf_bytes)
                docai_pages_billed = 0
                outcome = "pypdf"
            else:
                try:
                    pages, docai_pages_billed = _docai_pages(pdf_bytes, settings, pc)
                    cost = docai_pages_billed * COST_DOCAI_PER_PAGE
                    total_docai_pages += docai_pages_billed
                    total_cost += cost
                    outcome = "docai"
                except Exception as exc:
                    _bump(error_counts, "docai_error")
                    _append_ledger({"manifest": blob_name, "sha": sha[:16], "outcome": f"docai_error:{exc}"})
                    continue

            sidecar: dict[str, Any] = {
                "pages": pages,
                "docai_pages": docai_pages_billed if cls != "pypdf_ok" else 0,
                "generated_at": _now_iso(),
                "source": cls,
            }
            sidecar = _truncate_sidecar_at_page_boundary(sidecar)

            if sidecar_blob and not dry_run:
                try:
                    bucket.blob(sidecar_blob).upload_from_string(
                        json.dumps(sidecar, indent=2, sort_keys=True),
                        content_type="application/json",
                    )
                except Exception as exc:
                    log.warning("Sidecar write failed for %s: %s", sidecar_blob, exc)

            all_page_text.extend(p.get("text", "") for p in pages)
            hoa_touched = True
            _append_ledger({
                "manifest": blob_name,
                "sha": sha[:16],
                "outcome": outcome,
                "pages": pc,
                "docai_pages": docai_pages_billed,
                "cost_usd": round(docai_pages_billed * COST_DOCAI_PER_PAGE, 4),
            })

        # Post-OCR enrichment
        full_text = "\n".join(t for t in all_page_text if t)
        if full_text and hoa_touched:
            manifests_touched += 1

            mismatch_state, mismatch_evidence = _detect_state_mismatch(full_text)
            if mismatch_state:
                state_mismatches += 1
                _append_ledger({
                    "manifest": blob_name,
                    "outcome": "state_mismatch",
                    "suspect_state": mismatch_state,
                    "evidence": mismatch_evidence,
                })
                log.warning("State mismatch: %s suspect=%s", blob_name, mismatch_state)

            if dry_run:
                # Don't mutate manifests in dry-run mode.
                continue

            # Reload manifest fresh before patching
            manifest = _load_manifest(bucket, blob_name) or manifest

            extracted_name = _extract_hoa_name(full_text)
            if extracted_name and not _names_match(extracted_name, hoa_name):
                repair = {
                    "old": hoa_name,
                    "new": extracted_name,
                    "source": "ocr",
                    "repaired_at": _now_iso(),
                }
                _apply_name_repair(bucket, blob_name, manifest, repair)
                slug_repairs += 1
                if len(slug_repair_examples) < 5:
                    slug_repair_examples.append({"old": hoa_name, "new": extracted_name})

            geo = _extract_geo(full_text)
            if geo:
                # Track whether the geo write actually changed any field.
                manifest_for_geo = _load_manifest(bucket, blob_name) or manifest
                addr = manifest_for_geo.get("address") or {}
                will_fill = any(
                    geo.get(f) and not addr.get(f)
                    for f in ("city", "county", "postal_code", "state")
                ) or (geo.get("legal_description") and not manifest_for_geo.get("legal_description"))
                _apply_geo_extraction(bucket, blob_name, manifest_for_geo, geo)
                if will_fill:
                    address_fills += 1

    summary = {
        "generated_at": _now_iso(),
        "budget_cap_usd": args.max_cost_usd,
        "budget_hit": budget_hit,
        "county_filter": county_filter,
        "dry_run": bool(dry_run),
        "limit": limit or None,
        "manifests_total_in_bank": len(all_blobs),
        "manifests_seen": manifests_seen,
        "manifests_touched": manifests_touched,
        "docai_pages_used": total_docai_pages,
        "total_cost_usd": round(total_cost, 4),
        "slug_repairs": slug_repairs,
        "slug_repair_examples": slug_repair_examples,
        "address_fills": address_fills,
        "state_mismatches": state_mismatches,
        "budget_deferred": budget_deferred,
        "skip_counts": skip_counts,
        "error_counts": error_counts,
        "ledger": str(ledger_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    print(f"\n=== Phase C Run Summary ===")
    print(f"Budget cap:             ${args.max_cost_usd:.2f}")
    print(f"Budget hit:             {budget_hit}")
    print(f"Manifests seen:         {manifests_seen:,}")
    print(f"DocAI pages used:       {total_docai_pages:,}")
    print(f"Total cost:             ${total_cost:.4f}")
    print(f"Manifests touched:      {manifests_touched:,}")
    print(f"Slug repairs:           {slug_repairs:,}")
    print(f"Address fills:          {address_fills:,}")
    print(f"State mismatches:       {state_mismatches:,}")
    print(f"Budget deferred:        {budget_deferred:,}")
    print(f"Skip counts:            {skip_counts}")
    print(f"Error counts:           {error_counts}")
    print(f"Ledger:                 {ledger_path}")
    print(f"Summary:                {summary_path}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bank-bucket", default=os.environ.get("HOA_BANK_GCS_BUCKET", DEFAULT_BANK_BUCKET))

    sub = parser.add_subparsers(dest="cmd", required=True)

    # audit
    p_audit = sub.add_parser("audit", help="Phase A: estimate cost, no DocAI calls")
    p_audit.add_argument("--out", default="state_scrapers/az/results/az_ocr_audit.json")

    # smoke
    p_smoke = sub.add_parser("smoke", help="Phase B: smoke-test on N HOAs from one county")
    p_smoke.add_argument("--county", default="pima", help="County slug to test")
    p_smoke.add_argument("--limit", type=int, default=10)
    p_smoke.add_argument("--dry-run", action="store_true", help="Skip actual DocAI calls")
    p_smoke.add_argument("--out", default="state_scrapers/az/results/az_ocr_smoke.json")

    # run
    p_run = sub.add_parser("run", help="Phase C: full pass (only after smoke approval)")
    p_run.add_argument("--max-cost-usd", type=float, default=200.0)
    p_run.add_argument("--county", default="", help="Optionally restrict to one county slug; 'all' or empty = all counties")
    p_run.add_argument("--limit", type=int, default=0, help="Stop after N manifests have been seen (0 = no limit)")
    p_run.add_argument("--dry-run", action="store_true", help="Don't call DocAI or mutate manifests/sidecars")
    p_run.add_argument("--output-dir", default=None, help="Where to write the per-PDF ledger and summary JSON")

    args = parser.parse_args()

    if args.cmd == "audit":
        return cmd_audit(args)
    elif args.cmd == "smoke":
        return cmd_smoke(args)
    elif args.cmd == "run":
        return cmd_run(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
