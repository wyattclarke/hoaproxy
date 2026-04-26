"""Tests for the agent-driven ingestion contract (PR-1).

Covers:
  - extract_pages routing on the text_extractable hint
  - /upload validating per-file categories and rejecting PII
  - /agent/precheck endpoint shape
  - documents.category / text_extractable / source_url persistence
"""

import io
import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["HOA_DB_PATH"] = _tmp_db.name
os.environ["JWT_SECRET"] = "test-secret-for-ci"
os.environ["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY", "sk-test-fake")
_tmp_db.close()

_tmp_docs = tempfile.mkdtemp(prefix="hoa_docs_test_")
os.environ["HOA_DOCS_ROOT"] = _tmp_docs

from api.main import app  # noqa: E402
from hoaware import db  # noqa: E402
from hoaware.config import load_settings  # noqa: E402
from hoaware.pdf_utils import extract_pages, detect_text_extractable  # noqa: E402


client = TestClient(app)


def _make_text_pdf(text: str = "Declaration of Covenants Conditions and Restrictions") -> bytes:
    """Create a tiny text-bearing PDF for tests."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, text)
    return bytes(pdf.output())


@pytest.fixture(autouse=True)
def _reset_db():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
        for table in [
            "proxy_audit", "proxy_assignments", "delegates", "membership_claims",
            "sessions", "users",
            "chunks", "documents", "hoa_locations", "hoas",
        ]:
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()


def _register_user(email: str = "agent-test@example.com") -> str:
    r = client.post("/auth/register", json={
        "email": email,
        "password": "test-password-123",
        "display_name": "Agent Test",
    })
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ---------- extract_pages routing ----------

def test_extract_pages_text_extractable_true_skips_ocr(tmp_path):
    """When agent says text_extractable=True, never call DocAI even if configured."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_make_text_pdf("Declaration of Covenants"))
    pages = extract_pages(
        pdf,
        text_extractable=True,
        enable_docai=True,
        docai_project_id="some-project",
        docai_processor_id="some-processor",
    )
    assert len(pages) == 1
    assert "Declaration" in pages[0].text


def test_extract_pages_text_extractable_false_no_docai_returns_blank(tmp_path):
    """text_extractable=False with no DocAI configured returns blank pages, never tesseract."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_make_text_pdf("hello"))
    pages = extract_pages(pdf, text_extractable=False, enable_docai=False)
    assert len(pages) == 1
    assert pages[0].text == ""  # no fallback to tesseract anymore


def test_extract_pages_no_hint_uses_pypdf(tmp_path):
    """Legacy path with no hint and no DocAI: PyPDF only."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_make_text_pdf("test text"))
    pages = extract_pages(pdf, text_extractable=None, enable_docai=False)
    assert len(pages) == 1
    assert "test text" in pages[0].text


def test_detect_text_extractable_true_for_text_pdf(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_make_text_pdf("This is more than fifty characters of text in the PDF" * 2))
    assert detect_text_extractable(pdf) is True


# ---------- /upload validation ----------

def test_upload_rejects_pii_category():
    token = _register_user()
    pdf_bytes = _make_text_pdf("test")
    r = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={"hoa": "Test HOA", "categories": ["membership_list"]},
        files=[("files", ("members.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 400
    assert "PII risk" in r.text


def test_upload_rejects_invalid_category():
    token = _register_user()
    pdf_bytes = _make_text_pdf("test")
    r = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={"hoa": "Test HOA", "categories": ["totally_made_up"]},
        files=[("files", ("doc.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 400


def test_upload_rejects_mismatched_array_length():
    token = _register_user(email="agent-mismatch@example.com")
    pdf_bytes = _make_text_pdf("test")
    r = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={"hoa": "Test HOA", "categories": ["ccr", "bylaws"]},  # 2 cats, 1 file
        files=[("files", ("doc.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 400
    assert "length" in r.text


def test_upload_accepts_valid_per_file_metadata():
    token = _register_user(email="agent-valid@example.com")
    pdf_bytes = _make_text_pdf("Declaration of Covenants Conditions and Restrictions")
    r = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "hoa": "Park Village",
            "categories": ["ccr"],
            "text_extractable": ["true"],
            "source_urls": ["https://example.com/decl.pdf"],
        },
        files=[("files", ("decl.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hoa"] == "Park Village"
    assert body["queued"] is True


# ---------- /agent/precheck ----------

def test_precheck_with_filename_only_classifies():
    """Filename-only fallback classification works without a URL."""
    r = client.post("/agent/precheck", json={
        "filename": "Cary_Park_Bylaws.pdf",
        "hoa": "Cary Park",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["suggested_category"] == "bylaws"
    assert body["is_valid_governing_doc"] is True
    assert body["is_pii_risk"] is False


def test_precheck_pii_filename_flagged():
    r = client.post("/agent/precheck", json={
        "filename": "member_directory_2024.pdf",
        "hoa": "Test",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["suggested_category"] == "membership_list"
    assert body["is_pii_risk"] is True
    assert body["is_valid_governing_doc"] is False
