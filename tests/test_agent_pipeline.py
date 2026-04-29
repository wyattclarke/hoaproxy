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
from hoaware.docai import OCRFailedError  # noqa: E402
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
            "api_usage_log",
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


def test_extract_pages_text_extractable_false_no_docai_raises(tmp_path):
    """text_extractable=False with no DocAI configured must fail loudly:
    raise OCRFailedError instead of silently returning blank pages.
    Returning blank caused 2k+ docs to be persisted as 0-chunk 'success' on prod.
    """
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_make_text_pdf("hello"))
    with pytest.raises(OCRFailedError) as excinfo:
        extract_pages(pdf, text_extractable=False, enable_docai=False)
    assert excinfo.value.reason == "not_configured"


def test_ingest_records_hidden_doc_on_ocr_failure(tmp_path, monkeypatch):
    """When OCR fails, ingest must persist the document with hidden_reason
    set so it shows up in admin tooling — never as a silent 0-chunk success.
    Also bumps the failed counter."""
    from hoaware import ingest as ingest_mod
    from hoaware.config import load_settings as _load
    from hoaware.docai import OCRFailedError as _OCRFailedError

    settings = _load()
    docs_root = tmp_path / "docs"
    hoa_dir = docs_root / "Failing HOA"
    hoa_dir.mkdir(parents=True)
    pdf_path = hoa_dir / "scanned.pdf"
    pdf_path.write_bytes(_make_text_pdf("placeholder"))
    monkeypatch.setattr(settings, "docs_root", docs_root)

    def _fake_extract(*args, **kwargs):
        raise _OCRFailedError("docai_failed", "simulated DocAI outage")
    monkeypatch.setattr(ingest_mod, "extract_pages", _fake_extract)

    stats = ingest_mod.ingest_pdf_paths(
        "Failing HOA",
        [pdf_path],
        settings=settings,
        metadata_by_path={pdf_path: {"category": "ccr", "text_extractable": False}},
    )
    assert stats.failed == 1
    assert stats.indexed == 0

    with db.get_connection(settings.db_path) as conn:
        row = conn.execute(
            "SELECT hidden_reason FROM documents WHERE relative_path = ?",
            ("Failing HOA/scanned.pdf",),
        ).fetchone()
        assert row is not None, "document row must be persisted on OCR failure"
        assert row["hidden_reason"] == "ocr_failed:docai_failed"


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


# ---------- daily DocAI budget guard (PR-6) ----------

def test_daily_docai_budget_blocks_when_exhausted(monkeypatch):
    """If 24h DocAI spend has already burned the cap, /upload returns 429."""
    from api import main as api_main

    # Force a tiny budget so any projection trips the limit
    monkeypatch.setattr(api_main, "DAILY_DOCAI_BUDGET_USD", 0.01)

    # Pretend recent spend is already $5
    def fake_recent(conn, service, *, hours=24):
        return 5.0 if service == "docai" else 0.0
    monkeypatch.setattr(db, "get_recent_service_cost_usd", fake_recent)

    token = _register_user(email="agent-budget@example.com")
    pdf_bytes = _make_text_pdf("scanned doc fake")
    r = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "hoa": "Budget Test HOA",
            "categories": ["ccr"],
            "text_extractable": ["false"],  # forces DocAI projection > 0
        },
        files=[("files", ("decl.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 429
    assert "budget" in r.text.lower()


# ---------- pre-extracted-text sidecar (offload OCR to local agent) ----------

def test_upload_sidecar_skips_server_extraction(monkeypatch):
    """Sidecar of pre-extracted pages bypasses server extract_pages entirely."""
    import json as _json
    from hoaware import ingest as ingest_mod
    from hoaware import pdf_utils

    # If anything calls extract_pages on the server, fail loudly.
    def _boom(*args, **kwargs):
        raise AssertionError("extract_pages must not run when sidecar is present")
    monkeypatch.setattr(pdf_utils, "extract_pages", _boom)
    monkeypatch.setattr(ingest_mod, "extract_pages", _boom)

    # Stub embeddings + Qdrant so the test doesn't hit the network.
    captured: dict = {}
    def _fake_embeddings(texts, *, client, model):
        captured["chunk_count"] = len(texts)
        return [[0.0] * 1536 for _ in texts]
    monkeypatch.setattr(ingest_mod, "batch_embeddings", _fake_embeddings)

    class _StubQdrant:
        pass
    monkeypatch.setattr(ingest_mod, "build_client", lambda *a, **kw: _StubQdrant())
    monkeypatch.setattr(ingest_mod, "ensure_collection", lambda *a, **kw: None)
    monkeypatch.setattr(ingest_mod, "upsert_chunks", lambda *a, **kw: ["pid"] * len(a[2]))
    monkeypatch.setattr(ingest_mod, "delete_points", lambda *a, **kw: None)
    monkeypatch.setattr(ingest_mod, "points_exist", lambda *a, **kw: False)

    token = _register_user(email="agent-sidecar@example.com")
    pdf_bytes = _make_text_pdf("ignored - sidecar wins")
    sidecar = _json.dumps({
        "pages": [
            {"number": 1, "text": "Article I. The agent ran OCR locally and shipped this text."},
            {"number": 2, "text": "Section 1. Assessments."},
        ],
        "docai_pages": 2,
    })
    r = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "hoa": "Sidecar Estates",
            "categories": ["ccr"],
            "text_extractable": ["false"],  # would normally trigger DocAI
            "extracted_texts": [sidecar],
        },
        files=[("files", ("decl.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 200, r.text

    # Background task runs synchronously in TestClient — chunks should exist.
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT chunks.text FROM chunks "
            "JOIN documents ON documents.id = chunks.document_id "
            "JOIN hoas ON hoas.id = documents.hoa_id "
            "WHERE hoas.name = ?",
            ("Sidecar Estates",),
        ).fetchall()
    assert rows, "expected chunks from sidecar pages"
    joined = "\n".join(row["text"] for row in rows)
    assert "agent ran OCR locally" in joined
    assert captured.get("chunk_count", 0) > 0


def test_upload_sidecar_logs_docai_pages_to_cost_tracker(monkeypatch):
    """docai_pages from a sidecar should be logged so the rolling 24h cap stays honest."""
    import json as _json
    from hoaware import ingest as ingest_mod
    from hoaware import pdf_utils

    monkeypatch.setattr(pdf_utils, "extract_pages", lambda *a, **kw: [])
    monkeypatch.setattr(ingest_mod, "extract_pages", lambda *a, **kw: [])
    monkeypatch.setattr(ingest_mod, "batch_embeddings",
                        lambda texts, *, client, model: [[0.0] * 1536 for _ in texts])
    monkeypatch.setattr(ingest_mod, "build_client", lambda *a, **kw: object())
    monkeypatch.setattr(ingest_mod, "ensure_collection", lambda *a, **kw: None)
    monkeypatch.setattr(ingest_mod, "upsert_chunks", lambda *a, **kw: ["pid"] * len(a[2]))
    monkeypatch.setattr(ingest_mod, "delete_points", lambda *a, **kw: None)
    monkeypatch.setattr(ingest_mod, "points_exist", lambda *a, **kw: False)

    token = _register_user(email="agent-sidecar-log@example.com")
    pdf_bytes = _make_text_pdf("doesn't matter")
    sidecar = _json.dumps({
        "pages": [{"number": 1, "text": "x" * 200}],
        "docai_pages": 7,
    })
    r = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "hoa": "Sidecar Logged HOA",
            "categories": ["ccr"],
            "text_extractable": ["false"],
            "extracted_texts": [sidecar],
        },
        files=[("files", ("d.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 200, r.text

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        row = conn.execute(
            "SELECT SUM(units) AS pages FROM api_usage_log WHERE service = 'docai'"
        ).fetchone()
    assert row["pages"] == 7


def test_upload_sidecar_respects_budget_cap(monkeypatch):
    """Even via sidecar, exceeding the 24h DocAI cap blocks the upload."""
    import json as _json
    from api import main as api_main

    monkeypatch.setattr(api_main, "DAILY_DOCAI_BUDGET_USD", 0.005)  # ~3 pages worth

    # Pretend the local agent already burned $4 of DocAI in the last 24h.
    def fake_recent(conn, service, *, hours=24):
        return 4.0 if service == "docai" else 0.0
    monkeypatch.setattr(db, "get_recent_service_cost_usd", fake_recent)

    token = _register_user(email="agent-sidecar-cap@example.com")
    pdf_bytes = _make_text_pdf("scanned-fake")
    # No sidecar — server projects DocAI for this file. Cap should trip.
    r = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "hoa": "Sidecar Cap HOA",
            "categories": ["ccr"],
            "text_extractable": ["false"],
        },
        files=[("files", ("d.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 429
    assert "budget" in r.text.lower()


def test_upload_sidecar_length_mismatch_400():
    token = _register_user(email="agent-sidecar-len@example.com")
    pdf_bytes = _make_text_pdf("x")
    r = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "hoa": "Mismatch HOA",
            "categories": ["ccr"],
            "extracted_texts": ['{"pages":[{"number":1,"text":"a"}]}', '{"pages":[{"number":1,"text":"b"}]}'],
        },
        files=[("files", ("d.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 400
    assert "length" in r.text.lower()


def test_upload_sidecar_invalid_json_400():
    token = _register_user(email="agent-sidecar-json@example.com")
    pdf_bytes = _make_text_pdf("x")
    r = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "hoa": "Bad JSON HOA",
            "categories": ["ccr"],
            "extracted_texts": ["not-json{{"],
        },
        files=[("files", ("d.pdf", pdf_bytes, "application/pdf"))],
    )
    assert r.status_code == 400
    assert "json" in r.text.lower()


# ---------- daily DocAI alert endpoint ----------

def test_docai_alert_endpoint_under_threshold(monkeypatch):
    """The cost-alert endpoint reports under-threshold when spend is low."""
    def fake_recent(conn, service, *, hours=24):
        return 0.42
    monkeypatch.setattr(db, "get_recent_service_cost_usd", fake_recent)

    r = client.get(
        "/admin/costs/docai-alert?threshold_usd=10",
        headers={"Authorization": "Bearer test-secret-for-ci"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["over_threshold"] is False
    assert body["spend_usd"] == 0.42
    assert body["threshold_usd"] == 10.0


def test_docai_alert_endpoint_over_threshold(monkeypatch):
    def fake_recent(conn, service, *, hours=24):
        return 99.0
    monkeypatch.setattr(db, "get_recent_service_cost_usd", fake_recent)

    r = client.get(
        "/admin/costs/docai-alert?threshold_usd=10",
        headers={"Authorization": "Bearer test-secret-for-ci"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["over_threshold"] is True
    assert body["spend_usd"] == 99.0
