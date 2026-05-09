#!/usr/bin/env python3
"""Revert the 7 bad name repairs written by the FL OCR smoke test.

Reads state_scrapers/fl/results/fl_ocr_smoke.json, finds entries with
name_repair set, and reverts each manifest's `name` to repair["old"].
Pops the most recent name_repair_audit entry and removes the bad name
from name_aliases.

Idempotent — re-running on already-reverted manifests is a no-op.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "hoaware")

from google.cloud import storage as gcs

SMOKE_RESULTS = ROOT / "state_scrapers" / "fl" / "results" / "fl_ocr_smoke.json"
BUCKET = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")


def main() -> int:
    payload = json.loads(SMOKE_RESULTS.read_text())
    client = gcs.Client()
    bucket = client.bucket(BUCKET)
    reverted = 0
    skipped = 0
    for r in payload["results"]:
        nr = r.get("name_repair")
        if not nr:
            continue
        old = nr["old"]
        new = nr["new"]
        slug = r["hoa"]
        blob_name = f"v1/FL/sumter/{slug}/manifest.json"
        try:
            manifest = json.loads(bucket.blob(blob_name).download_as_bytes())
        except Exception as exc:
            print(f"[err] {slug}: {exc}")
            continue
        # Only revert if current name is the bad new value
        if manifest.get("name") != new:
            print(f"[skip] {slug}: current name={manifest.get('name')!r} (already reverted?)")
            skipped += 1
            continue
        manifest["name"] = old
        if "name_aliases" in manifest and new in manifest["name_aliases"]:
            manifest["name_aliases"].remove(new)
        if old in (manifest.get("name_aliases") or []):
            manifest["name_aliases"] = [a for a in manifest["name_aliases"] if a != old]
        # Pop the most recent audit entry
        audits = manifest.get("name_repair_audit") or []
        if audits and audits[-1].get("new") == new:
            audits.pop()
            manifest["name_repair_audit"] = audits
        if not manifest.get("name_aliases"):
            manifest.pop("name_aliases", None)
        if not manifest.get("name_repair_audit"):
            manifest.pop("name_repair_audit", None)
        bucket.blob(blob_name).upload_from_string(
            json.dumps(manifest, indent=2, sort_keys=True),
            content_type="application/json",
        )
        print(f"[revert] {slug}: {new!r} -> {old!r}")
        reverted += 1
    print(f"\nReverted: {reverted}, Skipped: {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
