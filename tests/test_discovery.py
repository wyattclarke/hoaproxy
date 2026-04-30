"""Unit tests for hoaware.discovery (no GCS / network required)."""

import pytest

from hoaware.discovery.fingerprint import fingerprint
from hoaware.discovery.probe import (
    _harvest_doc_subpages,
    _harvest_pdf_candidates,
    _looks_like_govdoc,
    _looks_like_pdf_url,
    _normalize_url,
    _SKIP_URL_PATTERNS,
)
from hoaware.discovery.state_verify import verify_state


# --- fingerprint ---

def test_fingerprint_wordpress_wins_over_walled_widget():
    """WordPress with embedded ConnectResident login → wordpress (not walled)."""
    html = """
    <html><head><link rel="stylesheet" href="/wp-content/themes/foo.css"></head>
    <body><a href="https://portal.connectresident.com/">Resident Login</a></body></html>
    """
    fp = fingerprint(html)
    assert fp.name == "wordpress"
    assert fp.is_walled is False


def test_fingerprint_townsq_walled():
    html = "<html>Powered by TownSq <link href='townsq.io/styles.css'></html>"
    fp = fingerprint(html)
    assert fp.name == "townsq"
    assert fp.is_walled is True


def test_fingerprint_unknown():
    fp = fingerprint("<html><body>plain</body></html>")
    assert fp.name in ("unknown", "static")


def test_fingerprint_empty():
    fp = fingerprint("")
    assert fp.name == "unknown"
    assert fp.is_walled is False


# --- url helpers ---

def test_looks_like_pdf_url():
    assert _looks_like_pdf_url("https://example.com/foo.pdf")
    assert _looks_like_pdf_url("https://example.com/foo.pdf?token=abc")
    assert not _looks_like_pdf_url("https://example.com/foo.html")


def test_looks_like_govdoc():
    assert _looks_like_govdoc("https://example.com/declaration.pdf", "")
    assert _looks_like_govdoc("https://example.com/x", "Bylaws and Articles")
    assert _looks_like_govdoc("https://example.com/ccrs", "")
    assert not _looks_like_govdoc("https://example.com/contact", "Contact Us")


def test_normalize_url_skips_javascript_and_anchors():
    assert _normalize_url("https://example.com/", "#top") is None
    assert _normalize_url("https://example.com/", "javascript:void(0)") is None
    assert _normalize_url("https://example.com/", "mailto:a@b.com") is None
    assert _normalize_url("https://example.com/", "/foo") == "https://example.com/foo"


def test_skip_url_patterns_known_walls():
    assert _SKIP_URL_PATTERNS.search("https://portal.connectresident.com/x")
    assert _SKIP_URL_PATTERNS.search("https://drive.google.com/file/d/abc")
    assert _SKIP_URL_PATTERNS.search("https://x.appfolio.com/y")
    assert not _SKIP_URL_PATTERNS.search("https://example.org/docs/ccrs.pdf")


# --- harvesters ---

def test_harvest_pdf_candidates_finds_pdfs_and_govdoc_links():
    html = """
    <html><body>
    <a href="/docs/declaration.pdf">CC&Rs</a>
    <a href="/governance/articles">Articles of Incorporation</a>
    <a href="https://example.com/about">About</a>
    <a href="https://drive.google.com/foo">Drive</a>
    </body></html>
    """
    out = _harvest_pdf_candidates(html, "https://example.org/")
    urls = [u for u, _ in out]
    assert "https://example.org/docs/declaration.pdf" in urls
    assert "https://example.org/governance/articles" in urls
    assert not any("drive.google.com" in u for u in urls)
    assert not any(u.endswith("/about") for u in urls)


def test_harvest_doc_subpages_finds_docs_and_library_links():
    html = """
    <html><body>
    <a href="/documents">Documents</a>
    <a href="/our-rules">Rules</a>
    <a href="/contact">Contact</a>
    <a href="https://other-domain.com/library">External Library</a>
    </body></html>
    """
    out = _harvest_doc_subpages(html, "https://example.org/")
    assert "https://example.org/documents" in out
    assert "https://example.org/our-rules" in out
    assert not any("contact" in u for u in out)
    # Cross-domain rejected
    assert not any("other-domain" in u for u in out)


# --- state_verify ---

def test_verify_state_with_expected_match_high():
    sv = verify_state("Located in the Commonwealth of Virginia", "VA")
    assert sv.state == "VA"
    assert sv.confidence in ("high", "medium")


def test_verify_state_assumes_lead_when_no_match():
    sv = verify_state("Some text with no states", "CA")
    assert sv.state == "CA"
    assert sv.confidence == "low"


def test_verify_state_no_lead_no_text():
    sv = verify_state("", None)
    assert sv.state is None


def test_verify_state_finds_full_state_name():
    sv = verify_state("Wake County, North Carolina", None)
    assert sv.state == "NC"
