"""End-to-end probe tests using a stub HTTP server (no GCS, no real network)."""

import http.server
import socket
import socketserver
import threading
from unittest.mock import patch

import pytest

from hoaware.discovery import Lead
from hoaware.discovery.probe import probe


# ---------- Stub HTTP server ----------

# Minimal valid 1-page PDF (matches PDF_MAGIC and is well-formed enough for pypdf)
_MINIMAL_PDF = (
    b"%PDF-1.1\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f\n0000000010 00000 n\n"
    b"0000000050 00000 n\n0000000095 00000 n\n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n145\n%%EOF\n"
)


class StubHandler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}  # path -> (status, content_type, body)

    def _resolve(self):
        return self.routes.get(self.path)

    def do_GET(self):
        r = self._resolve()
        if not r:
            self.send_error(404)
            return
        status, ctype, body = r
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        r = self._resolve()
        if not r:
            self.send_error(404)
            return
        status, ctype, body = r
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def log_message(self, *_):  # silence
        pass


@pytest.fixture
def stub_server():
    """Start a stub HTTP server on a free port; yield (base_url, set_routes)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    StubHandler.routes = {}
    httpd = socketserver.TCPServer(("127.0.0.1", port), StubHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", lambda routes: StubHandler.routes.update(routes)
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------- Tests ----------

def _patch_bank(monkeypatch):
    """Replace bank_hoa with a recorder that returns a fake URI.

    Note: hoaware.discovery.probe is shadowed by the ``probe`` function in the
    package __init__, so we have to import the module via importlib.
    """
    import importlib
    probe_module = importlib.import_module("hoaware.discovery.probe")
    calls: list[dict] = []

    def fake_bank(**kwargs):
        calls.append(kwargs)
        return "gs://test/manifest.json"

    monkeypatch.setattr(probe_module, "bank_hoa", fake_bank)
    return calls


def test_probe_no_website_banks_stub(monkeypatch):
    calls = _patch_bank(monkeypatch)
    lead = Lead(name="Foo HOA", state="VA", source="test", source_url="x")
    result = probe(lead)
    assert result.documents_banked == 0
    assert result.homepage_fetched is False
    assert len(calls) == 1
    assert calls[0]["name"] == "Foo HOA"
    assert calls[0]["address"] == {"state": "VA"}


def test_probe_walled_lead_url_does_not_fetch(monkeypatch):
    """A lead pointing directly at a known walled platform shouldn't fetch."""
    calls = _patch_bank(monkeypatch)
    lead = Lead(
        name="Foo HOA",
        state="VA",
        website="https://portal.connectresident.com/foo",
        source="test",
        source_url="x",
    )
    result = probe(lead)
    assert result.is_walled is True
    assert result.documents_banked == 0
    assert calls[0]["website"]["is_walled"] is True


def test_probe_finds_pdf_and_invokes_bank(monkeypatch, stub_server):
    base_url, set_routes = stub_server
    set_routes({
        "/": (200, "text/html", b'<html><body>'
              b'<a href="/declaration.pdf">CC&Rs</a>'
              b'<a href="/about">About</a>'
              b'</body></html>'),
        "/declaration.pdf": (200, "application/pdf", _MINIMAL_PDF),
    })
    calls = _patch_bank(monkeypatch)
    lead = Lead(name="Test HOA", state="VA", website=base_url + "/", source="test", source_url="x")
    result = probe(lead)
    assert result.documents_banked == 1
    assert result.documents_skipped == 0
    assert calls[0]["documents"][0].source_url.endswith("/declaration.pdf")


def test_probe_tries_validated_docpage_url_as_pdf(monkeypatch, stub_server):
    base_url, set_routes = stub_server
    set_routes({
        "/DocumentCenter/View/123/Declaration": (200, "application/pdf", _MINIMAL_PDF),
    })
    calls = _patch_bank(monkeypatch)
    lead = Lead(
        name="Test HOA",
        state="VA",
        website=base_url + "/DocumentCenter/View/123/Declaration",
        source="search-serper-ks-docpages",
        source_url="x",
    )
    result = probe(lead)
    assert result.homepage_fetched is False
    assert result.documents_banked == 1
    assert calls[0]["documents"][0].source_url.endswith("/DocumentCenter/View/123/Declaration")


def test_probe_gets_pdf_when_head_404(monkeypatch, stub_server):
    base_url, set_routes = stub_server
    set_routes({
        "/DocumentCenter/View/404-head": (200, "application/pdf", _MINIMAL_PDF),
    })
    orig_do_head = StubHandler.do_HEAD

    def patched_head(self):
        if self.path == "/DocumentCenter/View/404-head":
            self.send_error(404)
        else:
            orig_do_head(self)

    StubHandler.do_HEAD = patched_head
    try:
        calls = _patch_bank(monkeypatch)
        lead = Lead(
            name="Test HOA",
            state="VA",
            website=base_url + "/DocumentCenter/View/404-head",
            source="search-serper-ks-docpages",
            source_url="x",
        )
        result = probe(lead)
        assert result.documents_banked == 1
        assert calls[0]["documents"][0].source_url.endswith("/DocumentCenter/View/404-head")
    finally:
        StubHandler.do_HEAD = orig_do_head


def test_probe_html_disguised_as_pdf_recorded_as_skipped(monkeypatch, stub_server):
    base_url, set_routes = stub_server
    set_routes({
        "/": (200, "text/html", b'<html><body><a href="/declaration.pdf">CC&Rs</a></body></html>'),
        "/declaration.pdf": (200, "text/html", b'<!DOCTYPE html><html><body>Login required</body></html>'),
    })
    calls = _patch_bank(monkeypatch)
    lead = Lead(name="Test HOA", state="VA", website=base_url + "/", source="test", source_url="x")
    result = probe(lead)
    assert result.documents_banked == 0
    assert result.documents_skipped == 1
    skipped = calls[0]["skipped_documents"][0]
    assert skipped["reason"] == "html_disguised_as_pdf"


def test_probe_oversize_pdf_skipped(monkeypatch, stub_server):
    """If Content-Length exceeds cap, mark skipped without downloading."""
    base_url, set_routes = stub_server

    class FakeBigHandler(StubHandler):
        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(60 * 1024 * 1024))  # > 50MB
            self.end_headers()

    set_routes({
        "/": (200, "text/html", b'<html><body><a href="/big.pdf">Big</a></body></html>'),
    })
    # Override handler for /big.pdf to send oversized HEAD
    orig_do_head = StubHandler.do_HEAD

    def patched_head(self):
        if self.path == "/big.pdf":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(60 * 1024 * 1024))
            self.end_headers()
        else:
            orig_do_head(self)

    StubHandler.do_HEAD = patched_head
    try:
        calls = _patch_bank(monkeypatch)
        lead = Lead(name="Test HOA", state="VA", website=base_url + "/", source="test", source_url="x")
        result = probe(lead)
        assert result.documents_banked == 0
        assert result.documents_skipped == 1
        assert calls[0]["skipped_documents"][0]["reason"].startswith("too_large")
    finally:
        StubHandler.do_HEAD = orig_do_head
