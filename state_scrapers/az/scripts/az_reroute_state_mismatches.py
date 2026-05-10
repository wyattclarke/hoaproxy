#!/usr/bin/env python3
"""Re-route 73 AZ bank manifests flagged by OCR as belonging to another state.

Background: the AZ OCR pass (``az_bank_ocr_enrich.py run``) emits
``outcome: "state_mismatch"`` when a manifest's PDF text mentions a non-AZ
state 2+ times without any FL signal. Per the playbook
"Out-Of-State And Out-Of-County Hits Are Free Wins, Not Rejects"
(``docs/multi-state-ingestion-playbook.md`` § Out-of-State Hit Rerouting),
we should re-bank these under the correct state's prefix when the evidence
is unambiguous, not drop them.

For each flagged manifest:
  1. Read the manifest from GCS.
  2. Read all available sidecar texts under ``doc-*/sidecar.json``.
  3. Apply a stricter detection: count "recorder-style" hits per state
     (ZIP address ``City, XX 12345``, ``X County, <State>``,
     ``State of <State>``). Require the suspect state to win by 2+ hits
     AND no recorder-style FL hit at all. Otherwise leave under AZ with
     ``audit.ocr_state_mismatch.review_pending: true``.
  4. If unambiguous: copy the entire ``v1/AZ/<county>/<slug>/`` tree to
     ``v1/<NEW>/_unknown-county/<slug>/`` (county fill is left for that
     state's later passes), patch the new manifest's ``address.state``
     and ``address.country``, append an ``audit.rerouted_from`` entry,
     then delete the AZ tree.

Idempotency: if the FL manifest already has ``audit.rerouted_to``, skip.
If the new prefix already has a manifest, log it and (only if the AZ tree
still exists) delete the AZ tree. The dest manifest is written first;
the AZ tree is only deleted after the dest manifest blob is confirmed.

Audit log: ``state_scrapers/az/results/az_state_reroute_audit.jsonl`` with
one row per flagged manifest.

Usage:
  source .venv/bin/activate
  set -a; source settings.env 2>/dev/null; set +a
  export GOOGLE_CLOUD_PROJECT=hoaware
  python state_scrapers/az/scripts/az_reroute_state_mismatches.py \\
      [--dry-run] [--ledger PATH]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "hoaware")

from google.cloud import storage as gcs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("az_reroute")


BUCKET = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")
VERSION = "v1"
SOURCE_STATE = "AZ"
DEFAULT_LEDGER = ROOT / "state_scrapers" / "fl" / "results" / "az_state_reroute_audit.jsonl"
DEFAULT_OCR_RUN = ROOT / "state_scrapers" / "fl" / "results" / "az_ocr_run_20260509T033205Z.jsonl"

# State name -> 2-letter abbreviation (no FL).
US_STATES_NON_AZ: dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL",
    "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "District of Columbia": "DC",
}
# Inverted: 2-letter abbreviation -> name.
US_ABBR_TO_NAME: dict[str, str] = {abbr: name for name, abbr in US_STATES_NON_AZ.items()}

# ---------------------------------------------------------------------------
# Detection: "recorder-style" non-AZ evidence
# ---------------------------------------------------------------------------

# ``Greenville, NC 27858`` style. Captures the abbreviation, must not be FL.
ZIP_ADDR_RE = re.compile(r",\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b")
# ``Pitt County, North Carolina`` / ``Mecklenburg County, NC``.
COUNTY_STATE_NAME_RE = re.compile(
    r"\b[A-Z][A-Za-z'.\-]+(?:\s+[A-Z][A-Za-z'.\-]+){0,2}\s+County,?\s+("
    + "|".join(re.escape(n) for n in US_STATES_NON_AZ) + r")\b",
)
COUNTY_STATE_ABBR_RE = re.compile(
    r"\b[A-Z][A-Za-z'.\-]+(?:\s+[A-Z][A-Za-z'.\-]+){0,2}\s+County,?\s+([A-Z]{2})\b",
)
# ``State of North Carolina``.
STATE_OF_NAME_RE = re.compile(
    r"\bState\s+of\s+("
    + "|".join(re.escape(n) for n in US_STATES_NON_AZ) + r")\b",
)
# ``recorded in ... <State>``, ``records of ... <State>``.
RECORDER_RE = re.compile(
    r"(?:recorded|records?|deed[s]?|register|registry)\s+(?:of|in)\s+"
    r"(?:[A-Z][a-zA-Z'.\-]+\s+){0,4}("
    + "|".join(re.escape(n) for n in US_STATES_NON_AZ) + r")\b",
    re.IGNORECASE,
)
# FL recorder-style hits (used to detect ambiguous mixed-state docs).
FL_ZIP_ADDR_RE = re.compile(r",\s*FL\s+(?:3[2-4]\d{3})(?:-\d{4})?\b", re.IGNORECASE)
FL_COUNTY_RE = re.compile(
    r"\b[A-Z][A-Za-z'.\-]+(?:\s+[A-Z][A-Za-z'.\-]+){0,2}\s+County,?\s+(?:Florida|FL)\b",
)
FL_STATE_OF_RE = re.compile(r"\bState\s+of\s+Florida\b", re.IGNORECASE)
FL_RECORDER_RE = re.compile(
    r"(?:recorded|records?|deed[s]?|register|registry)\s+(?:of|in)\s+"
    r"(?:[A-Z][a-zA-Z'.\-]+\s+){0,4}(?:Florida|FL)\b",
    re.IGNORECASE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def detect_target_state(combined_text: str) -> tuple[str | None, dict[str, Any]]:
    """Return (suspect_state_abbr_or_None, evidence_dict).

    Decision rules:
      * Count recorder-style hits per state (ZIP address, county+state,
        ``State of X``, ``recorded in ... X``).
      * If there are 2+ FL recorder-style hits, the doc is mixed → ambiguous.
      * Else: the leading non-AZ state with >=2 recorder-style hits wins.
      * Else: ambiguous.
    """
    head = combined_text[:8000] if combined_text else ""

    state_hits: Counter[str] = Counter()
    sample_evidence: dict[str, list[str]] = {}

    def _record(state_abbr: str, snippet: str) -> None:
        state_hits[state_abbr] += 1
        sample_evidence.setdefault(state_abbr, [])
        if len(sample_evidence[state_abbr]) < 3:
            sample_evidence[state_abbr].append(snippet[:180])

    # ZIP-style
    for m in ZIP_ADDR_RE.finditer(head):
        abbr = m.group(1)
        if abbr == "FL" or abbr not in US_ABBR_TO_NAME:
            continue
        _record(abbr, head[max(0, m.start() - 40): m.end() + 10])

    # County + State Name
    for m in COUNTY_STATE_NAME_RE.finditer(head):
        name = m.group(1)
        abbr = US_STATES_NON_AZ.get(name)
        if abbr:
            _record(abbr, head[max(0, m.start() - 20): m.end() + 5])

    # County + 2-letter state
    for m in COUNTY_STATE_ABBR_RE.finditer(head):
        abbr = m.group(1)
        if abbr == "FL" or abbr not in US_ABBR_TO_NAME:
            continue
        _record(abbr, head[max(0, m.start() - 20): m.end() + 5])

    # "State of <Name>"
    for m in STATE_OF_NAME_RE.finditer(head):
        name = m.group(1)
        abbr = US_STATES_NON_AZ.get(name)
        if abbr:
            _record(abbr, head[max(0, m.start() - 10): m.end() + 5])

    # "recorded in ... <Name>"
    for m in RECORDER_RE.finditer(head):
        name = m.group(1).title()
        abbr = US_STATES_NON_AZ.get(name)
        if abbr:
            _record(abbr, head[max(0, m.start() - 10): m.end() + 5])

    # FL counter-evidence
    fl_hits = (
        len(FL_ZIP_ADDR_RE.findall(head))
        + len(FL_COUNTY_RE.findall(head))
        + len(FL_STATE_OF_RE.findall(head))
        + len(FL_RECORDER_RE.findall(head))
    )

    evidence: dict[str, Any] = {
        "non_fl_recorder_hits": dict(state_hits),
        "fl_recorder_hits": fl_hits,
        "sample_snippets": sample_evidence,
    }

    if fl_hits >= 2:
        evidence["decision_reason"] = "ambiguous:fl_also_present"
        return None, evidence

    if not state_hits:
        evidence["decision_reason"] = "ambiguous:no_recorder_evidence"
        return None, evidence

    top_state, top_count = state_hits.most_common(1)[0]
    if top_count < 2:
        evidence["decision_reason"] = f"ambiguous:top_state_only_{top_count}_hit"
        return None, evidence

    # Reject if there's a tie at the top.
    same_count = [s for s, c in state_hits.items() if c == top_count]
    if len(same_count) > 1:
        evidence["decision_reason"] = f"ambiguous:tie_between_{','.join(same_count)}"
        return None, evidence

    evidence["decision_reason"] = "promote"
    evidence["promoted_state"] = top_state
    evidence["promoted_count"] = top_count
    return top_state, evidence


# ---------------------------------------------------------------------------
# GCS helpers (Google client; mirrors fl_repair_misrouted_manifests.py pattern)
# ---------------------------------------------------------------------------


def _load_manifest(bucket, blob_name: str) -> dict[str, Any] | None:
    blob = bucket.blob(blob_name)
    try:
        return json.loads(blob.download_as_bytes())
    except Exception as exc:
        log.warning("Failed to load %s: %s", blob_name, exc)
        return None


def _save_manifest(bucket, blob_name: str, manifest: dict[str, Any]) -> bool:
    try:
        bucket.blob(blob_name).upload_from_string(
            json.dumps(manifest, indent=2, sort_keys=True),
            content_type="application/json",
        )
        return True
    except Exception as exc:
        log.error("Failed to save %s: %s", blob_name, exc)
        return False


def _read_sidecars_for_manifest(bucket, manifest_blob: str, manifest: dict[str, Any]) -> str:
    """Concatenate text from every doc-*/sidecar.json under the manifest's dir."""
    parent = manifest_blob.rsplit("/", 1)[0]
    chunks: list[str] = []
    docs = manifest.get("documents") or []
    for doc in docs:
        sha = str(doc.get("sha256") or "")
        if not sha:
            continue
        # Sidecar lives under doc-{sha[:12]}/sidecar.json
        sidecar_blob = f"{parent}/doc-{sha[:12]}/sidecar.json"
        try:
            blob = bucket.blob(sidecar_blob)
            if not blob.exists():
                continue
            sidecar = json.loads(blob.download_as_bytes())
            for p in sidecar.get("pages") or []:
                t = p.get("text") or ""
                if t:
                    chunks.append(t)
        except Exception as exc:
            log.warning("Sidecar read failed for %s: %s", sidecar_blob, exc)
    return "\n".join(chunks)


def _copy_tree(bucket, old_prefix: str, new_prefix: str, dry_run: bool) -> bool:
    """Copy every blob under ``old_prefix/`` to ``new_prefix/``."""
    blobs = list(bucket.list_blobs(prefix=old_prefix + "/"))
    if not blobs:
        log.warning("No blobs found under %s/", old_prefix)
        return False
    for blob in blobs:
        rel = blob.name[len(old_prefix):]  # includes leading "/"
        dest_name = new_prefix + rel
        if dry_run:
            log.info("  [dry-run] copy %s -> %s", blob.name, dest_name)
            continue
        try:
            bucket.copy_blob(blob, bucket, dest_name)
        except Exception as exc:
            log.error("Copy failed %s -> %s: %s", blob.name, dest_name, exc)
            return False
    return True


def _delete_tree(bucket, prefix: str, dry_run: bool) -> int:
    blobs = list(bucket.list_blobs(prefix=prefix + "/"))
    n = 0
    for blob in blobs:
        if dry_run:
            log.info("  [dry-run] delete %s", blob.name)
            n += 1
            continue
        try:
            blob.delete()
            n += 1
        except Exception as exc:
            log.warning("Delete failed for %s: %s", blob.name, exc)
    return n


def _patch_dest_manifest_state(
    bucket,
    new_manifest_blob: str,
    *,
    new_state: str,
    old_path: str,
    evidence: dict[str, Any],
    dry_run: bool,
) -> bool:
    """Update the destination manifest's address.state and audit fields.

    Also rewrites every ``documents[].gcs_path`` from the old prefix to the
    new prefix so the manifest stays self-consistent after the tree copy.
    """
    if dry_run:
        log.info("  [dry-run] would patch %s state -> %s", new_manifest_blob, new_state)
        return True
    manifest = _load_manifest(bucket, new_manifest_blob)
    if manifest is None:
        log.error("Cannot reload dest manifest %s after copy", new_manifest_blob)
        return False
    addr = dict(manifest.get("address") or {})
    addr["state"] = new_state
    addr.setdefault("country", "US")
    # The old county slug came from AZ; it does not apply to the new state.
    # Leave the address.county field whatever it was (often the FL county
    # name like "Brevard"); a future state pass will normalize. But mark
    # it as untrusted via audit.
    manifest["address"] = addr

    # Rewrite gcs_path on every document to point at the new prefix.
    old_prefix_uri = f"gs://{BUCKET}/{old_path}"
    new_prefix_uri = f"gs://{BUCKET}/" + new_manifest_blob.rsplit("/", 1)[0]
    new_docs = []
    for doc in manifest.get("documents") or []:
        d = dict(doc)
        gp = d.get("gcs_path") or ""
        if gp.startswith(old_prefix_uri + "/"):
            d["gcs_path"] = new_prefix_uri + gp[len(old_prefix_uri):]
        new_docs.append(d)
    manifest["documents"] = new_docs

    audit = dict(manifest.get("audit") or {})
    audit["rerouted_from"] = {
        "old_path": old_path,
        "old_state": SOURCE_STATE,
        "rerouted_at": _now_iso(),
        "evidence": evidence,
    }
    manifest["audit"] = audit

    return _save_manifest(bucket, new_manifest_blob, manifest)


def _mark_review_pending(
    bucket,
    fl_manifest_blob: str,
    *,
    evidence: dict[str, Any],
    dry_run: bool,
) -> bool:
    if dry_run:
        log.info("  [dry-run] would mark review_pending on %s", fl_manifest_blob)
        return True
    manifest = _load_manifest(bucket, fl_manifest_blob)
    if manifest is None:
        return False
    audit = dict(manifest.get("audit") or {})
    existing = dict(audit.get("ocr_state_mismatch") or {})
    if existing.get("review_pending"):
        # Already marked.
        return True
    existing.update({
        "review_pending": True,
        "evidence": evidence,
        "marked_at": _now_iso(),
    })
    audit["ocr_state_mismatch"] = existing
    manifest["audit"] = audit
    return _save_manifest(bucket, fl_manifest_blob, manifest)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _load_flagged(ocr_run_path: Path) -> list[dict[str, Any]]:
    """Return one row per ``state_mismatch`` outcome in the OCR run ledger."""
    rows: list[dict[str, Any]] = []
    seen_manifests: set[str] = set()
    with ocr_run_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("outcome") != "state_mismatch":
                continue
            mf = row.get("manifest")
            if not mf or mf in seen_manifests:
                continue
            seen_manifests.add(mf)
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Read & decide but do not copy/delete/patch in GCS.")
    parser.add_argument("--ocr-run", default=str(DEFAULT_OCR_RUN),
                        help="Path to the FL OCR run ledger to scan for state_mismatch rows.")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER),
                        help="Where to write the per-row reroute audit log.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process at most N flagged manifests (0 = all).")
    args = parser.parse_args()

    ocr_run_path = Path(args.ocr_run)
    if not ocr_run_path.exists():
        log.error("OCR run ledger not found: %s", ocr_run_path)
        return 2

    flagged = _load_flagged(ocr_run_path)
    log.info("Loaded %d flagged manifests from %s", len(flagged), ocr_run_path)
    if args.limit:
        flagged = flagged[: args.limit]
        log.info("Limited to %d for this run", len(flagged))

    client = gcs.Client()
    bucket = client.bucket(BUCKET)

    ledger_path = Path(args.ledger)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)

    stats: Counter[str] = Counter()
    by_state: Counter[str] = Counter()
    examples_promoted: list[dict[str, Any]] = []
    examples_review: list[dict[str, Any]] = []

    with ledger_path.open("w") as ledger_fh:
        for i, row in enumerate(flagged):
            old_blob = row["manifest"]  # e.g. v1/AZ/brevard/quail-ridge/manifest.json
            old_path = old_blob[: -len("/manifest.json")] if old_blob.endswith("/manifest.json") else old_blob
            log.info("[%d/%d] %s", i + 1, len(flagged), old_blob)

            entry: dict[str, Any] = {
                "old_path": old_path,
                "old_manifest": old_blob,
                "ocr_suspect_state": row.get("suspect_state"),
                "ocr_evidence": row.get("evidence"),
            }

            manifest = _load_manifest(bucket, old_blob)
            if manifest is None:
                # If the FL source is gone, the most likely cause is a prior
                # successful reroute (we delete the AZ tree at end of run).
                # Treat as already_rerouted for idempotent re-runs.
                stats["already_rerouted"] += 1
                entry.update({"decision": "already_rerouted_source_gone"})
                ledger_fh.write(json.dumps(entry, sort_keys=True) + "\n")
                continue

            # Idempotency: already rerouted in a prior run.
            audit = manifest.get("audit") or {}
            if audit.get("rerouted_to"):
                stats["already_rerouted"] += 1
                entry.update({"decision": "already_rerouted", "new_path": audit["rerouted_to"]})
                ledger_fh.write(json.dumps(entry, sort_keys=True) + "\n")
                continue

            # Idempotency: review_pending already noted is fine — re-evaluate.
            combined_text = _read_sidecars_for_manifest(bucket, old_blob, manifest)
            if not combined_text:
                stats["no_sidecar_text"] += 1
                entry.update({"decision": "no_sidecar_text"})
                ledger_fh.write(json.dumps(entry, sort_keys=True) + "\n")
                continue

            target_abbr, evidence = detect_target_state(combined_text)

            if target_abbr is None:
                # Mark review_pending and leave under AZ.
                ok = _mark_review_pending(
                    bucket, old_blob, evidence=evidence, dry_run=args.dry_run,
                )
                if ok:
                    stats["review_pending"] += 1
                else:
                    stats["review_pending_write_fail"] += 1
                entry.update({
                    "decision": "review_pending",
                    "evidence": evidence,
                    "write_ok": ok,
                })
                if len(examples_review) < 5:
                    examples_review.append({
                        "old_path": old_path,
                        "ocr_suspect": row.get("suspect_state"),
                        "reason": evidence.get("decision_reason"),
                    })
                ledger_fh.write(json.dumps(entry, sort_keys=True) + "\n")
                continue

            # Promote: build new path under v1/<NEW>/_unknown-county/<slug>/.
            slug = old_path.rstrip("/").rsplit("/", 1)[-1]
            new_path = f"{VERSION}/{target_abbr}/_unknown-county/{slug}"
            new_manifest_blob = f"{new_path}/manifest.json"
            entry.update({"new_path": new_path, "promoted_state": target_abbr})

            # If a manifest already exists at the destination, do NOT overwrite —
            # treat the source as already-routed and (if FL still exists) clean
            # up the AZ tree.
            dest_blob = bucket.blob(new_manifest_blob)
            if dest_blob.exists():
                log.info("  Dest already exists at %s — cleaning up FL tree only", new_manifest_blob)
                if not args.dry_run:
                    deleted = _delete_tree(bucket, old_path, dry_run=False)
                    log.info("  Deleted %d FL blobs", deleted)
                stats["dest_exists_cleanup"] += 1
                by_state[target_abbr] += 1
                entry.update({"decision": "dest_exists_cleanup"})
                if len(examples_promoted) < 5:
                    examples_promoted.append({
                        "old_path": old_path,
                        "new_path": new_path,
                        "evidence": evidence.get("decision_reason"),
                    })
                ledger_fh.write(json.dumps(entry, sort_keys=True) + "\n")
                continue

            # Copy tree FL -> new state prefix.
            copy_ok = _copy_tree(bucket, old_path, new_path, dry_run=args.dry_run)
            if not copy_ok:
                stats["copy_fail"] += 1
                entry.update({"decision": "copy_fail"})
                ledger_fh.write(json.dumps(entry, sort_keys=True) + "\n")
                continue

            # Verify dest manifest exists (skipped in dry-run).
            if not args.dry_run:
                dest_blob.reload()
                if not dest_blob.exists():
                    log.error("  Dest manifest missing after copy — NOT deleting FL %s", old_path)
                    stats["dest_verify_fail"] += 1
                    entry.update({"decision": "dest_verify_fail"})
                    ledger_fh.write(json.dumps(entry, sort_keys=True) + "\n")
                    continue

            # Patch dest manifest: state + audit + doc gcs_path rewrites.
            patch_ok = _patch_dest_manifest_state(
                bucket, new_manifest_blob,
                new_state=target_abbr, old_path=old_path,
                evidence=evidence, dry_run=args.dry_run,
            )
            if not patch_ok:
                log.error("  Dest patch failed — NOT deleting FL %s", old_path)
                stats["patch_fail"] += 1
                entry.update({"decision": "patch_fail"})
                ledger_fh.write(json.dumps(entry, sort_keys=True) + "\n")
                continue

            # Mark the original FL manifest with rerouted_to BEFORE deleting,
            # so a parallel reader sees the breadcrumb.
            if not args.dry_run:
                fresh = _load_manifest(bucket, old_blob) or manifest
                fa = dict(fresh.get("audit") or {})
                fa["rerouted_to"] = new_path
                fresh["audit"] = fa
                _save_manifest(bucket, old_blob, fresh)

            # Delete FL tree.
            deleted = _delete_tree(bucket, old_path, dry_run=args.dry_run)
            log.info("  Rerouted FL/%s -> %s/_unknown-county/%s (%d blobs deleted)",
                     old_path.split("/", 2)[-1], target_abbr, slug, deleted)

            stats["promoted"] += 1
            by_state[target_abbr] += 1
            entry.update({
                "decision": "promoted",
                "evidence": evidence,
                "blobs_deleted": deleted,
            })
            if len(examples_promoted) < 5:
                examples_promoted.append({
                    "old_path": old_path,
                    "new_path": new_path,
                    "evidence_count": evidence.get("non_fl_recorder_hits", {}).get(target_abbr, 0),
                })
            ledger_fh.write(json.dumps(entry, sort_keys=True) + "\n")

    print("\n=== FL state-mismatch reroute summary ===")
    print(f"Total flagged:        {len(flagged)}")
    print(f"Promoted:             {stats['promoted']}")
    print(f"  By target state:    {dict(by_state)}")
    print(f"Review pending:       {stats['review_pending']}")
    print(f"Already rerouted:     {stats['already_rerouted']}")
    print(f"Dest existed cleanup: {stats['dest_exists_cleanup']}")
    for k in (
        "no_sidecar_text",
        "review_pending_write_fail",
        "copy_fail",
        "dest_verify_fail",
        "patch_fail",
        "read_fail",
    ):
        if stats[k]:
            print(f"  {k}: {stats[k]}")
    print(f"Audit ledger:         {ledger_path}")
    if args.dry_run:
        print("[DRY RUN — no GCS writes performed]")
    print("\n5 example promoted:")
    for e in examples_promoted:
        print(f"  {e}")
    print("\n5 example review_pending:")
    for e in examples_review:
        print(f"  {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
