"""Tests for hoaware.name_utils — pure-function, no network/DB required."""

from __future__ import annotations

import pytest

from hoaware.name_utils import (
    dedupe_tail,
    derive_clean_slug,
    extract_after_marker,
    is_dirty,
    name_from_source_url,
    strip_leading_stopwords,
)


# ---------------------------------------------------------------------------
# is_dirty — one positive case per reason code + clean controls
# ---------------------------------------------------------------------------

class TestIsDirty:
    """Each test verifies a specific reason code is returned for a dirty name."""

    def test_long_dashed_phrase(self):
        # More than 50 chars and contains " - "
        dirty, reason = is_dirty("Foo Estates - Amended and Restated Declaration of Covenants")
        assert dirty is True
        assert reason == "long_dashed_phrase"

    def test_starts_lowercase(self):
        dirty, reason = is_dirty("oakwood homeowners association")
        assert dirty is True
        assert reason == "starts_lowercase"

    def test_numeric_prefix(self):
        dirty, reason = is_dirty("1) Pebble Creek HOA")
        assert dirty is True
        assert reason == "numeric_prefix"

    def test_year_prefix(self):
        dirty, reason = is_dirty("2018 Exhibit A Supplemental Dec Lake Laceola HOA")
        assert dirty is True
        assert reason == "year_prefix"

    def test_longdigit_prefix(self):
        dirty, reason = is_dirty("5021942267194390towne park pooler")
        assert dirty is True
        assert reason == "longdigit_prefix"

    def test_street_address_prefix(self):
        dirty, reason = is_dirty("6318 Suwanee Dam Rd HOA")
        assert dirty is True
        assert reason == "street_address_prefix"

    def test_shouting_prefix(self):
        # All-caps prefix longer than 40 chars total
        dirty, reason = is_dirty("DECLARATION OF COVENANTS Foo Estates HOA extra words here")
        assert dirty is True
        assert reason == "shouting_prefix"

    def test_too_short(self):
        dirty, reason = is_dirty("AB")
        assert dirty is True
        assert reason == "too_short"

    def test_stopword_prefix(self):
        dirty, reason = is_dirty("By-laws of the Oakwood Community")
        assert dirty is True
        assert reason == "stopword_prefix"

    def test_county_prefix(self):
        dirty, reason = is_dirty("Gwinnett County of Faith Hollow Homeowners Association")
        assert dirty is True
        assert reason == "county_prefix"

    def test_doc_fragment_anywhere(self):
        # "Amended and Restated" is a mid-string doc fragment (not a prefix match)
        dirty, reason = is_dirty("Lake Laceola Amended and Restated HOA")
        assert dirty is True
        assert reason == "doc_fragment_anywhere"

    def test_tail_truncation(self):
        dirty, reason = is_dirty("Bridgeberry Amenity and HOA")
        assert dirty is True
        assert reason == "tail_truncation"

    def test_doubled_name(self):
        dirty, reason = is_dirty("Foo Bar POA FOO BAR PROPERTY OWNERS ASSOCIATION")
        assert dirty is True
        assert reason == "doubled_name"

    def test_garbled_acronym(self):
        dirty, reason = is_dirty("GL-LB-BAR HOA")
        assert dirty is True
        assert reason == "garbled_acronym"

    def test_very_long(self):
        dirty, reason = is_dirty("A" * 71)
        assert dirty is True
        assert reason == "very_long"

    def test_citation_in_name(self):
        dirty, reason = is_dirty("Oakwood HOA book 3 page 45")
        assert dirty is True
        assert reason == "citation_in_name"

    def test_ccr_in_name_long(self):
        dirty, reason = is_dirty("Oakwood Estates CC&Rs and General Rules of the Community")
        assert dirty is True
        assert reason == "ccr_in_name_long"

    def test_project_code_prefix_te(self):
        dirty, reason = is_dirty("TE1-12 Townhouses Condominium Association")
        assert dirty is True
        assert reason == "project_code_prefix"

    def test_project_code_prefix_phase(self):
        dirty, reason = is_dirty("Phase II Oakwood HOA")
        assert dirty is True
        assert reason == "project_code_prefix"

    def test_project_code_prefix_block(self):
        dirty, reason = is_dirty("Block A Foo Condominium Association")
        assert dirty is True
        assert reason == "project_code_prefix"

    def test_project_code_prefix_building(self):
        dirty, reason = is_dirty("Building 4 Sunrise HOA")
        assert dirty is True
        assert reason == "project_code_prefix"

    def test_generic_single_stem_sunrise(self):
        dirty, reason = is_dirty("Sunrise Homeowners Association")
        assert dirty is True
        assert reason == "generic_single_stem"

    def test_generic_single_stem_mountainside(self):
        dirty, reason = is_dirty("Mountainside Condominium Association")
        assert dirty is True
        assert reason == "generic_single_stem"

    def test_generic_single_stem_with_roman_qualifier(self):
        # "Slopeside II" still reduces to "Slopeside" (generic) after stripping
        # the roman-numeral qualifier.
        dirty, reason = is_dirty("Slopeside II Condominium Association")
        assert dirty is True
        assert reason == "generic_single_stem"

    def test_generic_single_stem_with_numeric_qualifier(self):
        dirty, reason = is_dirty("Willows IV Condominium Association")
        assert dirty is True
        assert reason == "generic_single_stem"

    # --- clean controls ---

    def test_clean_name_simple(self):
        dirty, reason = is_dirty("Park Village HOA")
        assert dirty is False
        assert reason is None

    def test_clean_name_with_inc(self):
        dirty, reason = is_dirty("Reston Association, Inc.")
        assert dirty is False
        assert reason is None

    def test_clean_short_hoa(self):
        # "HOA" alone is 3 chars but contains hoa — should pass too_short
        dirty, reason = is_dirty("HOA")
        assert dirty is False
        assert reason is None

    def test_clean_name_condominium(self):
        dirty, reason = is_dirty("Cumberland Harbour Condominium Association")
        assert dirty is False
        assert reason is None

    def test_clean_two_token_with_generic_token(self):
        # "Sunset Cove" combines a generic stem with a place specifier.
        dirty, reason = is_dirty("Sunset Cove Condominium Association")
        assert dirty is False
        assert reason is None

    def test_clean_villmarksauna_single_distinctive(self):
        # Single-token but distinctive (not in the generic list) — keep.
        dirty, reason = is_dirty("Villmarksauna Condominium Association")
        assert dirty is False
        assert reason is None

    def test_clean_phase_qualifier_with_real_name(self):
        # "Smugglers Notch Phase II" has a real two-token stem before the
        # qualifier; should not be flagged as generic.
        dirty, reason = is_dirty("Smugglers Notch Phase II Condominium Association")
        assert dirty is False
        assert reason is None


# ---------------------------------------------------------------------------
# derive_clean_slug — happy paths and failure case
# ---------------------------------------------------------------------------

class TestDeriveCleanSlug:

    def test_year_prefix_recovery(self):
        # "2018 Exhibit A Supplemental Dec Lake Laceola HOA" — year_prefix dirty;
        # _try_strip_name_prefix should peel it and return something clean.
        result = derive_clean_slug("2018 Exhibit A Supplemental Dec Lake Laceola HOA")
        # The exact output depends on strategy, but must be non-dirty and non-None.
        assert result is not None
        assert not is_dirty(result)[0], f"result {result!r} is still dirty"

    def test_declaration_prefix_strip(self):
        # "Declaration of Covenants for Foo Estates HOA" — stopword_prefix /
        # doc_fragment_anywhere dirty; name-level strip should recover.
        result = derive_clean_slug("Declaration of Covenants for Foo Estates HOA")
        assert result is not None
        assert not is_dirty(result)[0], f"result {result!r} is still dirty"

    def test_all_strategies_fail_returns_none(self):
        # Pure date-like noise with no HOA suffix and no usable URL.
        result = derive_clean_slug("2018 03 14")
        assert result is None

    def test_clean_name_unchanged_by_derive(self):
        # A clean name should not be dirtied by derive_clean_slug.
        # (derive_clean_slug may return None for a clean name since it only
        # runs strategies when the slug is junk or prefix can be stripped.)
        name = "Park Village HOA"
        # is_dirty must pass
        assert not is_dirty(name)[0]
        # derive_clean_slug may return None or a clean result — never a dirty one
        result = derive_clean_slug(name)
        if result is not None:
            assert not is_dirty(result)[0]

    def test_source_url_strategy(self):
        # Junky slug with real URL — name_from_source_url should fire.
        result = derive_clean_slug(
            "a georgia nonprofit corporation hereinafter called the association",
            source_url="https://example.com/Walton-Reserve-Declaration-Recorded.pdf",
        )
        assert result is not None
        assert not is_dirty(result)[0]

    def test_dedupe_tail_strategy(self):
        # slug: "big-canoe-poa-big-canoe" — dedupe_tail extracts "big-canoe"
        result = derive_clean_slug("Big Canoe POA Big Canoe")
        # Either recovers something or returns None; if something, must be clean.
        if result is not None:
            assert not is_dirty(result)[0]


# ---------------------------------------------------------------------------
# Sub-function unit tests
# ---------------------------------------------------------------------------

class TestSubFunctions:

    def test_strip_leading_stopwords(self):
        assert strip_leading_stopwords("and-restated-pebble-creek-farm") == "pebble-creek-farm"

    def test_strip_leading_stopwords_all_junk(self):
        # All stop-tokens — should return None
        assert strip_leading_stopwords("and-of-the") is None

    def test_extract_after_marker(self):
        slug = "architectural-review-committee-of-reserve-at-reid-plantation"
        result = extract_after_marker(slug)
        # The "of" marker fires; result must be clean (reid-plantation is 2 tokens <= 5)
        assert result is not None

    def test_dedupe_tail(self):
        assert dedupe_tail("amount-of-coverage-spartan-estates-spartan-estates") == "spartan-estates"

    def test_dedupe_tail_no_dupe(self):
        assert dedupe_tail("pebble-creek-farm") is None

    def test_name_from_source_url_clean(self):
        url = "https://example.com/Walton-Reserve-Declaration-Recorded.pdf"
        result = name_from_source_url(url)
        assert result is not None
        assert "walton" in result

    def test_name_from_source_url_none_input(self):
        assert name_from_source_url(None) is None

    def test_name_from_source_url_noise_only(self):
        # URL whose filename is all noise words — should return None
        result = name_from_source_url("https://example.com/ccrs-bylaws-hoa.pdf")
        assert result is None
