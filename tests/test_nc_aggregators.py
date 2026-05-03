"""Tests for hoaware.discovery.sources.nc_aggregators (no network required)."""

import json
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
import requests

from hoaware.discovery.leads import Lead
from hoaware.discovery.sources.nc_aggregators import (
    ALL_SOURCES,
    _WEBSITE_SOURCES,
    _DIRECT_PDF_SOURCES,
    casnc_leads,
    seaside_leads,
    signature_leads,
    bam_leads,
    wake_hoa_leads,
    closing_carolina_leads,
    triad_leads,
    wilson_pm_leads,
    nc_leads,
    nc_leads_with_pdfs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(html: str, status_code: int = 200) -> MagicMock:
    r = MagicMock(spec=requests.Response)
    r.status_code = status_code
    r.text = html
    r.headers = {}
    return r


def _make_session(html_by_url: dict[str, str]) -> MagicMock:
    """Session mock: returns preset HTML for known URLs, 404 for others."""
    session = MagicMock(spec=requests.Session)
    session.headers = MagicMock()
    session.headers.update = MagicMock()

    def _get(url, **kwargs):
        if url in html_by_url:
            return _mock_response(html_by_url[url])
        r = MagicMock(spec=requests.Response)
        r.status_code = 404
        return r

    session.get.side_effect = _get
    return session


# ---------------------------------------------------------------------------
# CASNC
# ---------------------------------------------------------------------------

CASNC_HTML = """
<html><body>
  <a href="/communities/stone-creek-village/">Stone Creek Village</a>
  <a href="/communities/the-village-at-buck/">The Village at Buck</a>
  <a href="/communities/">Communities (listing, skip)</a>
  <a href="/about/">About</a>
</body></html>
"""


def test_casnc_yields_leads():
    session = _make_session({"https://casnc.com/communities/": CASNC_HTML})
    leads = list(casnc_leads(session=session))
    assert len(leads) == 2
    names = {l.name for l in leads}
    assert "Stone Creek Village" in names
    assert "The Village at Buck" in names
    for l in leads:
        assert l.state == "NC"
        assert l.source == "casnc"
        assert l.website.startswith("https://casnc.com/communities/")


def test_casnc_handles_network_error():
    session = MagicMock(spec=requests.Session)
    session.headers = MagicMock()
    session.headers.update = MagicMock()
    session.get.side_effect = requests.ConnectionError("refused")
    leads = list(casnc_leads(session=session))
    assert leads == []


# ---------------------------------------------------------------------------
# Seaside OBX
# ---------------------------------------------------------------------------

SEASIDE_HTML = """
<html><body>
  <a href="/myassociations/corolla-light/">Corolla Light</a>
  <a href="/associations/duck-dunes/">Duck Dunes</a>
  <a href="/myassociations/">Listing (skip)</a>
</body></html>
"""


def test_seaside_yields_leads():
    session = _make_session({
        "https://www.seaside-management.com/associations/": SEASIDE_HTML,
        "https://www.seaside-management.com/myassociations/": SEASIDE_HTML,
    })
    leads = list(seaside_leads(session=session))
    slugs = {l.source_url for l in leads}
    assert any("corolla-light" in u for u in slugs)
    assert any("duck-dunes" in u for u in slugs)
    # No duplicates despite two listing pages having same content
    names = [l.name for l in leads]
    assert len(names) == len(set(names))
    for l in leads:
        assert l.state == "NC"
        assert l.source == "seaside-obx"


# ---------------------------------------------------------------------------
# Closing Carolina
# ---------------------------------------------------------------------------

CC_HTML = """
<html><body>
  <h3>Mecklenburg County</h3>
  <ul>
    <li><a href="https://closingcarolina.com/wp-uploads/sunrise-estates.pdf">Sunrise Estates</a></li>
    <li><a href="https://closingcarolina.com/wp-uploads/pines-of-carolina.pdf">Pines of Carolina</a></li>
  </ul>
</body></html>
"""


def test_closing_carolina_yields_tuples():
    session = _make_session({"https://closingcarolina.com/covenants/": CC_HTML})
    results = list(closing_carolina_leads(session=session))
    assert len(results) == 2
    for lead, pdfs in results:
        assert isinstance(lead, Lead)
        assert lead.state == "NC"
        assert lead.source == "closing-carolina"
        assert len(pdfs) == 1
        assert pdfs[0].endswith(".pdf")
        assert lead.website is None


def test_closing_carolina_deduplicates():
    # Same PDF URL appearing twice in the page
    html = """
    <html><body>
      <a href="https://closingcarolina.com/wp-uploads/foo.pdf">Foo HOA</a>
      <a href="https://closingcarolina.com/wp-uploads/foo.pdf">Foo HOA (dup)</a>
    </body></html>
    """
    session = _make_session({"https://closingcarolina.com/covenants/": html})
    results = list(closing_carolina_leads(session=session))
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Triad
# ---------------------------------------------------------------------------

TRIAD_HTML = """
<html><body>
  <h3>Oak Forest HOA</h3>
  <p><a href="https://img1.wsimg.com/blobby/go/oak-forest-covenants.pdf">Covenants</a></p>
  <p><a href="https://img1.wsimg.com/blobby/go/oak-forest-bylaws.pdf">Bylaws</a></p>
  <h3>Sunset Ridge Association</h3>
  <p><a href="https://img1.wsimg.com/blobby/go/sunset-ridge.pdf">Declaration</a></p>
</body></html>
"""


def test_triad_yields_tuples():
    session = _make_session({"https://triadcommunitymanagement.com/forms": TRIAD_HTML})
    results = list(triad_leads(session=session))
    # Should get 2 HOAs with their PDFs
    assert len(results) == 2
    oak = next(r for r in results if "Oak Forest" in r[0].name)
    assert len(oak[1]) == 2
    sunset = next(r for r in results if "Sunset Ridge" in r[0].name)
    assert len(sunset[1]) == 1
    for lead, pdfs in results:
        assert lead.state == "NC"
        assert lead.source == "triad-mgmt"


# ---------------------------------------------------------------------------
# Signature Management
# ---------------------------------------------------------------------------

SIG_HTML = """
<html><body>
  <a href="/communities/hampton-crossing/">Hampton Crossing</a>
  <a href="/communities/meadow-ridge/">Meadow Ridge</a>
  <a href="/communities/">All Communities (skip)</a>
  <a href="/about/">About</a>
</body></html>
"""


def test_signature_yields_leads():
    session = _make_session({"https://signaturemgt.com/communities/": SIG_HTML})
    leads = list(signature_leads(session=session))
    assert len(leads) == 2
    for l in leads:
        assert l.state == "NC"
        assert l.county == "Johnston"
        assert l.source == "signaturemgt"


def test_signature_skips_commercial():
    html = """
    <html><body>
      <a href="/communities/real-hoa/">Real HOA</a>
      <a href="/communities/business-park/">North Office Park</a>
    </body></html>
    """
    session = _make_session({"https://signaturemgt.com/communities/": html})
    leads = list(signature_leads(session=session))
    names = [l.name for l in leads]
    assert "Real HOA" in names
    assert not any("Park" in n for n in names)


# ---------------------------------------------------------------------------
# BAM Wilmington
# ---------------------------------------------------------------------------

BAM_HTML = """
<html><body>
  <a href="/communities/anchor-cove/">Anchor Cove</a>
  <a href="/communities/marina-commons/">Marina Commons</a>
  <a href="/communities/magnolia-office-park/">Magnolia Office Park</a>
  <a href="/communities/">All (skip)</a>
</body></html>
"""


def test_bam_skips_marina_and_office_park():
    session = _make_session({"https://bamgt.com/communities/": BAM_HTML})
    leads = list(bam_leads(session=session))
    names = [l.name for l in leads]
    assert "Anchor Cove" in names
    assert "Marina Commons" not in names  # "marina" in skip pattern
    assert "Magnolia Office Park" not in names


# ---------------------------------------------------------------------------
# nc_leads combined iterator
# ---------------------------------------------------------------------------

def test_nc_leads_all_sources_key_set():
    assert "casnc" in ALL_SOURCES
    assert "closing-carolina" in ALL_SOURCES
    assert "seaside" in ALL_SOURCES
    assert "triad" in ALL_SOURCES
    assert "signature" in ALL_SOURCES


def test_nc_leads_subset():
    """nc_leads with specific source only calls that source."""
    # Patch both source dicts to verify only casnc is called
    mock_casnc = MagicMock(return_value=iter([
        Lead(name="Test HOA", source="casnc", source_url="https://casnc.com/communities/test/", state="NC")
    ]))
    mock_seaside = MagicMock(return_value=iter([]))

    with patch.dict("hoaware.discovery.sources.nc_aggregators._WEBSITE_SOURCES",
                    {"casnc": mock_casnc, "seaside": mock_seaside}), \
         patch.dict("hoaware.discovery.sources.nc_aggregators._DIRECT_PDF_SOURCES", {}):
        leads = list(nc_leads(sources=["casnc"]))

    assert len(leads) == 1
    mock_casnc.assert_called_once()
    mock_seaside.assert_not_called()


def test_nc_leads_direct_pdf_source_yields_lead_only():
    """nc_leads() unwraps (Lead, pdfs) tuples from direct-PDF sources."""
    mock_fn = MagicMock(return_value=iter([
        (Lead(name="Sunrise Estates", source="closing-carolina",
              source_url="https://example.com/foo.pdf", state="NC"), ["https://example.com/foo.pdf"])
    ]))

    with patch.dict("hoaware.discovery.sources.nc_aggregators._DIRECT_PDF_SOURCES",
                    {"closing-carolina": mock_fn}), \
         patch.dict("hoaware.discovery.sources.nc_aggregators._WEBSITE_SOURCES", {}):
        leads = list(nc_leads(sources=["closing-carolina"]))

    assert len(leads) == 1
    assert isinstance(leads[0], Lead)


def test_nc_leads_with_pdfs_website_source_yields_empty_pdf_list():
    mock_fn = MagicMock(return_value=iter([
        Lead(name="Stone Creek", source="casnc",
             source_url="https://casnc.com/communities/stone-creek/", state="NC")
    ]))

    with patch.dict("hoaware.discovery.sources.nc_aggregators._WEBSITE_SOURCES",
                    {"casnc": mock_fn}), \
         patch.dict("hoaware.discovery.sources.nc_aggregators._DIRECT_PDF_SOURCES", {}):
        results = list(nc_leads_with_pdfs(sources=["casnc"]))

    assert len(results) == 1
    lead, pdfs = results[0]
    assert isinstance(lead, Lead)
    assert pdfs == []


# ---------------------------------------------------------------------------
# CLI scrape-leads command
# ---------------------------------------------------------------------------

def test_cli_scrape_leads_nc(capsys, tmp_path):
    """scrape-leads nc writes JSONL with valid Lead fields."""
    from hoaware.discovery.__main__ import main

    mock_leads = [
        Lead(name="Test HOA", source="casnc",
             source_url="https://casnc.com/communities/test/", state="NC"),
    ]

    with patch("hoaware.discovery.__main__.nc_leads", return_value=iter(mock_leads)):
        rc = main(["scrape-leads", "nc"])

    assert rc == 0
    captured = capsys.readouterr()
    lines = [l for l in captured.out.strip().split("\n") if l]
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["name"] == "Test HOA"
    assert obj["state"] == "NC"
    assert obj["source"] == "casnc"


def test_cli_scrape_leads_nc_to_file(tmp_path):
    from hoaware.discovery.__main__ import main

    out_file = str(tmp_path / "leads.jsonl")
    mock_leads = [
        Lead(name="HOA One", source="casnc",
             source_url="https://casnc.com/communities/one/", state="NC"),
        Lead(name="HOA Two", source="seaside",
             source_url="https://seaside.com/two/", state="NC"),
    ]

    with patch("hoaware.discovery.__main__.nc_leads", return_value=iter(mock_leads)):
        rc = main(["scrape-leads", "nc", "--output", out_file])

    assert rc == 0
    with open(out_file) as f:
        lines = [l.strip() for l in f if l.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["name"] == "HOA One"
    assert json.loads(lines[1])["name"] == "HOA Two"


# ---------------------------------------------------------------------------
# probe() pre_discovered_pdf_urls integration
# ---------------------------------------------------------------------------

def test_probe_pre_discovered_pdf_urls_seeded():
    """pre_discovered_pdf_urls are seeded into candidates before homepage harvest."""
    from hoaware.discovery.probe import probe
    from hoaware.discovery.leads import Lead

    lead = Lead(
        name="Sunrise Estates",
        source="closing-carolina",
        source_url="https://closingcarolina.com/wp-uploads/sunrise.pdf",
        state="NC",
        website=None,
    )

    banked_docs = []

    def _fake_bank(**kwargs):
        banked_docs.extend(kwargs.get("documents", []))
        return "gs://hoaproxy-bank/v1/NC/test/sunrise-estates/manifest.json"

    PDF_BYTES = b"%PDF-1.4 fake pdf content for testing"

    def _fake_get(url, **kwargs):
        r = MagicMock(spec=requests.Response)
        if url.endswith(".pdf"):
            r.status_code = 200
            r.headers = {"Content-Type": "application/pdf"}
            r.iter_content.return_value = [PDF_BYTES]
            r.close = MagicMock()
        else:
            r.status_code = 404
        return r

    mock_session = MagicMock(spec=requests.Session)
    mock_session.head.return_value = MagicMock(
        status_code=200, headers={"Content-Length": str(len(PDF_BYTES))}
    )
    mock_session.get.side_effect = _fake_get

    with patch("hoaware.discovery.probe.bank_hoa", side_effect=_fake_bank):
        result = probe(
            lead,
            http_session=mock_session,
            pre_discovered_pdf_urls=["https://closingcarolina.com/wp-uploads/sunrise.pdf"],
        )

    assert result.documents_banked == 1
