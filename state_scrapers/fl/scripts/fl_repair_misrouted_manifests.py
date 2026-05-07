"""Repair 22 FL manifests mis-routed under GA county slugs.

Background: A GA discovery pass detected "Florida" in PDF text and rerouted
`state` to FL, but the GA county name carried over because the city→county map
didn't fire for FL.  All 22 manifests live under GA county names
(bryan, carroll, chatham, dekalb, fulton, glynn, hall, henry, houston, paulding, walton)
but should be under their correct FL county prefix.

Usage:
    python scripts/fl_repair_misrouted_manifests.py [--dry-run]

Idempotent: re-running on already-corrected manifests is a no-op (destination
already exists, old path already gone).

Plan is written to data/fl_manifest_repair_plan.jsonl before any mutations.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Hard-coded repair plan (derived offline from source-URL / sunbiz / ZIP research)
# ---------------------------------------------------------------------------
# Each entry: (old_county, slug, new_county, strategy)
# new_county == "_unknown-county" means the HOA name is garbled OCR junk OR a
# generic document with no locatable FL county — we re-bank it under the
# _unknown-county prefix rather than deleting it.
REPAIR_PLAN = [
    (
        "bryan",
        "black-creek-and-of-any-or",
        "clay",
        "domain:blackcreekcdd.org → Black Creek CDD is in Clay County FL",
    ),
    (
        "bryan",
        "mbroke-pines-police-non-emergency-number-954-431-2200-please-pierpointe-five",
        "broward",
        "name:Pembroke Pines (954 area code) + sunbiz pierpointe master association → broward",
    ),
    (
        "carroll",
        "associations-and-use-of-property",
        "_unknown-county",
        "junk: Temple Law Review article — not an actual HOA",
    ),
    (
        "chatham",
        "holly-dahune-holly-river-birch-little-gem-magnolia-and-drake-river-landing",
        "st-johns",
        "name: River Landing at Twenty Mile → Nocatee/Twenty Mile = St. Johns County FL",
    ),
    (
        "dekalb",
        "profit-is-for-brookhaven-village",
        "_unknown-county",
        "wsimg generic; 'Brookhaven Village FL' amendment, no sunbiz match → unknown",
    ),
    (
        "fulton",
        "villa-floresta",
        "collier",
        "domain:wyndemerehomeowners.com → Wyndemere Naples FL; sunbiz wyndemere → collier",
    ),
    (
        "fulton",
        "you-will-be-a-member-of-a",
        "_unknown-county",
        "junk: generic lot purchase agreement, name is boilerplate text",
    ),
    (
        "glynn",
        "coral-ridge-country-club-estates",
        "broward",
        "domain:crcceassociation.com; sunbiz coral ridge country club estates → broward",
    ),
    (
        "glynn",
        "sterling-meadows-sterling-meadows",
        "seminole",
        "sunbiz: sterling meadows property owners association → seminole",
    ),
    (
        "hall",
        "access-to-inspection-and-copying-only",
        "_unknown-county",
        "junk: grsmgt.com generic rules doc; HOA name is regulatory text fragment",
    ),
    (
        "hall",
        "fairways-at-sandestin",
        "walton",
        "domain:sandestinowners.com; sunbiz fairways at sandestin → walton",
    ),
    (
        "hall",
        "nesville-fl-32608-2-2-fiscal-year-fiscal-year-of-outreach-from",
        "alachua",
        "ZIP 32608 in slug → alachua; domain:gainesvillegam.com → Gainesville FL",
    ),
    (
        "hall",
        "represented-in-negotiating-turnover-of",
        "_unknown-county",
        "junk: wsimg.com attorney CV / transaction profile, not an HOA document",
    ),
    (
        "henry",
        "formerly-known-as-hampton-estates",
        "_unknown-county",
        "rackcdn articles-of-incorp with numeric file ID; Hampton Estates absent from FL sunbiz",
    ),
    (
        "henry",
        "proper-operation-of",
        "_unknown-county",
        "junk: grsmgt.com UUID-named doc; HOA name is 'proper operation of the association'",
    ),
    (
        "houston",
        "garden-0464-yes-yes-yes-143-160-153-green-earthen-cheer-subdivision",
        "_unknown-county",
        "junk: garbled OCR name 'Garden 0464 Yes Yes Yes ...' is not a real HOA name",
    ),
    (
        "houston",
        "perry-e-lap-parties-to-that-certain-of-and",
        "_unknown-county",
        "junk: garbled OCR name 'PERRY E. LAP parties to that certain of and'",
    ),
    (
        "paulding",
        "wells-fargo-bank-my-name-is-robert-patton-and-i-am-with",
        "st-johns",
        "domain:middlevillagecdd.com → Middle Village CDD in St. Johns County FL",
    ),
    (
        "walton",
        "and",
        "_unknown-county",
        "junk: grsmgt.com generic doc; HOA name resolved to just 'AND'",
    ),
    (
        "walton",
        "of-old-still",
        "duval",
        "filename: 'Old Still - Bylaws'; sunbiz old still homeowners association → duval",
    ),
    (
        "walton",
        "our-mission-who-manages-communications-between",
        "_unknown-county",
        "wsimg newsletter; HOA name fragment 'OUR MISSION...' = garbled OCR junk",
    ),
    (
        "walton",
        "rainberry-park",
        "palm-beach",
        "name: Rainberry Park → Rainberry area is Delray Beach FL; sunbiz rainberry → palm-beach",
    ),
]

BUCKET = os.environ.get("HOA_BANK_GCS_BUCKET", "hoaproxy-bank")
VERSION = "v1"
STATE = "FL"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _old_prefix(old_county: str, slug: str) -> str:
    return f"{VERSION}/{STATE}/{old_county}/{slug}"


def _new_prefix(new_county: str, slug: str) -> str:
    return f"{VERSION}/{STATE}/{new_county}/{slug}"


def build_plan() -> list[dict]:
    rows = []
    for old_county, slug, new_county, strategy in REPAIR_PLAN:
        action = "rebank" if old_county != new_county else "no-op"
        rows.append(
            {
                "old_county": old_county,
                "slug": slug,
                "new_county": new_county,
                "old_path": f"gs://{BUCKET}/{_old_prefix(old_county, slug)}/",
                "new_manifest": f"gs://{BUCKET}/{_new_prefix(new_county, slug)}/manifest.json",
                "strategy": strategy,
                "action": action,
            }
        )
    return rows


def _copy_tree(bucket, old_prefix: str, new_prefix: str, dry_run: bool) -> bool:
    """Copy all blobs under old_prefix → new_prefix. Returns True on success."""
    from google.cloud import storage as gcs

    blobs = list(bucket.list_blobs(prefix=old_prefix + "/"))
    if not blobs:
        log.warning("No blobs found under %s", old_prefix)
        return False
    for blob in blobs:
        rel = blob.name[len(old_prefix):]  # includes leading /
        dest_name = new_prefix + rel
        if dry_run:
            log.info("  [dry-run] copy %s → %s", blob.name, dest_name)
        else:
            bucket.copy_blob(blob, bucket, dest_name)
            log.info("  copied %s → %s", blob.name, dest_name)
    return True


def _delete_tree(bucket, prefix: str, dry_run: bool) -> None:
    """Delete all blobs under prefix/."""
    blobs = list(bucket.list_blobs(prefix=prefix + "/"))
    for blob in blobs:
        if dry_run:
            log.info("  [dry-run] delete %s", blob.name)
        else:
            blob.delete()
            log.info("  deleted %s", blob.name)


def repair(dry_run: bool = False) -> dict:
    from google.cloud import storage as gcs

    client = gcs.Client()
    bucket = client.bucket(BUCKET)

    # Write plan file
    plan = build_plan()
    plan_path = Path(__file__).parent.parent / "data" / "fl_manifest_repair_plan.jsonl"
    plan_path.parent.mkdir(exist_ok=True)
    with open(plan_path, "w") as f:
        for row in plan:
            f.write(json.dumps(row) + "\n")
    log.info("Plan written to %s", plan_path)

    results = {"repaired": 0, "already_correct": 0, "unknown_county": 0, "failed": 0}

    for row in plan:
        old_county = row["old_county"]
        slug = row["slug"]
        new_county = row["new_county"]

        old_prefix = _old_prefix(old_county, slug)
        new_prefix = _new_prefix(new_county, slug)

        log.info("Processing %s/%s → %s/%s", old_county, slug, new_county, slug)

        # Idempotency check: if destination already exists and source is gone, skip
        dest_manifest = bucket.blob(f"{new_prefix}/manifest.json")
        old_manifest = bucket.blob(f"{old_prefix}/manifest.json")

        dest_exists = dest_manifest.exists()
        old_exists = old_manifest.exists()

        if old_county == new_county:
            # Shouldn't happen given the plan, but guard anyway
            log.info("  Already in correct county — no-op")
            results["already_correct"] += 1
            continue

        if dest_exists and not old_exists:
            log.info("  Already repaired (dest exists, old gone) — no-op")
            results["repaired"] += 1  # count as done
            continue

        if not old_exists:
            log.warning("  Source missing — skipping %s", old_prefix)
            results["failed"] += 1
            continue

        # Copy tree to new location
        copy_ok = _copy_tree(bucket, old_prefix, new_prefix, dry_run)
        if not copy_ok:
            log.error("  Copy failed for %s", old_prefix)
            results["failed"] += 1
            continue

        # Verify destination manifest was written before deleting source
        if not dry_run:
            dest_manifest.reload()
            if not dest_manifest.exists():
                log.error("  Destination manifest missing after copy — NOT deleting source %s", old_prefix)
                results["failed"] += 1
                continue

        # Delete old tree
        _delete_tree(bucket, old_prefix, dry_run)

        if new_county == "_unknown-county":
            log.info("  → moved to _unknown-county")
            results["unknown_county"] += 1
        else:
            log.info("  → repaired to %s", new_county)
            results["repaired"] += 1

    log.info("Summary: %s", results)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair FL manifests mis-routed under GA county slugs")
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes without applying them")
    args = parser.parse_args()

    if args.dry_run:
        log.info("=== DRY RUN — no changes will be made ===")

    # Print plan first
    plan = build_plan()
    print("\n=== REPAIR PLAN ===")
    print(f"{'old_county/slug':<55} {'new_county':<20} strategy")
    print("-" * 120)
    for row in plan:
        old = f"{row['old_county']}/{row['slug']}"
        print(f"{old:<55} {row['new_county']:<20} {row['strategy']}")
    print()

    results = repair(dry_run=args.dry_run)
    print("\n=== RESULTS ===")
    for k, v in results.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
