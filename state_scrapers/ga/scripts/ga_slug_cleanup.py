#!/usr/bin/env python3
"""Rewrite junk GA bank slugs to real HOA names, then re-route county.

The legal-phrase deterministic-clean discovery pass produced manifest paths
with slugs like ``a-georgia-nonprofit-corporation-hereinafter-called-...`` —
fragments of CCR preamble text rather than HOA names. This script walks every
``gs://hoaproxy-bank/v1/GA/**/manifest.json``, detects junk slugs, re-derives
a clean name from (in order):

1. Leading-legalese strip (``and-restated-pebble-creek-farm`` -> ``pebble-creek-farm``)
2. ``manifest.name_aliases`` — non-junk alias if present
3. Source-URL filename (``echelon_ccrs_15790.pdf`` -> ``echelon``)
4. Conservative page-1 PDF regex (``Declaration ... for X Homeowners Association``)

If a clean name can be derived AND the resulting slug differs, the manifest
prefix is server-side-copied to the new path, ``manifest.name`` is rewritten
(with the old name pushed to ``name_aliases``), and ``document.gcs_path`` is
fixed. While we're at it, county routing is re-run on the cleaned name in
case it unlocks a city match that the junk slug had hidden.

Concurrency: each manifest's last-modified time is checked; entries touched
in the last ``--skip-fresh-seconds`` are skipped to avoid stomping on a
parallel discovery writer. Collisions (target slug already has a manifest)
are skipped and logged — those need merge logic, not blind overwrite.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "settings.env", override=False)
load_dotenv(ROOT / ".env", override=False)

from google.cloud import storage as gcs  # noqa: E402

from hoaware.bank import slugify  # noqa: E402
from scripts.ga_county_backfill import (  # noqa: E402
    extract_pdf_text,
    infer_county_from_city_in_text,
    infer_county_from_name_or_url,
    infer_county_from_text,
)

BUCKET_NAME = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")
STATE_PREFIX = "v1/GA"

# Tokens that signal a legalese-extracted slug. Order matters for the
# leading-strip pass — keep these as a frozen set, never trim past them.
STOP_TOKENS = frozenset(
    {
        "a", "an", "the", "and", "or", "of", "for", "to", "in", "with", "by",
        "is", "that", "this", "which", "if", "as", "be", "are", "was", "were",
        "hereinafter", "whereas", "wherein", "whereof", "thereof", "thereto",
        "amount", "restated", "amended", "articles", "article", "declaration",
        "declarations", "bylaws", "covenants", "certain", "first", "called",
        "made", "between", "among", "shall", "having", "hereby", "incorporation",
        "section", "subsection", "paragraph", "exhibit", "page", "pages",
        "all", "any", "each", "every", "no", "not", "such", "said",
        "you", "we", "us", "our", "your", "their", "his", "her", "its",
        "mr", "mrs", "ms", "mrhoa",
        "georgia", "ga", "tn", "nc", "north", "south", "east", "west",
        "nonprofit", "corporation", "company", "association", "associations",
        # PDF-extraction head-truncation artifacts ("[oper]ated", "[loc]ation",
        # "[l]assiter") and other extracted preamble noise.
        "agree", "agreed", "ated", "ation", "assiter", "subject", "date",
        "name", "names", "address", "addresses", "laws", "law",
    }
)

# Slugs shorter than this after stripping are too generic to keep.
MIN_CLEAN_SLUG_LEN = 3
MIN_CLEAN_SLUG_TOKENS = 1

# Slugs that are almost certainly extracted-text artifacts. Add patterns
# carefully — false positives here mean a real HOA gets renamed/skipped.
_LEADING_JUNK_PREFIXES = sorted(
    {
        "a", "an", "the", "and", "or", "of", "for", "to", "in", "with", "by",
        "is", "that", "this", "which", "as",
        "hereinafter", "whereas", "wherein", "witnesseth",
        "amount", "restated", "amended", "articles", "article",
        "declaration", "declarations", "covenants", "certain",
        "between", "having", "hereby", "incorporation",
        "section", "exhibit", "page", "agree", "agreed",
        "ated", "ation", "assiter",  # common pdfminer head-truncation artifacts
        "subject",
    },
    key=len,
    reverse=True,
)

# Heuristics for "this slug looks like a fragment, not a name".
JUNK_SLUG_PATTERNS = [
    re.compile(r"^[a-z]$"),  # single letter
    re.compile(r"^[a-z]{1,3}$"),  # 1-3 letter slugs are too generic
    # Starts with a stop-token followed by another token.
    re.compile(r"^(" + "|".join(_LEADING_JUNK_PREFIXES) + r")-"),
    # Sentence-fragment markers anywhere in the slug.
    re.compile(
        r"-(hereinafter|whereas|wherein|witnesseth|thereto|thereof|"
        r"by-laws|board-of|secretary-of|committee-of|directors|"
        r"as-of-date|nonprofit-corporation|with-georgia|of-georgia|"
        r"this-second|that-parc)-"
    ),
    # Statute-like number runs (e.g. "44-3-220-through-44-3-235").
    re.compile(r"\b\d+-\d+-\d+\b"),
    # Repeated trailing word (e.g. "spartan-estates-spartan-estates",
    # "lassiter-walk-walk") — pdf-extraction stutter.
    re.compile(r"\b([a-z]{4,})\b.*-\1$"),
    # Roman-numeral-only (article-iii etc.)
    re.compile(r"^(article-)?[ivx]{1,5}$"),
]

LONG_SLUG_THRESHOLD = 60  # chars; legalese fragments tend to be long
RESULT_MAX_TOKENS = 5  # real HOA names rarely run longer than this


def is_junk_slug(slug: str) -> tuple[bool, str]:
    """Return (is_junk, reason) for a slug."""
    if not slug:
        return True, "empty"
    if len(slug) > LONG_SLUG_THRESHOLD:
        return True, f"long({len(slug)})"
    for pat in JUNK_SLUG_PATTERNS:
        if pat.search(slug):
            return True, f"pattern:{pat.pattern[:40]}"
    return False, ""


# Common doc-section / governance / boilerplate words that look name-shaped
# but aren't HOA names. These are *result*-rejected, not slug-rejected.
_DOC_SECTION_TOKENS = frozenset(
    {
        "mandatory", "age", "directors", "officers", "members", "membership",
        "meetings", "meeting", "voting", "votes", "vote", "dues", "fines",
        "assessments", "assessment", "liens", "lien", "easements", "easement",
        "definitions", "definition", "amendments", "arbitration", "permanent",
        "lots", "lot", "properties", "property", "subject", "purpose",
        "purposes", "notice", "notices", "rules", "regulations", "owner",
        "owners", "homeowners", "association", "associations", "corporation",
        "incorporation", "covenants", "declaration", "declarations", "bylaws",
        "articles", "amendment", "rights", "powers", "duties", "indemnity",
        "indemnification", "fees", "expenses", "books", "records",
        "consent", "approval", "approvals", "second", "first", "third",
        "preliminary", "general", "special", "annual", "regular",
        "recording", "recorded",
    }
)


# Substrings that, when GLUED into a token, indicate a parser failure
# (e.g. ``covenantsand``, ``articlesof``). Rejecting these avoids accepting
# names like ``dm-covenantsand`` from a botched filename parse.
_NOISE_SUBSTRINGS = (
    "covenant", "bylaws", "articleof", "articlesof", "amendment",
    "declaration", "incorporation", "subdivision",
)


def is_clean_result(slug: str) -> bool:
    """Strict check applied to the OUTPUT of every cleanup strategy.

    A strategy's result must:
      - not match any junk pattern
      - have at most RESULT_MAX_TOKENS tokens
      - have no stop-token in any position
      - not be entirely doc-section/governance words ('mandatory-age',
        'board-of-directors', 'amendments-rules')
      - if it's a single token, be at least 5 chars and not a doc-section word
      - not contain a token with a glued doc-noise substring
        ('covenantsand', 'articlesof')
      - not end with ``-county`` (counties aren't HOAs)
    """
    if not slug:
        return False
    if is_junk_slug(slug)[0]:
        return False
    tokens = slug.split("-")
    if len(tokens) > RESULT_MAX_TOKENS:
        return False
    if any(t in STOP_TOKENS for t in tokens):
        return False
    if all(t in _DOC_SECTION_TOKENS or t.isdigit() for t in tokens):
        return False
    if len(tokens) == 1:
        only = tokens[0]
        if len(only) < 5 or only in _DOC_SECTION_TOKENS:
            return False
    for tok in tokens:
        for noise in _NOISE_SUBSTRINGS:
            if noise in tok and tok != noise:
                return False
    if tokens[-1] == "county":
        return False
    return True


def slug_is_too_junky_to_clean(slug: str) -> bool:
    """Slugs saturated with stop tokens are usually unrecoverable — any tail
    we extract is more likely sentence fragment than HOA name.

    Used as a guard before trying after-marker / dedupe / source-url, so we
    don't produce wrong renames for slugs like
    ``a-is-property-subject-to-a-of-and-mandatory-age``.
    """
    tokens = slug.split("-")
    stop_count = sum(1 for t in tokens if t in STOP_TOKENS)
    return stop_count >= 4


# ---------------------------------------------------------------------------
# Name re-derivation strategies
# ---------------------------------------------------------------------------

def strip_leading_stopwords(slug: str) -> str | None:
    """Drop leading stop-token chunks until a real name remains.

    ``and-restated-pebble-creek-farm`` -> ``pebble-creek-farm``
    ``and-of-lochwolde``               -> ``lochwolde``
    ``a-quiet-place-in-woods``         -> ``quiet-place-in-woods``
    """
    tokens = slug.split("-")
    while tokens and tokens[0] in STOP_TOKENS:
        tokens.pop(0)
    if not tokens:
        return None
    cleaned = "-".join(tokens)
    if len(cleaned) < MIN_CLEAN_SLUG_LEN:
        return None
    if len(tokens) < MIN_CLEAN_SLUG_TOKENS:
        return None
    # If after stripping we still match a junk pattern, give up — the slug
    # was wholly junk (e.g. all stop tokens).
    is_junk, _ = is_junk_slug(cleaned)
    if is_junk:
        return None
    return cleaned


_FILENAME_NOISE_RE = re.compile(
    r"\b(ccrs?|cc&rs?|covenants?|bylaws?|by-laws?|articles?|amendment|amendments?|"
    r"amended|restated|declaration|declarations|rules|regulations?|hoa|homeowners?|"
    r"association|incorporation|condominium|condo|original|final|copy|recorded|signed|"
    r"executed|exhibit|schedule|attachment|appendix|of|the|for|and|to|by)\b",
    re.IGNORECASE,
)

# Single-word slugs that look like a name on their own but are actually
# leftover legalese.
_SINGLE_TOKEN_REJECT = frozenset(
    {"incorporation", "declaration", "covenants", "bylaws", "amendment",
     "restated", "amended", "articles", "association", "homeowners",
     "subdivision", "condominium", "rules", "regulations"}
)


def name_from_source_url(url: str | None) -> str | None:
    """Extract HOA name from a PDF URL's filename.

    ``https://x.com/echelon_ccrs_15790.pdf``                 -> ``echelon``
    ``https://x.com/cloud/12BrookstoneHOABylaws.pdf``        -> ``brookstone``
    ``https://x.com/Walton-Reserve-Declaration-Recorded.pdf`` -> ``walton-reserve``
    """
    if not url:
        return None
    last = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "." in last:
        last = last.rsplit(".", 1)[0]
    # Separators -> spaces so word-boundary noise stripping works.
    last = re.sub(r"[_\-]+", " ", last)
    # Split digit/letter boundaries so "12Brookstone" -> "12 Brookstone".
    last = re.sub(r"(\d)([A-Za-z])", r"\1 \2", last)
    last = re.sub(r"([A-Za-z])(\d)", r"\1 \2", last)
    # CamelCase -> spaced.
    last = re.sub(r"([a-z])([A-Z])", r"\1 \2", last)
    last = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", last)
    last = _FILENAME_NOISE_RE.sub(" ", last)
    # Drop standalone numeric tokens (id strings, dates, recording numbers).
    last = re.sub(r"\b\d+\b", " ", last)
    last = re.sub(r"\s+", " ", last).strip()
    if not last:
        return None
    # Require at least one alphabetic token >= 4 chars — rejects pure
    # abbreviations like "CC Rs" or "AOI".
    tokens = last.lower().split()
    if not any(len(t) >= 4 and t.isalpha() for t in tokens):
        return None
    candidate = slugify(last)
    if not candidate or len(candidate) < MIN_CLEAN_SLUG_LEN:
        return None
    if candidate in _SINGLE_TOKEN_REJECT:
        return None
    is_junk, _ = is_junk_slug(candidate)
    if is_junk:
        return None
    # Defensive: peel any residual stop-word prefix.
    candidate = strip_leading_stopwords(candidate) or candidate
    return candidate


def name_from_aliases(aliases: list[str]) -> str | None:
    """Pick the first non-junk alias from manifest.name_aliases."""
    for alias in aliases or []:
        if not alias:
            continue
        cand = slugify(alias)
        if not cand:
            continue
        is_junk, _ = is_junk_slug(cand)
        if is_junk:
            continue
        if len(cand) >= MIN_CLEAN_SLUG_LEN:
            return cand
    return None


# Conservative regex over PDF page-1 text for "Declaration ... for X HOA".
_PDF_NAME_PATTERNS = [
    re.compile(
        r"\bfor\s+([A-Z][A-Za-z0-9'&\-\s]{2,60}?)\s+(?:Homeowners?|Property Owners?|"
        r"Owners?|Condominium|Townhome|Townhomes?|Master|Community)\s+(?:Association|"
        r"Owners?\s+Association|HOA)\b",
    ),
    re.compile(
        r"\b(?:Declaration|Articles)\s+of\s+(?:Covenants[^.]{0,40}for|Incorporation\s+of)\s+"
        r"([A-Z][A-Za-z0-9'&\-\s]{2,60}?)\s+(?:Homeowners?|Property Owners?|"
        r"Subdivision|Condominium|Owners)",
    ),
]


def name_from_pdf_text(text: str) -> str | None:
    """Conservative regex pull of HOA name from declaration preamble."""
    if not text:
        return None
    candidates: list[str] = []
    for pat in _PDF_NAME_PATTERNS:
        for m in pat.finditer(text[:20000]):
            raw = m.group(1).strip()
            # Drop very short matches and obviously-still-legalese matches.
            if len(raw) < 3 or len(raw.split()) > 8:
                continue
            cand = slugify(raw)
            if not cand or len(cand) < MIN_CLEAN_SLUG_LEN:
                continue
            is_junk, _ = is_junk_slug(cand)
            if is_junk:
                continue
            candidates.append(cand)
    if not candidates:
        return None
    # Most-frequent wins; ties go to first match.
    counter: dict[str, int] = {}
    order: list[str] = []
    for c in candidates:
        if c not in counter:
            order.append(c)
        counter[c] = counter.get(c, 0) + 1
    return max(order, key=lambda c: (counter[c], -order.index(c)))


# ---------------------------------------------------------------------------
# Manifest walk + processing
# ---------------------------------------------------------------------------

def list_state_manifests(client: gcs.Client) -> list[gcs.Blob]:
    bucket = client.bucket(BUCKET_NAME)
    out = []
    for blob in client.list_blobs(bucket, prefix=f"{STATE_PREFIX}/"):
        if blob.name.endswith("/manifest.json"):
            out.append(blob)
    return out


def parse_path(name: str) -> tuple[str, str, str] | None:
    """``v1/GA/cobb/foo/manifest.json`` -> (``v1/GA/cobb/foo``, ``cobb``, ``foo``)"""
    parts = name.split("/")
    if len(parts) != 5 or parts[-1] != "manifest.json":
        return None
    if parts[0] != "v1" or parts[1] != "GA":
        return None
    prefix = "/".join(parts[:4])
    return prefix, parts[2], parts[3]


_AFTER_MARKER_RES = [
    re.compile(r"-(?:committee|secretary|board|directors)-of-(.+)$"),
    re.compile(r"-(?:for|of)-([a-z][a-z0-9\-]+)$"),
]


def extract_after_marker(slug: str) -> str | None:
    """Pull the trailing name fragment out of slugs like
    ``architectural-review-committee-of-reserve-at-reid-plantation``
    or ``ated-by-board-of-directors-...-secretary-of-dutch-island``.

    Takes the LAST marker match (so nested ``-of-`` chains land on the
    innermost name) and returns it only if the tail is itself clean.
    """
    if not slug:
        return None
    for pat in _AFTER_MARKER_RES:
        matches = list(pat.finditer(slug))
        if not matches:
            continue
        tail = matches[-1].group(1)
        tokens = tail.split("-")
        while tokens and tokens[-1] in STOP_TOKENS:
            tokens.pop()
        while tokens and tokens[0] in STOP_TOKENS:
            tokens.pop(0)
        candidate = "-".join(tokens)
        if candidate and is_clean_result(candidate):
            return candidate
    return None


def dedupe_tail(slug: str) -> str | None:
    """Find a duplicated trailing chunk and return it if clean.

    ``1-wellington-walk-walk``                    -> ``wellington-walk``
    ``big-canoe-poa-big-canoe``                   -> ``big-canoe``
    ``amount-of-coverage-spartan-estates-spartan-estates`` -> ``spartan-estates``

    The duplicated chunk is preferred over the prefix because the prefix
    usually contains the legalese that produced the bad slug. Only commits
    to a result that passes ``is_clean_result``.
    """
    if not slug:
        return None
    tokens = slug.split("-")
    # Try chunk lengths from 3 down to 1 (longest match wins).
    for chunk_len in range(min(3, len(tokens) // 2), 0, -1):
        tail = tokens[-chunk_len:]
        prev = tokens[-2 * chunk_len : -chunk_len]
        if tail == prev and tail:
            candidate = "-".join(tail)
            if is_clean_result(candidate):
                return candidate
    return None


def strip_then_dedupe(slug: str) -> str | None:
    """Strip leading legalese and then dedupe the tail. Catches cases like
    ``and-of-spartan-estates-spartan-estates`` where neither op alone yields
    a clean result.
    """
    stripped = strip_leading_stopwords(slug)
    if not stripped or stripped == slug:
        stripped = slug
    return dedupe_tail(stripped)


def junk_slug_has_real_tokens(slug: str) -> bool:
    """Does the slug contain any plausible HOA-name tokens?

    Used to gate the source_url strategy: if the slug already has real-name
    tokens, prefer slug-derived strategies. If it doesn't (pure legalese
    like ``a-georgia-nonprofit-corporation-...``), source_url is safe.
    """
    for tok in slug.split("-"):
        if tok in STOP_TOKENS:
            continue
        if tok.isdigit():
            continue
        if len(tok) < 4:
            continue
        if not tok.isalpha():
            continue
        return True
    return False


def derive_clean_slug(
    *,
    junk_slug: str,
    manifest: dict,
    pdf_text: str,
) -> tuple[str | None, str]:
    """Try every strategy in order. Returns (clean_slug or None, strategy_name).

    Order: deterministic-on-slug strategies first (high confidence), then
    document-derived strategies as last resort.
    """
    # 1. Strip leading legalese (only if the result is fully clean).
    cleaned = strip_leading_stopwords(junk_slug)
    if cleaned and cleaned != junk_slug and is_clean_result(cleaned):
        return cleaned, "strip_leading"

    # If the slug is saturated with stop tokens, anything we extract from
    # mid-slug is more likely sentence fragment than HOA name.
    if slug_is_too_junky_to_clean(junk_slug):
        return None, "too_junky"

    # 2. Tail-after-marker ("committee-of-X" -> X, "by-laws-of-X" -> X).
    cleaned = extract_after_marker(junk_slug)
    if cleaned:
        return cleaned, "after_marker"

    # 3. Dedupe a repeated trailing chunk ("...-X-X" -> X).
    cleaned = dedupe_tail(junk_slug)
    if cleaned:
        return cleaned, "dedupe_tail"

    # 4. Combined: strip leading, then dedupe.
    cleaned = strip_then_dedupe(junk_slug)
    if cleaned:
        return cleaned, "strip_then_dedupe"

    # 5. Source-URL filename — only when the junk slug is pure legalese
    #    with no real-word tokens (e.g. "a-georgia-nonprofit-..."). When the
    #    slug has real tokens, source_url often misfires on doc-type filenames
    #    ("covenantamendment.pdf") that don't carry the HOA name.
    if not junk_slug_has_real_tokens(junk_slug):
        sources = manifest.get("metadata_sources") or []
        docs = manifest.get("documents") or []
        urls: list[str] = []
        urls.extend(d.get("source_url") for d in docs if d.get("source_url"))
        urls.extend(s.get("source_url") for s in sources if s.get("source_url"))
        for url in urls:
            cleaned = name_from_source_url(url)
            if cleaned and is_clean_result(cleaned):
                return cleaned, "source_url"

    # 6. PDF page-1 regex (last resort).
    cleaned = name_from_pdf_text(pdf_text)
    if cleaned and is_clean_result(cleaned):
        return cleaned, "pdf_text"

    return None, "none"


def get_pdf_text_for_manifest(
    client: gcs.Client, manifest: dict, max_docs: int = 2
) -> str:
    bucket = client.bucket(BUCKET_NAME)
    docs = manifest.get("documents") or []
    parts: list[str] = []
    for doc in docs[:max_docs]:
        gcs_path = doc.get("gcs_path", "")
        if not gcs_path.startswith(f"gs://{BUCKET_NAME}/"):
            continue
        blob_name = gcs_path[len(f"gs://{BUCKET_NAME}/") :]
        blob = bucket.blob(blob_name)
        if not blob.exists():
            continue
        text = extract_pdf_text(blob)
        if text:
            parts.append(text)
            if sum(len(p) for p in parts) > 12000:
                break
    return "\n".join(parts)


def copy_prefix(client: gcs.Client, old_prefix: str, new_prefix: str) -> dict[str, str]:
    """Server-side copy every blob under old_prefix to new_prefix.

    Returns map of old_uri -> new_uri for ``original.pdf`` blobs (used to
    fix ``document.gcs_path`` in the new manifest).
    """
    bucket = client.bucket(BUCKET_NAME)
    pdf_uri_map: dict[str, str] = {}
    for blob in client.list_blobs(bucket, prefix=old_prefix + "/"):
        rel = blob.name[len(old_prefix) + 1 :]
        new_name = f"{new_prefix}/{rel}"
        bucket.copy_blob(blob, bucket, new_name=new_name)
        if blob.name.endswith("/original.pdf"):
            pdf_uri_map[f"gs://{BUCKET_NAME}/{blob.name}"] = (
                f"gs://{BUCKET_NAME}/{new_name}"
            )
    return pdf_uri_map


def delete_prefix(client: gcs.Client, prefix: str) -> int:
    bucket = client.bucket(BUCKET_NAME)
    n = 0
    for blob in client.list_blobs(bucket, prefix=prefix + "/"):
        blob.delete()
        n += 1
    return n


def update_new_manifest(
    client: gcs.Client,
    *,
    new_prefix: str,
    new_county: str | None,
    old_slug: str,
    old_name: str | None,
    pdf_uri_map: dict[str, str],
    cleanup_strategy: str,
) -> None:
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(f"{new_prefix}/manifest.json")
    raw = blob.download_as_bytes()
    data = json.loads(raw)

    # If the existing name is the junk slug or matches the old name, replace
    # it with a Title-Cased version of the new slug so the manifest stops
    # reading like legalese.
    new_slug = new_prefix.rsplit("/", 1)[-1]
    titled = " ".join(part.capitalize() for part in new_slug.split("-"))

    aliases = list(data.get("name_aliases") or [])
    if old_name and old_name not in aliases and old_name != titled:
        aliases.append(old_name)
    data["name_aliases"] = sorted(set(aliases))

    # Only rewrite name if current name looks junk (slug-shaped) or is missing.
    cur_name = data.get("name") or ""
    if (
        not cur_name
        or slugify(cur_name) == old_slug
        or is_junk_slug(slugify(cur_name))[0]
    ):
        data["name"] = titled

    if new_county:
        addr = data.setdefault("address", {})
        addr["county"] = new_county

    for doc in data.get("documents", []):
        old_path = doc.get("gcs_path")
        if old_path and old_path in pdf_uri_map:
            doc["gcs_path"] = pdf_uri_map[old_path]

    sources = data.setdefault("metadata_sources", [])
    sources.append(
        {
            "source": "ga-slug-cleanup",
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fields_provided": ["name", "name_aliases"]
            + (["address.county"] if new_county else []),
            "notes": f"renamed from {old_slug!r} via {cleanup_strategy}",
        }
    )

    blob.upload_from_string(
        json.dumps(data, indent=2, sort_keys=True),
        content_type="application/json",
    )


def merge_into_existing(
    client: gcs.Client,
    *,
    old_prefix: str,
    new_prefix: str,
    old_manifest: dict,
    old_slug: str,
    strategy: str,
) -> tuple[int, int]:
    """Merge old_prefix's docs+aliases into new_prefix's existing manifest,
    then delete old_prefix.

    Returns (docs_merged, blobs_deleted_from_old).
    """
    bucket = client.bucket(BUCKET_NAME)
    new_blob = bucket.blob(f"{new_prefix}/manifest.json")
    new_manifest = json.loads(new_blob.download_as_bytes())

    existing_shas = {
        d.get("sha256")
        for d in (new_manifest.get("documents") or [])
        if d.get("sha256")
    }

    docs_merged = 0
    for old_doc in old_manifest.get("documents") or []:
        sha = old_doc.get("sha256")
        doc_id = old_doc.get("doc_id") or (sha[:12] if sha else None)
        if not sha or not doc_id or sha in existing_shas:
            continue
        # Server-side copy doc-{doc_id}/* into the new prefix.
        new_pdf_uri: str | None = None
        for blob in client.list_blobs(bucket, prefix=f"{old_prefix}/doc-{doc_id}/"):
            rel = blob.name[len(old_prefix) + 1 :]
            new_name = f"{new_prefix}/{rel}"
            bucket.copy_blob(blob, bucket, new_name=new_name)
            if blob.name.endswith("/original.pdf"):
                new_pdf_uri = f"gs://{BUCKET_NAME}/{new_name}"
        merged_doc = dict(old_doc)
        if new_pdf_uri:
            merged_doc["gcs_path"] = new_pdf_uri
        new_manifest.setdefault("documents", []).append(merged_doc)
        existing_shas.add(sha)
        docs_merged += 1

    # Aliases: add the old name + any old aliases.
    aliases = set(new_manifest.get("name_aliases") or [])
    for n in (old_manifest.get("name"), *(old_manifest.get("name_aliases") or [])):
        if n and n != new_manifest.get("name"):
            aliases.add(n)
    new_manifest["name_aliases"] = sorted(aliases)

    # Provenance: keep both source histories + cleanup note.
    new_manifest["metadata_sources"] = (
        (new_manifest.get("metadata_sources") or [])
        + (old_manifest.get("metadata_sources") or [])
        + [
            {
                "source": "ga-slug-cleanup-merge",
                "fetched_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "notes": f"merged junk slug {old_slug!r} into clean slug via {strategy}",
            }
        ]
    )

    new_blob.upload_from_string(
        json.dumps(new_manifest, indent=2, sort_keys=True),
        content_type="application/json",
    )
    deleted = delete_prefix(client, old_prefix)
    return docs_merged, deleted


def reroute_county(
    *, manifest: dict, name_for_inference: str, pdf_text: str
) -> str | None:
    """Re-run county inference on the cleaned name (with PDF text fallback)."""
    docs = manifest.get("documents") or []
    sources = manifest.get("metadata_sources") or []
    source_url = next(
        (s.get("source_url") for s in sources if s.get("source_url")),
        None,
    ) or next(
        (d.get("source_url") for d in docs if d.get("source_url")), None
    )

    county = infer_county_from_text(pdf_text)
    if not county:
        county = infer_county_from_name_or_url(name_for_inference, source_url)
    if not county and pdf_text:
        county = infer_county_from_city_in_text(pdf_text)
    return county


def process_manifest(
    client: gcs.Client,
    blob: gcs.Blob,
    *,
    dry_run: bool,
    skip_fresh_seconds: int,
) -> dict:
    parsed = parse_path(blob.name)
    if not parsed:
        return {"status": "skip_bad_path", "name": blob.name}
    old_prefix, old_county_slug, old_slug = parsed

    is_junk, reason = is_junk_slug(old_slug)
    if not is_junk:
        return {"status": "clean", "slug": old_slug}

    # Concurrency guard: don't touch manifests that an active discovery
    # writer just updated.
    if blob.updated:
        age = datetime.now(timezone.utc) - blob.updated.astimezone(timezone.utc)
        if age < timedelta(seconds=skip_fresh_seconds):
            return {
                "status": "skip_fresh",
                "slug": old_slug,
                "age_s": int(age.total_seconds()),
            }

    try:
        manifest = json.loads(blob.download_as_bytes())
    except Exception as exc:
        return {"status": "skip_bad_manifest", "slug": old_slug, "error": str(exc)}

    pdf_text = get_pdf_text_for_manifest(client, manifest)
    new_slug, strategy = derive_clean_slug(
        junk_slug=old_slug, manifest=manifest, pdf_text=pdf_text
    )
    if not new_slug:
        return {"status": "no_clean_name", "slug": old_slug, "reason": reason}
    if new_slug == old_slug:
        return {"status": "noop_same_slug", "slug": old_slug}

    # Re-run county inference on the cleaned name.
    new_county = reroute_county(
        manifest=manifest, name_for_inference=new_slug, pdf_text=pdf_text
    )
    if new_county:
        new_county_slug = slugify(new_county)
    else:
        # Keep the existing county routing if any; otherwise stay unknown.
        new_county_slug = (
            old_county_slug if old_county_slug != "_unknown-county" else "_unknown-county"
        )
        new_county = manifest.get("address", {}).get("county")

    new_prefix = f"{STATE_PREFIX}/{new_county_slug}/{new_slug}"
    if new_prefix == old_prefix:
        return {"status": "noop_same_prefix", "slug": old_slug}

    bucket = client.bucket(BUCKET_NAME)
    target_manifest_blob = bucket.blob(f"{new_prefix}/manifest.json")
    target_exists = target_manifest_blob.exists()

    # Concurrency guard for the target as well — don't merge into a
    # manifest that an active discovery writer just touched.
    if target_exists:
        target_manifest_blob.reload()
        if target_manifest_blob.updated:
            target_age = datetime.now(timezone.utc) - target_manifest_blob.updated.astimezone(
                timezone.utc
            )
            if target_age < timedelta(seconds=skip_fresh_seconds):
                return {
                    "status": "skip_target_fresh",
                    "slug": old_slug,
                    "new_slug": new_slug,
                    "target_age_s": int(target_age.total_seconds()),
                }

    if target_exists:
        if dry_run:
            return {
                "status": "would_merge",
                "old": old_prefix,
                "new": new_prefix,
                "strategy": strategy,
            }
        docs_merged, deleted = merge_into_existing(
            client,
            old_prefix=old_prefix,
            new_prefix=new_prefix,
            old_manifest=manifest,
            old_slug=old_slug,
            strategy=strategy,
        )
        return {
            "status": "merged",
            "old": old_prefix,
            "new": new_prefix,
            "strategy": strategy,
            "docs_merged": docs_merged,
            "deleted": deleted,
        }

    if dry_run:
        return {
            "status": "would_rename",
            "old": old_prefix,
            "new": new_prefix,
            "strategy": strategy,
            "reason": reason,
        }

    pdf_uri_map = copy_prefix(client, old_prefix, new_prefix)
    update_new_manifest(
        client,
        new_prefix=new_prefix,
        new_county=new_county,
        old_slug=old_slug,
        old_name=manifest.get("name"),
        pdf_uri_map=pdf_uri_map,
        cleanup_strategy=strategy,
    )
    deleted = delete_prefix(client, old_prefix)
    return {
        "status": "renamed",
        "old": old_prefix,
        "new": new_prefix,
        "strategy": strategy,
        "deleted": deleted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean up junk GA bank slugs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="0 = no limit")
    parser.add_argument(
        "--only-unknown-county",
        action="store_true",
        help="Restrict to v1/GA/_unknown-county/* (skip already-routed)",
    )
    parser.add_argument(
        "--skip-fresh-seconds",
        type=int,
        default=300,
        help="Skip manifests updated this recently (parallel-write guard)",
    )
    args = parser.parse_args()

    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "hoaware"

    client = gcs.Client()
    manifests = list_state_manifests(client)
    if args.only_unknown_county:
        manifests = [b for b in manifests if "/_unknown-county/" in b.name]
    if args.limit:
        manifests = manifests[: args.limit]

    print(
        f"Scanning {len(manifests)} manifests under {STATE_PREFIX}/"
        + (" (unknown-county only)" if args.only_unknown_county else ""),
        file=sys.stderr,
    )

    summary: dict[str, int] = {}
    strategy_counts: dict[str, int] = {}
    for i, blob in enumerate(manifests, 1):
        result = process_manifest(
            client,
            blob,
            dry_run=args.dry_run,
            skip_fresh_seconds=args.skip_fresh_seconds,
        )
        summary[result["status"]] = summary.get(result["status"], 0) + 1
        if "strategy" in result:
            strategy_counts[result["strategy"]] = (
                strategy_counts.get(result["strategy"], 0) + 1
            )
        print(json.dumps({"i": i, **result}))
    print(
        json.dumps(
            {"summary": summary, "strategies": strategy_counts}, indent=2
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
