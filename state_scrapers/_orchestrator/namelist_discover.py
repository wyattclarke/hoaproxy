#!/usr/bin/env python3
"""Name-binding HOA discovery for any state with a registry seed.

Reads a seed JSONL of canonical entity names (one per line, with
{name, state, county, address, registry_id, ...}) and runs name-anchored
Serper queries for each. Filters SERP results to governing-doc PDFs only,
downloads them with sanity caps, and banks each PDF directly via
hoaware.bank.bank_hoa() with the canonical name PINNED — so the bank
manifest carries the registry name, not whatever benchmark/scrape_state_
serper_docpages.py's infer_name() would derive from a SERP result.

This fixes the structural bug that made the original DC keyword-Serper run
yield only 5 live HOAs from 165 bank manifests: SERP results for queries
like `"218 VISTA CONDO" "Washington DC"` could match a court filing that
mentioned the condo, and infer_name() would derive a junk title which Phase
10's LLM then correctly identified as not-a-governing-doc and rejected.
With name pinning, the bank manifest stays as "218 Vista Condo" and the
junk PDF (if any) is rejected at the doc level rather than the entity level.

See docs/name-list-first-ingestion-playbook.md for full rationale.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hoaware.bank import DocumentInput, bank_hoa, slugify  # noqa: E402

SERPER_ENDPOINT = "https://google.serper.dev/search"
USER_AGENT = (
    os.environ.get("HOA_DISCOVERY_USER_AGENT")
    or "HOAproxy public-document discovery (+https://hoaproxy.org; contact: hello@hoaproxy.org)"
)

# Governing-doc keyword regex — applied to URL/filename/snippet.
GOVERNING_DOC_RE = re.compile(
    r"(bylaws?|by-laws?|declarations?|cc&?rs?|covenants?|restrictions?|"
    r"rules?\s+and\s+regulations?|articles?\s+of\s+incorporation|amend(?:ment|ed)?|"
    r"master\s+deed|condominium\s+(?:declaration|bylaws|association|act)|"
    r"unit\s+owners?\s+association|cooperative\s+(?:bylaws|declaration)|"
    r"offering\s+plan|governing\s+documents?|hoa\s+documents?|"
    r"declaration\s+of\s+condominium|public\s+offering\s+statement)",
    re.IGNORECASE,
)

# PDF impostor patterns — reject filenames that match
JUNK_FILENAME_RE = re.compile(
    r"\b(listing|brochure|appraisal|prospectus|annual\s+report|press\s+release|"
    r"news|article|blog|review|inspection\s+report|invoice|receipt|coupon|"
    r"estoppel|closing\s+statement|tax\s+return|990|sale\s+sheet|"
    r"floor\s+plan|menu|registration\s+form|application\s+form)\b",
    re.IGNORECASE,
)

# Junk-host blocklist (URL host substring; case-insensitive)
JUNK_HOST_RE = re.compile(
    r"(casetext|courtlistener|justia|scholar\.google|ssrn|jstor|papers\.ssrn|"
    r"law\.cornell|leagle|casemine|govinfo\.gov/content/pkg|congress\.gov|"
    r"pacer|lexis|westlaw|bizjournals|reuters|bloomberg|nytimes|"
    r"washingtonpost|wsj|cnn|marketwatch|foxbusiness|"
    r"scribd|issuu|yumpu|dokumen|pdfcoffee|fliphtml5|"
    r"zillow|redfin|trulia|realtor|homes\.com|55places|apartments|"
    r"facebook|instagram|reddit|nextdoor|hoamanagement|hopb|"
    r"propublica|sec\.gov/edgar|bbb\.org|yelp|indeed|glassdoor|"
    r"academia\.edu|researchgate)",
    re.IGNORECASE,
)

REQUEST_TIMEOUT = 20
PDF_DOWNLOAD_TIMEOUT = 30
MIN_PDF_BYTES = 30_000
MAX_PDF_BYTES = 30_000_000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DiscoveryResult:
    name: str
    queries_run: int = 0
    serper_hits: int = 0
    candidates_after_filter: int = 0
    downloads_ok: int = 0
    downloads_skipped: int = 0
    docs_banked: int = 0
    bank_uri: str | None = None
    error: str | None = None
    decisions: list[dict[str, Any]] = field(default_factory=list)


def is_pdf_url(url: str) -> bool:
    clean = url.lower().split("?", 1)[0].split("#", 1)[0]
    return clean.endswith(".pdf") or "format=pdf" in url.lower()


def serper_search(query: str, *, num: int = 8, api_key: str) -> list[dict]:
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {"q": query, "num": num, "gl": "us", "hl": "en"}
    response = requests.post(SERPER_ENDPOINT, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
    if response.status_code >= 400:
        raise RuntimeError(f"serper {response.status_code}: {response.text[:200]}")
    return list(response.json().get("organic", []))


def state_hint_present(text: str, state: str, state_name: str) -> bool:
    if not text:
        return False
    pattern = re.compile(rf"\b({re.escape(state)}|{re.escape(state_name)})\b", re.IGNORECASE)
    return bool(pattern.search(text))


def looks_like_governing_doc(*, link: str, title: str, snippet: str) -> bool:
    """SERP-time filter: must be PDF, must hit governing-doc keyword somewhere,
    must NOT hit junk filename patterns, must NOT be from a junk host."""
    if not is_pdf_url(link):
        return False
    host = urlparse(link).netloc.lower()
    if JUNK_HOST_RE.search(host):
        return False
    hay = " ".join([link, title or "", snippet or ""])
    if JUNK_FILENAME_RE.search(hay):
        return False
    if not GOVERNING_DOC_RE.search(hay):
        return False
    return True


def guess_category(url: str, snippet: str) -> str | None:
    hay = (url + " " + (snippet or "")).lower()
    if re.search(r"\bby-?laws?\b", hay):
        return "bylaws"
    if re.search(r"\bamend(?:ment|ed)?\b", hay):
        return "amendment"
    if re.search(r"\b(rules?\s+and\s+regulations?|house\s+rules?|regulations?)\b", hay):
        return "rules"
    if re.search(r"\bplat\b|\bsite\s+plan\b", hay):
        return "plat"
    if re.search(r"\barticles?\s+of\s+incorporation\b", hay):
        return "articles"
    if re.search(r"\b(declaration|cc&?rs?|covenants?|restrictions?|master\s+deed|condominium)\b", hay):
        return "ccr"
    return None


def derive_filename(seed_name: str, source_url: str) -> str:
    parsed = urlparse(source_url)
    path_tail = Path(parsed.path).name or "doc.pdf"
    if not path_tail.lower().endswith(".pdf"):
        path_tail = f"{path_tail}.pdf"
    slug = slugify(seed_name)
    # Compose: slug-tail.pdf, capped at 120 chars
    composed = f"{slug}-{path_tail}"[:120]
    if not composed.lower().endswith(".pdf"):
        composed = composed[:116] + ".pdf"
    return composed


class ExistingSlugIndex:
    """Lightweight cache of existing v1/{STATE}/{county}/{slug}/ prefixes for
    skip-existing mode. Built once at startup; not refreshed during the run."""

    def __init__(self, bucket_name: str, state: str):
        self.lock = threading.Lock()
        self.slugs: set[str] = set()
        try:
            from google.cloud import storage
            client = storage.Client()
            for blob in client.bucket(bucket_name).list_blobs(prefix=f"v1/{state}/", max_results=100000):
                if not blob.name.endswith("/manifest.json"):
                    continue
                # path: v1/{state}/{county}/{slug}/manifest.json
                parts = blob.name.split("/")
                if len(parts) >= 5:
                    county_or_holding = parts[2]
                    slug = parts[3]
                    # Skip the _unresolved-name/ holding pen — those entries
                    # weren't successfully banked under a canonical slot, and
                    # we want a re-run with pinned_name=True to retry them.
                    if county_or_holding == "_unresolved-name":
                        continue
                    self.slugs.add(slug)
        except Exception as exc:
            print(f"WARN: existing-slug index failed to build: {exc}", file=sys.stderr)

    def has(self, slug: str) -> bool:
        return slug in self.slugs

    def add(self, slug: str) -> None:
        with self.lock:
            self.slugs.add(slug)


def queries_for_seed(seed: dict, state: str, state_name: str) -> list[str]:
    """Build a varied query set per entity.

    CAMA-style names are short and tax-record-flavored ("Brooks Park Condo");
    the real legal name on governing documents is usually expanded
    ("Brooks Park Condominium Association" or "Brooks Park Condominium Owners
    Association"). So we generate a mix of:
      - quoted full-name (highest precision when the full name appears verbatim)
      - quoted base-name + condo synonym phrasing (catches the expanded forms)
      - unquoted bag-of-words with state hint (broadest recall)

    Returns up to ~7 queries depending on how distinctive the base is.
    """
    name = seed["name"]
    # Strip generic suffixes to find the distinctive base
    base = re.sub(
        r"\b(the\s+)?(condo|condominium|condominiums|condos|coop|co-?op|"
        r"cooperative|cooperatives|hoa|association|apartments?|apts?)\b",
        " ", name, flags=re.IGNORECASE,
    )
    base = re.sub(r"\s+", " ", base).strip(" ,.-")
    qs: list[str] = []

    # Quoted base-name + condo synonyms (catches expanded legal names)
    if len(base) >= 4 and base.lower() != name.lower():
        qs.append(f'"{base}" "{state_name}" "Condominium Association"')
        qs.append(f'"{base}" "{state}" "Condominium" Bylaws filetype:pdf')
        qs.append(f'"{base} Condominium" "{state}" Declaration')
        qs.append(f'"{base}" "{state}" "Unit Owners Association"')

    # Quoted full CAMA name (catches PDFs that actually use the tax-record name)
    qs.append(f'"{name}" "{state_name}" filetype:pdf')
    qs.append(f'"{name}" "{state}" Bylaws')

    # Unquoted broad recall (loosest)
    if len(base) >= 4:
        qs.append(f'{base} "Washington" "{state}" Condominium Declaration filetype:pdf')

    return qs


def download_pdf(url: str, session: requests.Session) -> tuple[bytes | None, str]:
    """Returns (bytes, reason). bytes=None means rejected/failed; reason is a
    short tag for the ledger."""
    try:
        r = session.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"},
            timeout=PDF_DOWNLOAD_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )
    except requests.RequestException as exc:
        return None, f"request_error:{type(exc).__name__}"
    if r.status_code != 200:
        return None, f"http:{r.status_code}"
    ctype = (r.headers.get("Content-Type") or "").lower()
    # Allow application/pdf; allow octet-stream (many misconfigured servers);
    # reject obvious HTML.
    if "html" in ctype or "json" in ctype or "text/" in ctype and "pdf" not in ctype:
        return None, f"bad_ctype:{ctype[:40]}"
    # Stream-read to enforce size cap
    chunks = bytearray()
    for chunk in r.iter_content(64 * 1024):
        chunks.extend(chunk)
        if len(chunks) > MAX_PDF_BYTES:
            return None, f"oversize:>{MAX_PDF_BYTES}"
    if len(chunks) < MIN_PDF_BYTES:
        return None, f"undersize:{len(chunks)}"
    # Sanity: PDFs start with %PDF
    if not bytes(chunks[:8]).lstrip().startswith(b"%PDF"):
        return None, "not_pdf_magic"
    return bytes(chunks), "ok"


def discover_for_entity(
    *,
    seed: dict,
    state: str,
    state_name: str,
    serper_api_key: str,
    bank_bucket: str,
    max_pdfs_per_entity: int,
    max_results_per_query: int,
    apply: bool,
    seen_url_set: set[str],
    seen_url_lock: threading.Lock,
    existing_slugs: ExistingSlugIndex,
    skip_existing: bool,
    session: requests.Session,
    serper_delay: float,
) -> DiscoveryResult:
    name = seed["name"]
    result = DiscoveryResult(name=name)

    canonical_slug = slugify(name)
    if skip_existing and existing_slugs.has(canonical_slug):
        result.error = "skip_existing"
        result.decisions.append({"event": "skip_existing", "slug": canonical_slug})
        return result

    queries = queries_for_seed(seed, state, state_name)

    candidates: list[dict] = []
    for q in queries:
        try:
            hits = serper_search(q, num=max_results_per_query, api_key=serper_api_key)
            result.queries_run += 1
            result.serper_hits += len(hits)
        except Exception as exc:
            result.decisions.append({"event": "serper_error", "query": q, "error": f"{type(exc).__name__}: {exc}"})
            time.sleep(min(5.0, serper_delay * 4))
            continue
        for h in hits:
            link = (h.get("link") or "").strip()
            title = h.get("title") or ""
            snippet = h.get("snippet") or ""
            if not link.startswith(("http://", "https://")):
                continue
            if not state_hint_present(" ".join([title, snippet, link]), state, state_name):
                result.decisions.append({"event": "skip", "reason": "no_state_hint", "url": link})
                continue
            if not looks_like_governing_doc(link=link, title=title, snippet=snippet):
                result.decisions.append({"event": "skip", "reason": "filter", "url": link})
                continue
            with seen_url_lock:
                if link in seen_url_set:
                    result.decisions.append({"event": "skip", "reason": "dedup_url", "url": link})
                    continue
                seen_url_set.add(link)
            candidates.append({"url": link, "title": title, "snippet": snippet, "query": q})
        time.sleep(serper_delay)

    result.candidates_after_filter = len(candidates)

    # Cap downloads per entity to keep cost bounded
    pdfs_to_bank: list[DocumentInput] = []
    seen_sha: set[str] = set()
    for c in candidates[:max_pdfs_per_entity * 3]:  # download up to 3x cap, dedupe by SHA
        if len(pdfs_to_bank) >= max_pdfs_per_entity:
            break
        pdf_bytes, reason = download_pdf(c["url"], session)
        if pdf_bytes is None:
            result.downloads_skipped += 1
            result.decisions.append({"event": "download_skip", "reason": reason, "url": c["url"]})
            continue
        sha = hashlib.sha256(pdf_bytes).hexdigest()
        if sha in seen_sha:
            result.downloads_skipped += 1
            result.decisions.append({"event": "download_skip", "reason": "duplicate_sha", "url": c["url"]})
            continue
        seen_sha.add(sha)
        pdfs_to_bank.append(
            DocumentInput(
                pdf_bytes=pdf_bytes,
                source_url=c["url"],
                filename=derive_filename(name, c["url"]),
                category_hint=guess_category(c["url"], c["snippet"]),
                text_extractable_hint=None,
            )
        )
        result.downloads_ok += 1
        result.decisions.append({"event": "download_ok", "url": c["url"], "sha256": sha, "size": len(pdf_bytes), "query": c["query"]})

    if not pdfs_to_bank:
        result.error = "no_docs_found"
        return result

    if not apply:
        result.error = "dry_run"
        return result

    # Bank with the canonical name pinned. Address from seed (may be partial).
    address = dict(seed.get("address") or {})
    address.setdefault("state", state)
    address.setdefault("county", seed.get("county") or state)
    if seed.get("city"):
        address.setdefault("city", seed["city"])

    metadata_type = (seed.get("metadata_type") or "condo").lower()
    if metadata_type not in {"hoa", "condo", "coop", "timeshare", "unknown"}:
        metadata_type = "unknown"

    metadata_source = {
        "source": seed.get("source") or f"{state.lower()}-namelist-first",
        "source_url": seed.get("source_url") or "",
        "discovery_pattern": "name-list-first",
        "registry_id": seed.get("registry_id") or seed.get("regime_id"),
        "unit_count": seed.get("unit_count"),
        "queried_at": now_iso(),
    }

    try:
        uri = bank_hoa(
            name=name,
            metadata_type=metadata_type,
            address=address,
            geometry={},
            website=None,
            metadata_source=metadata_source,
            documents=pdfs_to_bank,
            state_verified_via=f"{state.lower()}-namelist-first",
            bucket_name=bank_bucket,
            pinned_name=True,  # registry-derived names bypass is_dirty()
        )
        result.bank_uri = uri
        result.docs_banked = len(pdfs_to_bank)
        existing_slugs.add(canonical_slug)
    except Exception as exc:
        result.error = f"bank_error:{type(exc).__name__}: {exc}"
    return result


def main() -> int:
    load_dotenv(ROOT / "settings.env", override=False)
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", required=True, help="Seed JSONL with {name, state, county, ...}")
    parser.add_argument("--state", required=True)
    parser.add_argument("--state-name", required=True)
    parser.add_argument("--bank-bucket", default=os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank"))
    parser.add_argument("--ledger", required=True, help="Per-entity ledger JSONL output")
    parser.add_argument("--max-pdfs-per-entity", type=int, default=4)
    parser.add_argument("--max-results-per-query", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--serper-delay", type=float, default=0.15, help="Per-query Serper delay (per worker)")
    parser.add_argument("--limit", type=int, default=0, help="0 = all; cap entities for testing")
    parser.add_argument("--skip-existing", action="store_true", help="Skip entities whose slug already exists in the bank")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    state = args.state.upper()
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        print("FATAL: SERPER_API_KEY missing", file=sys.stderr)
        return 2

    seed_path = Path(args.seed)
    if not seed_path.exists():
        print(f"FATAL: seed file missing: {seed_path}", file=sys.stderr)
        return 2

    seeds: list[dict] = []
    seen_keys: set[str] = set()
    with seed_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            name = (row.get("name") or "").strip()
            if not name or len(name) < 4:
                continue
            # Drop test/withdrawn entries
            if re.search(r"\b(test|sample|placeholder|withdrawn|cancelled|rescinded|inactive|n/a|tbd)\b", name, re.IGNORECASE):
                continue
            key = name.lower()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            seeds.append(row)
    if args.limit:
        seeds = seeds[: args.limit]

    print(f"Loaded {len(seeds)} entities from {seed_path}", file=sys.stderr)

    # Build existing-slug index up front (one-time GCS list)
    existing_slugs = ExistingSlugIndex(args.bank_bucket, state)
    print(f"Existing-slug index: {len(existing_slugs.slugs)} slugs", file=sys.stderr)

    # Shared dedup
    seen_url_set: set[str] = set()
    seen_url_lock = threading.Lock()

    ledger_path = Path(args.ledger)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_lock = threading.Lock()

    def write_ledger(payload: dict) -> None:
        with ledger_lock, ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")

    # One requests.Session per worker (thread-local)
    session_local = threading.local()

    def get_session() -> requests.Session:
        s = getattr(session_local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": USER_AGENT})
            session_local.session = s
        return s

    summary = {
        "started_at": now_iso(),
        "state": state,
        "state_name": args.state_name,
        "seed_count": len(seeds),
        "workers": args.workers,
        "max_pdfs_per_entity": args.max_pdfs_per_entity,
        "apply": args.apply,
    }
    print(json.dumps(summary, sort_keys=True), file=sys.stderr)

    totals = {
        "entities_processed": 0,
        "entities_banked": 0,
        "entities_skip_existing": 0,
        "entities_no_docs": 0,
        "docs_banked": 0,
        "queries_run": 0,
    }
    totals_lock = threading.Lock()

    def task(seed: dict) -> DiscoveryResult:
        return discover_for_entity(
            seed=seed,
            state=state,
            state_name=args.state_name,
            serper_api_key=api_key,
            bank_bucket=args.bank_bucket,
            max_pdfs_per_entity=args.max_pdfs_per_entity,
            max_results_per_query=args.max_results_per_query,
            apply=args.apply,
            seen_url_set=seen_url_set,
            seen_url_lock=seen_url_lock,
            existing_slugs=existing_slugs,
            skip_existing=args.skip_existing,
            session=get_session(),
            serper_delay=args.serper_delay,
        )

    t0 = time.time()
    last_report_t = t0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(task, s): s for s in seeds}
        done = 0
        for fut in as_completed(futures):
            seed = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:
                res = DiscoveryResult(name=seed.get("name") or "?", error=f"task_exception:{type(exc).__name__}: {exc}")
            done += 1
            with totals_lock:
                totals["entities_processed"] += 1
                totals["queries_run"] += res.queries_run
                if res.error == "skip_existing":
                    totals["entities_skip_existing"] += 1
                elif res.docs_banked > 0:
                    totals["entities_banked"] += 1
                    totals["docs_banked"] += res.docs_banked
                elif res.error in ("no_docs_found", "dry_run"):
                    totals["entities_no_docs"] += 1
            write_ledger({
                "name": res.name,
                "queries_run": res.queries_run,
                "serper_hits": res.serper_hits,
                "candidates_after_filter": res.candidates_after_filter,
                "downloads_ok": res.downloads_ok,
                "downloads_skipped": res.downloads_skipped,
                "docs_banked": res.docs_banked,
                "bank_uri": res.bank_uri,
                "error": res.error,
                "decisions_count": len(res.decisions),
            })
            now = time.time()
            if now - last_report_t >= 30 or done == len(seeds):
                last_report_t = now
                rate = done / max(1.0, (now - t0))
                eta = (len(seeds) - done) / max(0.001, rate)
                with totals_lock:
                    print(
                        f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                        f"{done}/{len(seeds)} ({rate:.2f}/s ETA {eta/60:.1f}m) "
                        f"banked={totals['entities_banked']} no_docs={totals['entities_no_docs']} "
                        f"skip={totals['entities_skip_existing']} docs={totals['docs_banked']}",
                        file=sys.stderr, flush=True,
                    )

    summary["finished_at"] = now_iso()
    summary["totals"] = totals
    summary["wall_seconds"] = round(time.time() - t0, 1)
    print(json.dumps(summary, indent=2, sort_keys=True))
    summary_path = ledger_path.parent / f"{ledger_path.stem}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
