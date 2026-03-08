#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import sys

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hoaware.config import load_settings


SECTION_SPLIT_RE = re.compile(r"(?im)^\s*(section|sec\.|§)\s+[0-9A-Za-z.\-()]+")


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


def _scope_snapshot_key(row: dict) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("jurisdiction") or "").upper(),
        str(row.get("community_type") or "").lower(),
        str(row.get("entity_form") or "unknown").lower(),
        str(row.get("governing_law_bucket") or "").lower(),
        str(row.get("snapshot_path") or ""),
    )


def _strip_html(raw: str) -> str:
    # Lightweight cleanup without external parser dependency.
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# Patterns that indicate legislative website navigation/boilerplate rather than statute text.
_NAV_PATTERNS: list[re.Pattern] = [
    # "Skip to Navigation / Main Content / Site Map" banners
    re.compile(r"Skip to (Navigation|Main Content|Site Map)", re.I),
    # Year-selector menus (long sequences of 4-digit years)
    re.compile(r"\b(20\d{2}\s+){6,}"),
    # Common nav labels from FL Senate, AZ Legislature, NC legislature sites
    re.compile(r"\b(Bills|Calendars|Journals|Appropriations|Conferences|Reports)\b.*?\b(Bills|Calendars|Journals)\b", re.I),
    re.compile(r"Go to Bill:", re.I),
    re.compile(r"Find Statutes:", re.I),
    re.compile(r"Within Chapter:", re.I),
    re.compile(r"Javascript must be enabled", re.I),
    re.compile(r"Senate Tracker:", re.I),
    re.compile(r"FLHouse\.gov", re.I),
    # Breadcrumb navigation chains  (e.g. "Home > Laws > Title XL > Chapter 718")
    re.compile(r"Home\s*[>|»]\s*Laws?\s*[>|»]", re.I),
    # "Order - Legistore" and similar footer e-commerce links
    re.compile(r"Order\s*[-–]\s*Legistore", re.I),
    # "Welcome to LexisNexis" landing pages
    re.compile(r"Welcome to LexisNexis", re.I),
    # Arizona ARS index pages
    re.compile(r"Arizona Revised Statutes\s+Arizona Revised Statutes\s+Title", re.I),
    # Generic nav link clusters: short lines that are all navigation labels
]


def _strip_boilerplate(text: str) -> str:
    """Remove known legislative website navigation boilerplate from plain text.

    Operates on already-HTML-stripped text. Removes lines/spans that match
    known navigation patterns, then collapses excess whitespace.
    """
    # Remove year-selector runs (e.g. "2026 2025 2024 ... 1997")
    text = re.sub(r"(\b20\d{2}\b\s*){5,}", " ", text)
    text = re.sub(r"(\b19\d{2}\b\s*){5,}", " ", text)

    # Remove bracketed nav items and pipe-separated nav menus
    # e.g. "Senators | Senator List | Find Your Legislators | District Maps"
    # Heuristic: a run of 4+ "|"-separated short tokens is nav
    def _remove_pipe_nav(m: re.Match) -> str:
        parts = [p.strip() for p in m.group(0).split("|")]
        if len(parts) >= 4 and all(len(p) < 40 for p in parts):
            return " "
        return m.group(0)
    text = re.sub(r"[^\n]+\|[^\n]+\|[^\n]+\|[^\n]+", _remove_pipe_nav, text)

    # Remove "Skip to ..." lines
    text = re.sub(r"Skip to [A-Za-z ]+", " ", text, flags=re.I)

    # Remove common FL/AZ/NC site chrome phrases
    for phrase in [
        "Go to Bill:", "Find Statutes:", "Within Chapter:", "Senate Tracker:",
        "Sign Up | Login", "FLHouse.gov", "Mobile Site", "Javascript must be enabled",
        "for site search", "Order - Legistore", "Welcome to LexisNexis",
        "Choose Your Path", "Solutions for professionals",
    ]:
        text = text.replace(phrase, " ")

    # Collapse whitespace again
    text = re.sub(r"\s{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _is_navigation_heavy(text: str) -> bool:
    """Return True if the text is primarily navigation/index content, not statute text.

    Heuristics:
    - Very few sentences with substantive words (>6 chars)
    - High density of short "tokens" relative to long words
    - Matches multiple nav indicator patterns
    """
    if not text or len(text) < 50:
        return True

    # Count substantive words (6+ chars, likely legal terms)
    long_words = re.findall(r"[A-Za-z]{6,}", text)
    total_words = re.findall(r"[A-Za-z]{3,}", text)
    if not total_words:
        return True

    # If fewer than 15% of words are substantive, likely nav page
    if len(long_words) / len(total_words) < 0.15:
        return True

    # Check for multiple nav indicators
    nav_hits = sum(1 for p in _NAV_PATTERNS if p.search(text))
    if nav_hits >= 3:
        return True

    # Check total substantive word count
    if len(long_words) < 30:
        return True

    return False


def _pdf_to_text(path: Path) -> str:
    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n\n".join([p for p in pages if p])


def _pdf_to_text_pdftotext(path: Path) -> str:
    completed = subprocess.run(
        ["pdftotext", str(path), "-"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    return (completed.stdout or "").strip()


def _pdf_text_quality(text: str) -> tuple[int, int]:
    alpha_count = sum(1 for ch in text if ch.isalpha())
    word_count = len(re.findall(r"[A-Za-z]{3,}", text))
    return alpha_count, word_count


def _is_low_quality_pdf_text(text: str) -> bool:
    alpha_count, word_count = _pdf_text_quality(text)
    if alpha_count < 120:
        return True
    if word_count < 20:
        return True
    return False


def _extract_pdf_text(path: Path) -> str:
    pypdf_text = _pdf_to_text(path)
    if not _is_low_quality_pdf_text(pypdf_text):
        return pypdf_text

    try:
        pdftotext_text = _pdf_to_text_pdftotext(path)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return pypdf_text

    if not pdftotext_text.strip():
        return pypdf_text
    pypdf_quality = _pdf_text_quality(pypdf_text)
    pdftotext_quality = _pdf_text_quality(pdftotext_text)
    if pdftotext_quality > pypdf_quality:
        return pdftotext_text
    return pypdf_text


def _split_sections(text: str) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    chunks: list[dict] = []
    indices = [match.start() for match in SECTION_SPLIT_RE.finditer(text)]
    if not indices:
        return [{"section_key": "section_1", "heading": "Section 1", "text": text}]
    # Some PDFs contain footer boilerplate like "Section 554D.107 (17, 0)"
    # near the end; splitting on that marker would discard the actual statute body.
    if indices[0] > int(len(text) * 0.5):
        return [{"section_key": "section_1", "heading": "Section 1", "text": text}]
    indices.append(len(text))
    for idx in range(len(indices) - 1):
        start = indices[idx]
        end = indices[idx + 1]
        section_text = text[start:end].strip()
        if not section_text:
            continue
        heading_line = section_text.splitlines()[0][:140] if section_text.splitlines() else f"Section {idx + 1}"
        chunks.append(
            {
                "section_key": f"section_{idx + 1}",
                "heading": heading_line.strip(),
                "text": section_text,
            }
        )
    return chunks


def _normalize_snapshot(path: Path) -> tuple[str, list[dict]]:
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        text = _strip_html(raw)
        text = _strip_boilerplate(text)
        if _is_navigation_heavy(text):
            # Page is mostly navigation chrome; return empty so extract step skips it.
            return text, [{"section_key": "section_1", "heading": "Navigation Page (low quality)", "text": ""}]
    elif suffix == ".pdf":
        text = _extract_pdf_text(path)
    else:
        text = path.read_text(encoding="utf-8", errors="ignore")
    sections = _split_sections(text)
    return text, sections


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize fetched legal artifacts into text+section markdown.")
    parser.add_argument("--sources-jsonl", type=Path, default=None, help="Metadata JSONL from fetch phase")
    parser.add_argument("--state", type=str, default=None, help="Optional state filter")
    parser.add_argument("--limit", type=int, default=0, help="Optional max records")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-normalize already normalized snapshots.",
    )
    args = parser.parse_args()

    settings = load_settings()
    sources_jsonl = args.sources_jsonl or (settings.legal_corpus_root / "metadata" / "sources.jsonl")
    if not sources_jsonl.exists():
        raise SystemExit(f"Sources metadata not found: {sources_jsonl}")

    state_filter = args.state.strip().upper() if args.state else None
    rows = []
    for line in sources_jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("status") != "ok":
            continue
        if state_filter and str(row.get("jurisdiction", "")).upper() != state_filter:
            continue
        rows.append(row)
    deduped_rows: dict[tuple[str, str, str, str, str], dict] = {}
    for row in rows:
        key = _scope_snapshot_key(row)
        if not key[-1]:
            continue
        previous = deduped_rows.get(key)
        if previous is None or str(row.get("fetched_at") or "") > str(previous.get("fetched_at") or ""):
            deduped_rows[key] = row
    rows = list(deduped_rows.values())
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    normalized_root = settings.legal_corpus_root / "normalized"
    metadata_out = settings.legal_corpus_root / "metadata" / "normalized_sources.jsonl"
    errors_out = settings.legal_corpus_root / "metadata" / "normalize_errors.jsonl"
    existing_metadata = _load_jsonl(metadata_out)
    metadata_by_scope_snapshot: dict[tuple[str, str, str, str, str], dict] = {}
    latest_by_snapshot: dict[str, dict] = {}
    for row in existing_metadata:
        snapshot_path = str(row.get("snapshot_path") or "")
        if not snapshot_path:
            continue
        key = _scope_snapshot_key(row)
        previous = metadata_by_scope_snapshot.get(key)
        if previous is None or str(row.get("normalized_at") or "") > str(previous.get("normalized_at") or ""):
            metadata_by_scope_snapshot[key] = row
        latest = latest_by_snapshot.get(snapshot_path)
        if latest is None or str(row.get("normalized_at") or "") > str(latest.get("normalized_at") or ""):
            latest_by_snapshot[snapshot_path] = row

    ok = 0
    failed = 0
    skipped_existing = 0
    reused_existing = 0
    for row in rows:
        snapshot_path = Path(str(row["snapshot_path"]))
        snapshot_key = str(snapshot_path)
        jurisdiction = str(row["jurisdiction"]).upper()
        community_type = str(row["community_type"]).lower()
        entity_form = str(row.get("entity_form") or "unknown").lower()
        bucket = str(row["governing_law_bucket"]).lower()
        scope_key = (jurisdiction, community_type, entity_form, bucket, snapshot_key)
        existing_row = metadata_by_scope_snapshot.get(scope_key)
        if not args.force and existing_row is not None:
            normalized_path = Path(str(existing_row.get("normalized_path") or ""))
            if normalized_path and normalized_path.exists():
                skipped_existing += 1
                print(f"[skip] {snapshot_path} already normalized -> {normalized_path}")
                continue
        if not args.force and snapshot_key in latest_by_snapshot:
            base = latest_by_snapshot[snapshot_key]
            payload = dict(base)
            payload.update(
                {
                    "normalized_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                    "jurisdiction": jurisdiction,
                    "community_type": community_type,
                    "entity_form": entity_form,
                    "governing_law_bucket": bucket,
                    "source_type": row.get("source_type"),
                    "source_quality": row.get("source_quality"),
                    "citation": row.get("citation"),
                    "source_url": row.get("source_url"),
                    "publisher": row.get("publisher"),
                    "verification_status": row.get("verification_status", "unverified"),
                    "last_verified_date": row.get("fetched_at"),
                }
            )
            metadata_by_scope_snapshot[scope_key] = payload
            reused_existing += 1
            print(f"[reuse] {jurisdiction} {community_type} {bucket} reused normalized {base.get('normalized_path')}")
            continue
        source_hash = hashlib.sha1(str(snapshot_path).encode("utf-8")).hexdigest()[:12]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = normalized_root / jurisdiction / community_type / f"{bucket}_{source_hash}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            text, sections = _normalize_snapshot(snapshot_path)
            body = [f"# {row.get('citation') or 'Unknown citation'}", ""]
            body.append(f"- jurisdiction: {jurisdiction}")
            body.append(f"- community_type: {community_type}")
            body.append(f"- bucket: {bucket}")
            body.append(f"- source_url: {row.get('source_url')}")
            body.append("")
            for section in sections:
                body.append(f"## {section['heading']}")
                body.append("")
                body.append(section["text"])
                body.append("")
            out_path.write_text("\n".join(body).strip() + "\n", encoding="utf-8")
            payload = {
                "normalized_at": stamp,
                "jurisdiction": jurisdiction,
                "community_type": community_type,
                "entity_form": entity_form,
                "governing_law_bucket": bucket,
                "source_type": row.get("source_type"),
                "source_quality": row.get("source_quality"),
                "citation": row.get("citation"),
                "source_url": row.get("source_url"),
                "publisher": row.get("publisher"),
                "snapshot_path": str(snapshot_path),
                "normalized_path": str(out_path),
                "raw_text_checksum_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "section_count": len(sections),
                "sections": sections,
                "verification_status": row.get("verification_status", "unverified"),
                "last_verified_date": row.get("fetched_at"),
            }
            metadata_by_scope_snapshot[scope_key] = payload
            latest_by_snapshot[snapshot_key] = payload
            ok += 1
            print(f"[ok] {jurisdiction} {community_type} {bucket} -> {out_path}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            _append_jsonl(
                errors_out,
                {
                    "normalized_at": stamp,
                    "snapshot_path": str(snapshot_path),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            print(f"[err] {snapshot_path} -> {exc}")
    rows_out = sorted(
        metadata_by_scope_snapshot.values(),
        key=lambda row: (
            str(row.get("jurisdiction") or ""),
            str(row.get("community_type") or ""),
            str(row.get("entity_form") or ""),
            str(row.get("governing_law_bucket") or ""),
            str(row.get("snapshot_path") or ""),
            str(row.get("normalized_at") or ""),
        ),
    )
    _write_jsonl(metadata_out, rows_out)
    print(
        "Normalize complete. "
        f"ok={ok} failed={failed} skipped_existing={skipped_existing} reused_existing={reused_existing}"
    )


if __name__ == "__main__":
    main()
