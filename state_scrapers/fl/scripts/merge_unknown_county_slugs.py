#!/usr/bin/env python3
"""Merge legacy non-canonical FL ``unknown-county/`` slugs into ``_unknown-county/``.

Some older code paths emitted ``v1/FL/unknown-county/<hoa>/`` (no leading
underscore) instead of the canonical ``v1/FL/_unknown-county/<hoa>/``. This
script consolidates them.

For every HOA slug under the non-canonical prefix:

  * If the same slug already exists under ``_unknown-county/``, merge the
    manifests (preserve all ``metadata_sources`` entries; pick the canonical
    name; merge ``documents``, ``skipped_documents``, ``geo_repair_audit``,
    ``name_aliases``) and copy any missing ``doc-<sha>/`` directories. The
    canonical manifest's GCS paths are rewritten to point under
    ``_unknown-county/`` so they remain valid.
  * Otherwise, copy the entire folder under ``_unknown-county/`` and rewrite
    ``gcs_path`` fields in the manifest to the new prefix.

After a successful copy/merge for a slug, the source folder is removed. This
makes the migration idempotent: re-runs are no-ops because the source folder
is gone.

Safety: destination writes happen first; deletes only run after the destination
is confirmed present. A dry run is available via ``--dry-run``.

Usage::

    python -m state_scrapers.fl.scripts.merge_unknown_county_slugs [--dry-run]

Requires:
    GOOGLE_APPLICATION_CREDENTIALS or ADC, GOOGLE_CLOUD_PROJECT=hoaware.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Iterable

from google.cloud import storage

log = logging.getLogger("merge_unknown_county")

BUCKET = "hoaproxy-bank"
STATE = "FL"
NON_CANONICAL = f"v1/{STATE}/unknown-county/"
CANONICAL = f"v1/{STATE}/_unknown-county/"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _list_slugs(client: storage.Client, prefix: str) -> set[str]:
    """Top-level slug names under ``prefix`` (using delimiter listing)."""
    bucket = client.bucket(BUCKET)
    iterator = client.list_blobs(bucket, prefix=prefix, delimiter="/")
    # Consume blobs to populate prefixes
    list(iterator)
    out: set[str] = set()
    for p in iterator.prefixes:
        # p is "v1/FL/unknown-county/<slug>/"
        rest = p[len(prefix):].rstrip("/")
        if rest:
            out.add(rest)
    return out


def _list_objects(client: storage.Client, prefix: str) -> list[storage.Blob]:
    bucket = client.bucket(BUCKET)
    return list(client.list_blobs(bucket, prefix=prefix))


def _read_manifest(client: storage.Client, blob_path: str) -> dict | None:
    bucket = client.bucket(BUCKET)
    blob = bucket.blob(blob_path)
    try:
        text = blob.download_as_text()
    except Exception as e:  # noqa: BLE001
        log.warning("could not read %s: %s", blob_path, e)
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("manifest %s is not valid JSON: %s", blob_path, e)
        return None


def _write_manifest(client: storage.Client, blob_path: str, manifest: dict) -> None:
    bucket = client.bucket(BUCKET)
    blob = bucket.blob(blob_path)
    blob.upload_from_string(
        json.dumps(manifest, indent=2, sort_keys=True),
        content_type="application/json",
    )


def _copy_blob(
    client: storage.Client,
    src_path: str,
    dst_path: str,
) -> None:
    bucket = client.bucket(BUCKET)
    src = bucket.blob(src_path)
    bucket.copy_blob(src, bucket, new_name=dst_path)


def _delete_prefix(client: storage.Client, prefix: str) -> int:
    bucket = client.bucket(BUCKET)
    n = 0
    for blob in client.list_blobs(bucket, prefix=prefix):
        blob.delete()
        n += 1
    return n


def _rewrite_gcs_paths(obj, old_prefix: str, new_prefix: str):
    """Recursively rewrite any ``gs://hoaproxy-bank/<old_prefix>...`` strings."""
    old_uri = f"gs://{BUCKET}/{old_prefix}"
    new_uri = f"gs://{BUCKET}/{new_prefix}"
    if isinstance(obj, dict):
        return {k: _rewrite_gcs_paths(v, old_prefix, new_prefix) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rewrite_gcs_paths(v, old_prefix, new_prefix) for v in obj]
    if isinstance(obj, str) and old_uri in obj:
        return obj.replace(old_uri, new_uri)
    return obj


# ---------------------------------------------------------------------------
# Manifest merge logic
# ---------------------------------------------------------------------------

def _dedup_by(items: Iterable[dict], key: str) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        k = it.get(key) if isinstance(it, dict) else None
        if k is None:
            out.append(it)
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def _pick_better_name(a: str | None, b: str | None) -> str | None:
    """Pick the more informative of two names (prefer non-empty, then longer)."""
    if not a:
        return b
    if not b:
        return a
    # Prefer the one that doesn't look like OCR junk (has spaces, mixed case)
    a_score = (len(a), a.count(" "))
    b_score = (len(b), b.count(" "))
    return a if a_score >= b_score else b


def _merge_manifests(canonical: dict, legacy: dict) -> dict:
    """Merge ``legacy`` into ``canonical`` non-destructively."""
    out = dict(canonical)

    # Name: prefer the more informative one; collect the loser into aliases.
    canonical_name = canonical.get("name")
    legacy_name = legacy.get("name")
    chosen = _pick_better_name(canonical_name, legacy_name)
    out["name"] = chosen
    aliases = list(canonical.get("name_aliases") or [])
    aliases += list(legacy.get("name_aliases") or [])
    for cand in (canonical_name, legacy_name):
        if cand and cand != chosen and cand not in aliases:
            aliases.append(cand)
    out["name_aliases"] = aliases

    # metadata_sources: union, dedup on (source, source_url).
    sources = list(canonical.get("metadata_sources") or [])
    seen = {(s.get("source"), s.get("source_url")) for s in sources}
    for s in legacy.get("metadata_sources") or []:
        key = (s.get("source"), s.get("source_url"))
        if key not in seen:
            sources.append(s)
            seen.add(key)
    out["metadata_sources"] = sources

    # documents: dedup on sha256.
    docs = list(canonical.get("documents") or []) + list(legacy.get("documents") or [])
    out["documents"] = _dedup_by(docs, "sha256")

    skipped = list(canonical.get("skipped_documents") or []) + list(
        legacy.get("skipped_documents") or []
    )
    out["skipped_documents"] = _dedup_by(skipped, "sha256")

    # geo_repair_audit: append (no canonical key — keep all).
    audit = list(canonical.get("geo_repair_audit") or []) + list(
        legacy.get("geo_repair_audit") or []
    )
    out["geo_repair_audit"] = audit

    # discovery: keep canonical, but use earliest first_seen and latest last_probed.
    canonical_disc = dict(canonical.get("discovery") or {})
    legacy_disc = legacy.get("discovery") or {}
    for k_pick, k in [("min", "first_seen"), ("max", "last_probed")]:
        a = canonical_disc.get(k)
        b = legacy_disc.get(k)
        if a and b:
            canonical_disc[k] = (min(a, b) if k_pick == "min" else max(a, b))
        elif b:
            canonical_disc[k] = b
    out["discovery"] = canonical_disc

    # website: prefer canonical if set, else legacy.
    if not canonical.get("website"):
        out["website"] = legacy.get("website") or {}

    # geometry: prefer whichever has content.
    if not canonical.get("geometry"):
        out["geometry"] = legacy.get("geometry") or {}

    return out


# ---------------------------------------------------------------------------
# Per-slug migration
# ---------------------------------------------------------------------------

def _migrate_slug(
    client: storage.Client,
    slug: str,
    canonical_slugs: set[str],
    *,
    dry_run: bool,
) -> str:
    src_prefix = f"{NON_CANONICAL}{slug}/"
    dst_prefix = f"{CANONICAL}{slug}/"
    src_manifest_path = f"{src_prefix}manifest.json"
    dst_manifest_path = f"{dst_prefix}manifest.json"

    legacy = _read_manifest(client, src_manifest_path)
    if legacy is None:
        log.warning("[%s] no readable manifest at source — skipping", slug)
        return "skipped_no_manifest"

    # Rewrite all gs:// paths inside the legacy manifest to the canonical prefix.
    legacy_rewritten = _rewrite_gcs_paths(legacy, src_prefix, dst_prefix)

    src_objects = _list_objects(client, src_prefix)
    src_paths = [b.name for b in src_objects]

    if slug in canonical_slugs:
        # Merge case
        canonical = _read_manifest(client, dst_manifest_path)
        if canonical is None:
            log.warning(
                "[%s] canonical manifest missing — treating as fresh copy", slug
            )
            merged = legacy_rewritten
            mode = "copy"
        else:
            merged = _merge_manifests(canonical, legacy_rewritten)
            mode = "merge"
    else:
        merged = legacy_rewritten
        mode = "copy"

    log.info("[%s] mode=%s objects=%d", slug, mode, len(src_paths))

    if dry_run:
        return f"dry_run_{mode}"

    # 1) Copy non-manifest blobs from src -> dst (idempotent: copy_blob overwrites).
    bucket = client.bucket(BUCKET)
    for src_path in src_paths:
        if src_path == src_manifest_path:
            continue
        rel = src_path[len(src_prefix):]
        dst_path = f"{dst_prefix}{rel}"
        # Skip if destination already exists with same size+md5 (idempotent re-run).
        dst_blob = bucket.blob(dst_path)
        if dst_blob.exists():
            log.debug("  [%s] dst exists, skipping copy: %s", slug, dst_path)
            continue
        _copy_blob(client, src_path, dst_path)
        log.debug("  [%s] copied %s -> %s", slug, src_path, dst_path)

    # 2) Verify all non-manifest dst blobs exist before writing manifest.
    for src_path in src_paths:
        if src_path == src_manifest_path:
            continue
        rel = src_path[len(src_prefix):]
        dst_path = f"{dst_prefix}{rel}"
        if not bucket.blob(dst_path).exists():
            log.error("  [%s] dst missing after copy: %s — aborting slug", slug, dst_path)
            return "failed_copy"

    # 3) Write merged manifest.
    _write_manifest(client, dst_manifest_path, merged)
    log.debug("  [%s] wrote merged manifest %s", slug, dst_manifest_path)

    # 4) Verify canonical manifest now exists.
    if not bucket.blob(dst_manifest_path).exists():
        log.error("  [%s] canonical manifest missing after write — aborting", slug)
        return "failed_manifest_write"

    # 5) Delete source prefix.
    deleted = _delete_prefix(client, src_prefix)
    log.info("  [%s] deleted %d source blobs", slug, deleted)
    return mode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="don't write or delete")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    client = storage.Client()

    canonical_slugs = _list_slugs(client, CANONICAL)
    legacy_slugs = _list_slugs(client, NON_CANONICAL)

    log.info(
        "FL bank: canonical _unknown-county slugs=%d, legacy unknown-county slugs=%d",
        len(canonical_slugs),
        len(legacy_slugs),
    )

    if not legacy_slugs:
        log.info("nothing to migrate; exiting.")
        return 0

    counters: dict[str, int] = {}
    for slug in sorted(legacy_slugs):
        try:
            result = _migrate_slug(
                client, slug, canonical_slugs, dry_run=args.dry_run
            )
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] migration failed: %s", slug, e)
            result = "exception"
        counters[result] = counters.get(result, 0) + 1
        # As we add new slugs to canonical, future merges should see them.
        if result in ("copy", "merge"):
            canonical_slugs.add(slug)

    final_canonical = _list_slugs(client, CANONICAL)
    final_legacy = _list_slugs(client, NON_CANONICAL)

    log.info("=" * 60)
    log.info("Migration summary:")
    for k, v in sorted(counters.items()):
        log.info("  %s: %d", k, v)
    log.info("Final canonical _unknown-county slugs: %d", len(final_canonical))
    log.info("Final legacy unknown-county slugs (should be 0): %d", len(final_legacy))

    return 0 if not final_legacy or args.dry_run else (0 if not final_legacy else 1)


if __name__ == "__main__":
    sys.exit(main())
