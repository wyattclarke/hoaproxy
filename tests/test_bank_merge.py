"""Unit tests for hoaware.bank merge semantics (no GCS required)."""

import pytest

from hoaware.bank import (
    SCHEMA_VERSION,
    _better_status,
    _dedup_append,
    _derive_status,
    _merge_manifest,
    _merge_scalar_dict,
    slugify,
)


def test_slugify_strips_hoa_noise_words():
    assert slugify("Reston Association, Inc.") == "reston"
    assert slugify("Hampton Meadow Cluster") == "hampton-meadow"
    assert slugify("Hampton Meadow Cluster HOA") == "hampton-meadow"
    assert slugify("Hampton Meadow Cluster Homeowners Association") == "hampton-meadow"


def test_slugify_falls_back_when_all_noise():
    # If every token would be stripped, the fallback preserves uniqueness.
    out = slugify("The HOA Inc")
    assert out  # non-empty
    assert out != ""


def test_slugify_handles_special_chars():
    assert slugify("Foo & Bar's HOA!") == "foo-bar-s"


def test_better_status_picks_higher_rank():
    assert _better_status("ready_with_docs", "stub_no_docs") == "ready_with_docs"
    assert _better_status("stub_walled", "stub_state_unknown") == "stub_walled"
    assert _better_status("stub_unverified", "ready_with_docs") == "ready_with_docs"


def test_dedup_append_keeps_first_occurrence():
    existing = [{"sha256": "a", "size": 1}]
    new = [{"sha256": "a", "size": 999}, {"sha256": "b", "size": 2}]
    out = _dedup_append(existing, new, key="sha256")
    assert len(out) == 2
    # Existing entry's size should be preserved (not overwritten by new "999")
    assert out[0]["size"] == 1


def test_merge_scalar_dict_first_write_wins_with_conflict_log():
    existing = {"city": "Reston", "state": "VA"}
    new = {"city": "Vienna", "state": "VA", "postal_code": "20191"}
    merged = _merge_scalar_dict(existing, new, ("city", "state", "postal_code"))
    assert merged["city"] == "Reston"  # first wins
    assert merged["postal_code"] == "20191"  # newly provided
    conflicts = merged.get("_conflicts", [])
    assert len(conflicts) == 1
    assert conflicts[0]["field"] == "city"


def test_merge_scalar_dict_newest_wins_overrides_first():
    existing = {"platform": "connectresident", "is_walled": True}
    new = {"platform": "wordpress", "is_walled": False}
    merged = _merge_scalar_dict(
        existing, new, ("platform", "is_walled"), newest_wins=("platform", "is_walled")
    )
    assert merged["platform"] == "wordpress"
    assert merged["is_walled"] is False
    assert merged.get("_conflicts", []) == []


def test_merge_manifest_aliases_old_canonical_name():
    existing = {"name": "Foo HOA", "name_aliases": []}
    new = {"name": "Foo Homeowners Association", "name_aliases": []}
    merged = _merge_manifest(existing, new)
    assert merged["name"] == "Foo HOA"  # first wins
    assert "Foo Homeowners Association" in merged["name_aliases"]


def test_merge_manifest_status_promotes_better():
    existing = {"discovery": {"status": "stub_unverified", "first_seen": "t0"}}
    new = {"discovery": {"status": "ready_with_docs"}}
    merged = _merge_manifest(existing, new)
    assert merged["discovery"]["status"] == "ready_with_docs"
    assert merged["discovery"]["first_seen"] == "t0"


def test_merge_manifest_dedups_documents_by_sha():
    existing = {"documents": [{"sha256": "abc", "size_bytes": 100}]}
    new = {
        "documents": [
            {"sha256": "abc", "size_bytes": 999},  # dup
            {"sha256": "def", "size_bytes": 200},  # new
        ]
    }
    merged = _merge_manifest(existing, new)
    shas = sorted(d["sha256"] for d in merged["documents"])
    assert shas == ["abc", "def"]


def test_merge_manifest_appends_metadata_sources():
    existing = {"metadata_sources": [{"source": "a"}]}
    new = {"metadata_sources": [{"source": "b"}, {"source": "c"}]}
    merged = _merge_manifest(existing, new)
    assert [s["source"] for s in merged["metadata_sources"]] == ["a", "b", "c"]


def test_merge_manifest_promotes_metadata_type_from_unknown():
    existing = {"metadata_type": "unknown"}
    new = {"metadata_type": "hoa"}
    merged = _merge_manifest(existing, new)
    assert merged["metadata_type"] == "hoa"


def test_derive_status_ready_when_docs_present():
    assert _derive_status(doc_records=[{"x": 1}], website={}, state="VA") == "ready_with_docs"


def test_derive_status_state_unknown_when_no_state():
    assert _derive_status(doc_records=[], website={}, state=None) == "stub_state_unknown"


def test_derive_status_walled_when_walled():
    assert _derive_status(doc_records=[], website={"is_walled": True}, state="VA") == "stub_walled"


def test_derive_status_no_docs_default():
    assert _derive_status(doc_records=[], website={}, state="VA") == "stub_no_docs"


def test_merge_keeps_schema_version():
    merged = _merge_manifest({}, {"name": "x"})
    assert merged["schema_version"] == SCHEMA_VERSION
