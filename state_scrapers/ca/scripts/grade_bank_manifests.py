#!/usr/bin/env python3
"""Phase 5c — content-grade CA bank manifests with DeepSeek-v4-flash.

Adapts scripts/audit/grade_hoa_text_quality.py to operate on bank
manifests (GCS) instead of live HOAs. Stamps the verdict on the manifest
as `audit.content_grade = {verdict, category, reason, model, graded_at}`.
Junk-graded manifests stay banked but never drain to live (Phase 7's
content-grade gate filters them out).

Why this exists: per docs/california-ingestion-playbook.md §3 Phase 5c,
the 2026-05-09 quality audit found 57.5% junk rate in legacy live CA
HOAs (46/80 sample), with diverse junk patterns (government docs, tax
forms, zoning resolutions, maps, newsletters, court filings). Filename
regex catches some patterns but only LLM content grading reliably catches
all of them.

Usage:
    python state_scrapers/ca/scripts/grade_bank_manifests.py \\
        --state CA \\
        --workers 4 \\
        --rate-limit-rps 3.0

    python state_scrapers/ca/scripts/grade_bank_manifests.py \\
        --state CA --county los-angeles --limit 50 \\
        --dry-run  # preview verdicts without stamping

Cost: ~$0.0002/HOA × ~25k bank entities = ~$5 OpenRouter spend.
Wall time: ~2-3h with workers=4 and rate-limit-rps=3.0.

Text source priority (in order):
1. `text-sidecar.txt` blobs in the manifest's doc-{sha}/ subdir
   (created by Phase 5 OCR)
2. PyPDF text extraction on `original.pdf` (free, fast, only works on
   text-extractable PDFs — covers ~50%)
3. Skip with `verdict=no_text` if neither yields content (Phase 5 OCR
   re-run will populate sidecars later)

This script is a STUB scaffold. Hardens once Phase 5 OCR populates
sidecar text in the bank, since most bank PDFs are scanned (not
text-extractable). For now it can grade the text-extractable subset
as a smoke test.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[3]

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_BUCKET = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")

# Reuse the audited grader prompt verbatim (alignment with the live audit
# is essential — same junk-vs-real definition).
GRADER_SYSTEM = (
    "You audit document text on a public HOA / condo association website. "
    "For each HOA, you receive its name and the extracted text from its document(s). "
    "Decide whether the text constitutes useful governing-document content "
    "for THAT HOA, or junk. "
    "Respond with strict JSON: "
    '{"verdict": "real" | "junk", "category": "<short>", "reason": "<one line>"}'
)

GRADER_INSTRUCTIONS = (
    "REAL governing content includes: declaration, declaration of covenants, "
    "CC&Rs, restrictive covenants, master deed, articles of incorporation "
    "(only when substantive — not a 1-page filing receipt), bylaws, rules and "
    "regulations, amendments, supplemental declarations, board resolutions, "
    "operating agreements, plat with covenants, owner-rights provisions. "
    "JUNK includes: annual-report filing receipts (Form 631, ASN), "
    "non-filing / delinquency notices, certificates of good standing, "
    "filing cover sheets / state filing letters with no body content, "
    "blank or near-blank pages, court rulings, tax forms, financial reports "
    "alone, membership lists, ballots, property listings, news articles, "
    "newsletters, local government ordinances/zoning, paywalled stub pages, "
    "or content clearly about a DIFFERENT HOA / different state / unrelated "
    "organization. "
    "If the text is mostly headers/letterhead and the HOA's name appears only "
    "as the addressee with no governing body content, it is JUNK. "
    "If multiple documents are concatenated and AT LEAST ONE has substantive "
    "governing content for this HOA, return REAL. "
    "Output ONLY the JSON object. No prose."
)


def list_manifest_uris(bucket_name: str, state: str, county: str | None = None):
    """Yield gs://.../manifest.json URIs under the given state (and optionally county)."""
    from google.cloud import storage as gcs

    client = gcs.Client()
    bucket = client.bucket(bucket_name)
    prefix = f"v1/{state}/"
    if county:
        prefix = f"v1/{state}/{county}/"
    for blob in bucket.list_blobs(prefix=prefix):
        if blob.name.endswith("/manifest.json"):
            yield f"gs://{bucket_name}/{blob.name}", blob


def read_manifest(blob) -> dict:
    return json.loads(blob.download_as_text())


def write_manifest(blob, manifest: dict) -> None:
    blob.upload_from_string(json.dumps(manifest, indent=2, sort_keys=True),
                            content_type="application/json")


def fetch_doc_text_from_manifest(blob, manifest: dict, bucket_name: str,
                                  max_chars: int = 4000) -> tuple[str, str]:
    """Return (combined_text, source) where source ∈ {sidecar, pypdf, none}."""
    from google.cloud import storage as gcs

    client = gcs.Client()
    bucket = client.bucket(bucket_name)

    parent_prefix = blob.name.rsplit("/", 1)[0]  # v1/CA/los-angeles/foo
    docs = manifest.get("documents") or []

    # Priority 1: text-sidecar.txt from Phase 5 OCR.
    sidecar_blobs = []
    for doc in docs:
        sha = doc.get("sha") or ""
        if not sha:
            continue
        sidecar_path = f"{parent_prefix}/doc-{sha[:12]}/text-sidecar.txt"
        sb = bucket.blob(sidecar_path)
        if sb.exists():
            sidecar_blobs.append(sb)
    if sidecar_blobs:
        text_blobs = []
        for sb in sidecar_blobs[:4]:
            try:
                txt = sb.download_as_text()
                text_blobs.append(f"--- SIDECAR: {sb.name} ---\n{txt[:max_chars]}")
            except Exception:
                pass
        combined = "\n\n".join(text_blobs).strip()
        if combined:
            return (combined[:9000], "sidecar")

    # Priority 2: PyPDF on original.pdf (text-extractable case only).
    try:
        from pypdf import PdfReader  # type: ignore
        import io
        for doc in docs[:2]:
            sha = doc.get("sha") or ""
            if not sha:
                continue
            pdf_path = f"{parent_prefix}/doc-{sha[:12]}/original.pdf"
            pb = bucket.blob(pdf_path)
            if not pb.exists():
                continue
            data = pb.download_as_bytes()
            try:
                reader = PdfReader(io.BytesIO(data))
                pages_text = []
                for page in reader.pages[:5]:  # first 5 pages only
                    pages_text.append(page.extract_text() or "")
                combined = "\n".join(pages_text).strip()
                if len(combined) >= 200:  # skip near-empty (scanned) PDFs
                    return (combined[:9000], "pypdf")
            except Exception:
                continue
    except ImportError:
        pass

    return ("", "none")


def call_openrouter(prompt: str, *, api_key: str, model: str, timeout: int = 90) -> dict:
    import requests

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": GRADER_SYSTEM + "\n\n" + GRADER_INSTRUCTIONS},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
    }
    last_err = None
    for attempt in range(4):
        try:
            r = requests.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://hoaproxy.org",
                    "X-Title": "hoaproxy bank content grader",
                },
                json=body,
                timeout=timeout,
            )
            if r.status_code == 200:
                content = r.json()["choices"][0]["message"].get("content") or ""
                content = content.strip()
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
                if not content:
                    return {"verdict": "error", "category": "empty_content", "reason": "LLM returned empty"}
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    m = re.search(r"\{[\s\S]*\}", content)
                    if m:
                        try:
                            return json.loads(m.group(0))
                        except Exception:
                            pass
                    return {"verdict": "error", "category": "bad_json", "reason": content[:200]}
            last_err = f"http {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(3 + attempt * 3)
    return {"verdict": "error", "category": "grader_failed", "reason": last_err or "unknown"}


def grade_manifest(uri: str, blob, manifest: dict, *, api_key: str, model: str,
                   bucket_name: str, dry_run: bool) -> dict:
    name = manifest.get("name") or "(unnamed)"
    docs = manifest.get("documents") or []

    out = {
        "manifest_uri": uri,
        "name": name,
        "doc_count": len(docs),
    }

    if not docs:
        out["verdict"] = "no_docs"
        out["category"] = "stub"
        out["reason"] = "manifest has 0 documents"
        return out

    text, text_source = fetch_doc_text_from_manifest(blob, manifest, bucket_name)
    out["text_source"] = text_source

    if text_source == "none" or not text:
        out["verdict"] = "no_text"
        out["category"] = "scanned_or_unreadable"
        out["reason"] = "no sidecar + PyPDF returned <200 chars (scanned PDF — needs Phase 5 OCR)"
        return out

    prompt = f"HOA name: {name}\nState: CA\n\nDocument text:\n{text}"
    verdict = call_openrouter(prompt, api_key=api_key, model=model)

    out.update({
        "verdict": verdict.get("verdict") or "error",
        "category": verdict.get("category"),
        "reason": verdict.get("reason"),
    })

    if not dry_run and out["verdict"] in {"real", "junk"}:
        manifest.setdefault("audit", {})["content_grade"] = {
            "verdict": out["verdict"],
            "category": out.get("category"),
            "reason": out.get("reason"),
            "text_source": text_source,
            "model": model,
            "graded_at": datetime.now(timezone.utc).isoformat(),
        }
        write_manifest(blob, manifest)

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="CA")
    ap.add_argument("--county", default=None, help="Optional county slug filter")
    ap.add_argument("--bucket", default=DEFAULT_BUCKET)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rate-limit-rps", type=float, default=3.0)
    ap.add_argument("--limit", type=int, default=0, help="0 = grade all; otherwise first N manifests")
    ap.add_argument("--out", default=None, help="Write per-manifest verdicts to JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print verdicts but don't stamp manifests")
    ap.add_argument("--skip-graded", action="store_true",
                    help="Skip manifests that already have audit.content_grade")
    args = ap.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        # Try settings.env via dotenv
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / "settings.env")
            api_key = os.environ.get("OPENROUTER_API_KEY")
        except ImportError:
            pass
    if not api_key and not args.dry_run:
        print("ERROR: OPENROUTER_API_KEY not set", file=sys.stderr)
        return 1

    print(f"[grade-bank] state={args.state} county={args.county or '*'} bucket={args.bucket}",
          file=sys.stderr)
    print(f"[grade-bank] model={args.model} workers={args.workers} rps={args.rate_limit_rps}",
          file=sys.stderr)
    print(f"[grade-bank] dry_run={args.dry_run}", file=sys.stderr)

    # Iterate manifests
    results = []
    counter = {"real": 0, "junk": 0, "no_docs": 0, "no_text": 0, "error": 0,
               "skipped_already_graded": 0}
    n_processed = 0
    for uri, blob in list_manifest_uris(args.bucket, args.state, args.county):
        if args.limit and n_processed >= args.limit:
            break
        try:
            manifest = read_manifest(blob)
        except Exception as e:
            print(f"  ERROR reading {uri}: {e}", file=sys.stderr)
            counter["error"] += 1
            continue

        if args.skip_graded and (manifest.get("audit") or {}).get("content_grade"):
            counter["skipped_already_graded"] += 1
            continue

        n_processed += 1
        out = grade_manifest(uri, blob, manifest,
                             api_key=api_key or "",
                             model=args.model,
                             bucket_name=args.bucket,
                             dry_run=args.dry_run)
        v = out.get("verdict") or "error"
        counter[v] = counter.get(v, 0) + 1
        results.append(out)
        if n_processed % 25 == 0:
            print(f"  [{n_processed}] verdict-counts: {counter}", file=sys.stderr)

    print(f"\n=== Summary ===\nprocessed: {n_processed}", file=sys.stderr)
    for k, v in counter.items():
        print(f"  {k}: {v}", file=sys.stderr)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({"summary": counter, "results": results}, indent=2))
        print(f"  -> {args.out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
