#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.parse import urlparse

import requests
from requests.exceptions import SSLError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hoaware.config import load_settings
from scripts.legal.source_quality import classify_source_quality, extraction_allowed


USER_AGENT = "hoaproxy-legal-corpus/0.1 (research fetcher)"
REQUEST_TIMEOUT = 30
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
INSECURE_TLS_RETRY_HOSTS = {
    "www.leg.colorado.gov",
    "www.cga.ct.gov",
    "billstatus.ls.state.ms.us",
}
NY_LAW_URL_RE = re.compile(r"^https?://www\.nysenate\.gov/legislation/laws/([A-Za-z0-9]+)/([A-Za-z0-9.-]+)$")
NC_SECTION_HTML_RE = re.compile(
    r"^https?://www\.ncleg\.gov/EnactedLegislation/Statutes/HTML/BySection/Chapter_([A-Za-z0-9]+)/GS_([A-Za-z0-9-]+)\.html$"
)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _pick_ext(url: str, content_type: str | None) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix in {".html", ".htm", ".pdf", ".txt", ".xml"}:
        return suffix
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return ".pdf"
    if "html" in ct:
        return ".html"
    if "text/plain" in ct:
        return ".txt"
    return ".bin"


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")


def _scope_key(row: dict) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("jurisdiction") or "").upper(),
        str(row.get("community_type") or "").lower(),
        str(row.get("entity_form") or "unknown").lower(),
        str(row.get("governing_law_bucket") or "").lower(),
        str(row.get("source_url") or "").strip(),
    )


def _dedupe_urls(urls: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = str(url or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _row_fallback_urls(row: dict) -> list[str]:
    raw = row.get("fallback_urls")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _derived_fallback_urls(source_url: str) -> list[str]:
    urls: list[str] = []
    parsed = urlparse(source_url)
    host = parsed.netloc.lower()
    if parsed.scheme == "https" and host in INSECURE_TLS_RETRY_HOSTS:
        urls.append(source_url.replace("https://", "http://", 1))
    if host == "lis.njleg.state.nj.us":
        urls.append("https://www.njleg.state.nj.us/statutes")
    if host in {"legis.state.sd.us", "legis.state.sd.us:80"}:
        urls.append("https://sdlegislature.gov/Statutes")
    if host == "sdlegislature.gov" or host == "www.sdlegislature.gov":
        # SPA pages need the /api/ HTML endpoint for static fetch
        path = urlparse(source_url).path
        if path.startswith("/Statutes/") and "/api/" not in source_url:
            section = path.removeprefix("/Statutes/")
            urls.append(f"https://sdlegislature.gov/api/Statutes/{section}.html?all=true")
    if host == "www.legis.state.ak.us":
        urls.append("https://www.akleg.gov/basis/statutes.asp")
    if host in {"alisondb.legislature.state.al.us", "alison.legislature.state.al.us"}:
        urls.append("https://alison.legislature.state.al.us/")
    ny_match = NY_LAW_URL_RE.match(source_url)
    if ny_match:
        law, section = ny_match.group(1), ny_match.group(2)
        urls.extend(
            [
                f"https://legislation.nysenate.gov/api/3/laws/{law}/{section}",
                f"https://legislation.nysenate.gov/laws/{law}/{section}",
                f"https://www.nysenate.gov/legislation/laws/{law}/{section}?view=all",
            ]
        )
    nc_match = NC_SECTION_HTML_RE.match(source_url)
    if nc_match:
        chapter, section = nc_match.group(1), nc_match.group(2)
        urls.extend(
            [
                f"https://www.ncleg.gov/EnactedLegislation/Statutes/PDF/BySection/Chapter_{chapter}/GS_{section}.pdf",
                f"https://www.ncleg.net/EnactedLegislation/Statutes/PDF/BySection/Chapter_{chapter}/GS_{section}.pdf",
            ]
        )
    if "www.ncleg.gov" in source_url:
        urls.append(source_url.replace("www.ncleg.gov", "www.ncleg.net"))
    elif "www.ncleg.net" in source_url:
        urls.append(source_url.replace("www.ncleg.net", "www.ncleg.gov"))
    return _dedupe_urls(urls)


def _candidate_urls(row: dict) -> list[str]:
    source_url = str(row.get("source_url") or "").strip()
    return _dedupe_urls([source_url, *_row_fallback_urls(row), *_derived_fallback_urls(source_url)])


def _fetch_with_fallback(candidate_urls: list[str]) -> tuple[requests.Response, str, list[str]]:
    attempts: list[str] = []
    nysenate_api_key = os.environ.get("NYSENATE_API_KEY", "").strip()
    for url in candidate_urls:
        for attempt in range(1, 3):
            try:
                headers = {"User-Agent": USER_AGENT}
                params = None
                if "legislation.nysenate.gov/api/3/" in url and nysenate_api_key:
                    headers["X-Api-Key"] = nysenate_api_key
                    headers["Authorization"] = nysenate_api_key
                    params = {"key": nysenate_api_key}
                resp = requests.get(
                    url,
                    timeout=REQUEST_TIMEOUT,
                    headers=headers,
                    params=params,
                )
            except SSLError as exc:
                host = urlparse(url).netloc.lower()
                if host not in INSECURE_TLS_RETRY_HOSTS:
                    attempts.append(f"{url} attempt={attempt} ssl_error={exc}")
                    break
                attempts.append(f"{url} attempt={attempt} ssl_error={exc}; retrying verify=false")
                try:
                    resp = requests.get(
                        url,
                        timeout=REQUEST_TIMEOUT,
                        headers={"User-Agent": USER_AGENT},
                        params={"key": nysenate_api_key} if "legislation.nysenate.gov/api/3/" in url and nysenate_api_key else None,
                        verify=False,
                    )
                except Exception as insecure_exc:  # noqa: BLE001
                    attempts.append(f"{url} attempt={attempt} insecure_retry_error={type(insecure_exc).__name__}: {insecure_exc}")
                    break
            except Exception as exc:  # noqa: BLE001
                attempts.append(f"{url} attempt={attempt} error={type(exc).__name__}: {exc}")
                break

            status = int(resp.status_code)
            if status in RETRYABLE_STATUS_CODES and attempt < 2:
                attempts.append(f"{url} attempt={attempt} status={status} retrying")
                continue
            if status >= 400:
                attempts.append(f"{url} attempt={attempt} status={status}")
                break
            content_type = str(resp.headers.get("Content-Type") or "").lower()
            is_html = "html" in content_type or url.endswith((".html", ".htm")) or "/laws/" in url
            if is_html:
                preview = (resp.text or "")[:5000].lower()
                if "<title>open legislation</title>" in preview and 'id="app"' in preview:
                    attempts.append(f"{url} attempt={attempt} unusable_html=open_legislation_shell")
                    break
                # SD Legislature SPA — static fetch returns a JS shell with no content
                if "sdlegislature.gov" in url and "noscript" in preview and "you need to enable javascript" in preview:
                    attempts.append(f"{url} attempt={attempt} unusable_html=sd_legislature_spa_shell")
                    break
            return resp, url, attempts
    raise RuntimeError("; ".join(attempts[-8:]) or "all candidate fetch attempts failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch legal source texts from source_map.json and store immutable snapshots.")
    parser.add_argument("--source-map", type=Path, default=None, help="Path to source_map.json")
    parser.add_argument("--state", type=str, default=None, help="Optional state filter, e.g. NC")
    parser.add_argument("--limit", type=int, default=0, help="Optional max records to fetch")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch sources even when they already exist in sources.jsonl.",
    )
    parser.add_argument(
        "--include-aggregators",
        action="store_true",
        help="Include secondary aggregator sources during fetch (default: off).",
    )
    args = parser.parse_args()

    settings = load_settings()
    source_map_path = args.source_map or settings.legal_source_map_path
    if not source_map_path.exists():
        raise SystemExit(f"Source map not found: {source_map_path}")

    rows = json.loads(source_map_path.read_text())
    state_filter = args.state.strip().upper() if args.state else None
    if state_filter:
        rows = [row for row in rows if str(row.get("jurisdiction", "")).upper() == state_filter]

    candidates = [
        row
        for row in rows
        if row.get("source_url")
        and str(row.get("retrieval_status", "")).lower() in {"seeded", "verified"}
    ]
    if args.limit and args.limit > 0:
        candidates = candidates[: args.limit]

    raw_root = settings.legal_corpus_root / "raw"
    metadata_path = settings.legal_corpus_root / "metadata" / "sources.jsonl"
    errors_path = settings.legal_corpus_root / "metadata" / "fetch_errors.jsonl"
    existing_rows = _load_jsonl(metadata_path)
    existing_ok_rows = [row for row in existing_rows if str(row.get("status", "")).lower() == "ok"]
    metadata_by_scope: dict[tuple[str, str, str, str, str], dict] = {}
    latest_by_url: dict[str, dict] = {}
    metadata_by_url_checksum: dict[tuple[str, str], dict] = {}
    for row in existing_ok_rows:
        source_url = str(row.get("source_url") or "")
        checksum = str(row.get("checksum_sha256") or "")
        scope = _scope_key(row)
        prev_scope = metadata_by_scope.get(scope)
        if prev_scope is None or str(row.get("fetched_at") or "") > str(prev_scope.get("fetched_at") or ""):
            metadata_by_scope[scope] = row
        prev_url = latest_by_url.get(source_url)
        if prev_url is None or str(row.get("fetched_at") or "") > str(prev_url.get("fetched_at") or ""):
            latest_by_url[source_url] = row
        if not source_url or not checksum:
            continue
        key = (source_url, checksum)
        previous = metadata_by_url_checksum.get(key)
        if previous is None or str(row.get("fetched_at") or "") > str(previous.get("fetched_at") or ""):
            metadata_by_url_checksum[key] = row

    fetched = 0
    failed = 0
    skipped_existing = 0
    reused_existing_url = 0
    skipped_unchanged = 0
    skipped_quality = 0

    for row in candidates:
        jurisdiction = str(row["jurisdiction"]).upper()
        community_type = str(row["community_type"]).lower()
        bucket = str(row["governing_law_bucket"]).lower()
        entity_form = str(row.get("entity_form") or "unknown").lower()
        source_url = str(row["source_url"])
        source_quality = str(row.get("source_quality") or "").strip().lower() or classify_source_quality(
            source_type=str(row.get("source_type") or "unknown"),
            source_url=source_url,
        )
        if not extraction_allowed(source_quality=source_quality, include_aggregators=args.include_aggregators):
            skipped_quality += 1
            print(
                f"[skip] {jurisdiction} {community_type} {bucket} source_quality={source_quality} "
                f"(use --include-aggregators to include)"
            )
            continue
        scope = (jurisdiction, community_type, entity_form, bucket, source_url)
        if not args.refresh and scope in metadata_by_scope:
            skipped_existing += 1
            print(f"[skip] {jurisdiction} {community_type} {bucket} already fetched for {source_url}")
            continue
        if not args.refresh and source_url in latest_by_url:
            base = latest_by_url[source_url]
            payload = dict(base)
            payload.update(
                {
                    "jurisdiction": jurisdiction,
                    "community_type": community_type,
                    "entity_form": entity_form,
                    "governing_law_bucket": bucket,
                    "source_type": row.get("source_type"),
                    "citation": row.get("citation"),
                    "publisher": row.get("publisher"),
                    "verification_status": row.get("verification_status", "unverified"),
                    "source_quality": source_quality,
                    "notes": row.get("notes"),
                }
            )
            metadata_by_scope[scope] = payload
            reused_existing_url += 1
            print(
                f"[reuse] {jurisdiction} {community_type} {bucket} reused snapshot "
                f"{base.get('snapshot_path')}"
            )
            continue
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        try:
            resp, fetched_url, attempts = _fetch_with_fallback(_candidate_urls(row))
            ext = _pick_ext(fetched_url, resp.headers.get("Content-Type"))
            source_hash = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:12]
            out_dir = raw_root / jurisdiction / community_type / bucket
            out_dir.mkdir(parents=True, exist_ok=True)
            raw = resp.content
            checksum = _sha256_bytes(raw)
            existing_key = (source_url, checksum)
            existing_row = metadata_by_url_checksum.get(existing_key)
            if existing_row is not None:
                payload = dict(existing_row)
                payload.update(
                    {
                        "jurisdiction": jurisdiction,
                        "community_type": community_type,
                        "entity_form": entity_form,
                        "governing_law_bucket": bucket,
                        "source_type": row.get("source_type"),
                        "citation": row.get("citation"),
                        "publisher": row.get("publisher"),
                        "source_quality": source_quality,
                        "verification_status": row.get("verification_status", "unverified"),
                        "notes": row.get("notes"),
                    }
                )
                metadata_by_scope[scope] = payload
                skipped_unchanged += 1
                print(
                    f"[reuse] {jurisdiction} {community_type} {bucket} unchanged checksum "
                    f"(snapshot={existing_row.get('snapshot_path')})"
                )
                continue

            out_path = out_dir / f"{now}_{source_hash}{ext}"
            out_path.write_bytes(raw)
            payload = {
                "fetched_at": now,
                "jurisdiction": jurisdiction,
                "community_type": community_type,
                "entity_form": entity_form,
                "governing_law_bucket": bucket,
                "source_type": row.get("source_type"),
                "citation": row.get("citation"),
                "source_url": source_url,
                "fetched_url": fetched_url,
                "publisher": row.get("publisher"),
                "snapshot_path": str(out_path),
                "checksum_sha256": checksum,
                "status": "ok",
                "source_quality": source_quality,
                "verification_status": row.get("verification_status", "unverified"),
                "notes": row.get("notes"),
                "fetch_attempt_log": attempts[-8:],
            }
            metadata_by_url_checksum[existing_key] = payload
            latest_by_url[source_url] = payload
            metadata_by_scope[scope] = payload
            fetched += 1
            print(f"[ok] {jurisdiction} {community_type} {bucket} -> {out_path} ({fetched_url})")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            attempted_urls = _candidate_urls(row)
            _append_jsonl(
                errors_path,
                {
                    "fetched_at": now,
                    "jurisdiction": jurisdiction,
                    "community_type": community_type,
                    "governing_law_bucket": bucket,
                    "source_url": source_url,
                    "candidate_urls": attempted_urls,
                    "source_quality": source_quality,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            print(f"[err] {jurisdiction} {community_type} {bucket} -> {exc}")

    rows_buffer = list(metadata_by_scope.values())
    for row in rows_buffer:
        if str(row.get("source_quality") or "").strip():
            continue
        row["source_quality"] = classify_source_quality(
            source_type=str(row.get("source_type") or "unknown"),
            source_url=str(row.get("source_url") or ""),
        )
    rows_out = sorted(
        rows_buffer,
        key=lambda row: (
            str(row.get("jurisdiction") or ""),
            str(row.get("community_type") or ""),
            str(row.get("entity_form") or ""),
            str(row.get("governing_law_bucket") or ""),
            str(row.get("source_url") or ""),
            str(row.get("fetched_at") or ""),
        ),
    )
    _write_jsonl(metadata_path, rows_out)
    print(
        "Fetch complete. "
        f"fetched={fetched} failed={failed} skipped_existing={skipped_existing} "
        f"reused_existing_url={reused_existing_url} skipped_unchanged={skipped_unchanged} "
        f"skipped_quality={skipped_quality}"
    )


if __name__ == "__main__":
    main()
