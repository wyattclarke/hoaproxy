"""Tests for Milestone 6: Real E-Signatures & Email Delivery."""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["HOA_DB_PATH"] = _tmp.name
os.environ["JWT_SECRET"] = "test-secret-for-ci"
os.environ["EMAIL_PROVIDER"] = "stub"
_tmp.close()

from api.main import app  # noqa: E402
from hoaware import db  # noqa: E402
from hoaware.config import load_settings  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _setup_db():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
        for table in ["proxy_audit", "proxy_assignments", "delegates", "membership_claims", "sessions", "users"]:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_users_and_hoa():
    grantor_reg = client.post("/auth/register", json={
        "email": "grantor@example.com", "password": "password1234", "display_name": "Grantor Owner",
    }).json()
    grantor_headers = {"Authorization": f"Bearer {grantor_reg['token']}"}

    delegate_reg = client.post("/auth/register", json={
        "email": "delegate@example.com", "password": "password1234", "display_name": "Delegate Holder",
    }).json()
    delegate_headers = {"Authorization": f"Bearer {delegate_reg['token']}"}

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, "Test HOA M6")
        db.create_membership_claim(conn, user_id=grantor_reg["user_id"], hoa_id=hoa_id)
        db.create_membership_claim(conn, user_id=delegate_reg["user_id"], hoa_id=hoa_id)
        db.create_delegate(conn, user_id=delegate_reg["user_id"], hoa_id=hoa_id, bio="Test delegate")
        db.mark_user_verified(conn, grantor_reg["user_id"])

    return grantor_headers, delegate_headers, hoa_id, delegate_reg["user_id"]


def _create_proxy(grantor_headers, hoa_id, delegate_user_id):
    resp = client.post("/proxies", headers=grantor_headers, json={
        "hoa_id": hoa_id,
        "delegate_user_id": delegate_user_id,
        "direction": "undirected",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# DB migration tests
# ---------------------------------------------------------------------------

def test_board_email_column_exists():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        cur = conn.execute("PRAGMA table_info(hoas)")
        cols = {row["name"] for row in cur.fetchall()}
    assert "board_email" in cols


def test_documenso_columns_exist():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        cur = conn.execute("PRAGMA table_info(proxy_assignments)")
        cols = {row["name"] for row in cur.fetchall()}
    assert "documenso_document_id" in cols
    assert "documenso_signing_url" in cols


# ---------------------------------------------------------------------------
# Board email endpoint
# ---------------------------------------------------------------------------

def test_set_board_email():
    grantor_headers, _, hoa_id, _ = _setup_users_and_hoa()
    resp = client.patch(f"/hoas/{hoa_id}/board-email", headers=grantor_headers,
                        json={"board_email": "board@testhoa.org"})
    assert resp.status_code == 200
    assert resp.json()["board_email"] == "board@testhoa.org"

    # Verify persisted
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        row = conn.execute("SELECT board_email FROM hoas WHERE id = ?", (hoa_id,)).fetchone()
    assert row["board_email"] == "board@testhoa.org"


def test_set_board_email_requires_membership():
    # Register a user with no HOA membership
    outsider = client.post("/auth/register", json={
        "email": "outsider@example.com", "password": "password1234",
    }).json()
    outsider_headers = {"Authorization": f"Bearer {outsider['token']}"}
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, "Other HOA")
    resp = client.patch(f"/hoas/{hoa_id}/board-email", headers=outsider_headers,
                        json={"board_email": "board@other.org"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Click-to-sign (no Documenso configured)
# ---------------------------------------------------------------------------

def test_click_to_sign_without_documenso():
    """Without DOCUMENSO_API_KEY, sign endpoint records click-to-sign immediately."""
    grantor_headers, _, hoa_id, delegate_user_id = _setup_users_and_hoa()
    proxy = _create_proxy(grantor_headers, hoa_id, delegate_user_id)
    assert proxy["status"] == "draft"

    resp = client.post(f"/proxies/{proxy['id']}/sign", headers=grantor_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "signed"
    assert data["signed_at"] is not None


def test_sign_wrong_user_rejected():
    grantor_headers, delegate_headers, hoa_id, delegate_user_id = _setup_users_and_hoa()
    proxy = _create_proxy(grantor_headers, hoa_id, delegate_user_id)
    resp = client.post(f"/proxies/{proxy['id']}/sign", headers=delegate_headers)
    assert resp.status_code == 403


def test_sign_already_signed_rejected():
    grantor_headers, _, hoa_id, delegate_user_id = _setup_users_and_hoa()
    proxy = _create_proxy(grantor_headers, hoa_id, delegate_user_id)
    client.post(f"/proxies/{proxy['id']}/sign", headers=grantor_headers)
    resp = client.post(f"/proxies/{proxy['id']}/sign", headers=grantor_headers)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Documenso path (mocked)
# ---------------------------------------------------------------------------

def test_sign_with_documenso_mocked():
    """When DOCUMENSO_API_KEY is set, sign endpoint calls Documenso and returns signing_url."""
    grantor_headers, _, hoa_id, delegate_user_id = _setup_users_and_hoa()
    proxy = _create_proxy(grantor_headers, hoa_id, delegate_user_id)

    mock_result = {
        "method": "documenso",
        "document_id": "doc-abc-123",
        "signing_url": "https://app.documenso.com/sign/abc123",
    }

    with patch("hoaware.esign.create_signing_request", return_value=mock_result):
        with patch.dict(os.environ, {"DOCUMENSO_API_KEY": "test-key"}):
            # Reload settings so the key is picked up
            from importlib import reload
            import hoaware.config
            reload(hoaware.config)
            from hoaware.config import load_settings as _ls
            settings = _ls()

            # Patch the settings on the sign endpoint path
            with patch("api.main.load_settings", return_value=settings):
                # Manually set the api key on settings
                settings.documenso_api_key = "test-key"
                resp = client.post(f"/proxies/{proxy['id']}/sign", headers=grantor_headers)

    # Status stays draft (waiting for webhook), signing_url is set
    assert resp.status_code == 200
    data = resp.json()
    assert data["signing_url"] == "https://app.documenso.com/sign/abc123"


# ---------------------------------------------------------------------------
# Documenso webhook
# ---------------------------------------------------------------------------

def test_documenso_webhook_marks_proxy_signed():
    grantor_headers, _, hoa_id, delegate_user_id = _setup_users_and_hoa()
    proxy = _create_proxy(grantor_headers, hoa_id, delegate_user_id)
    proxy_id = proxy["id"]

    # Simulate webhook — no secret configured so signature check passes
    payload = json.dumps({
        "event": "document.completed",
        "data": {"externalId": str(proxy_id)},
    }).encode()

    resp = client.post(
        "/webhooks/documenso",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "signed"

    # Verify DB state
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        updated = db.get_proxy_assignment(conn, proxy_id)
    assert updated["status"] == "signed"
    assert updated["signed_at"] is not None


def test_documenso_webhook_idempotent():
    """Calling webhook twice for the same proxy is safe."""
    grantor_headers, _, hoa_id, delegate_user_id = _setup_users_and_hoa()
    proxy = _create_proxy(grantor_headers, hoa_id, delegate_user_id)
    proxy_id = proxy["id"]

    payload = json.dumps({
        "event": "document.completed",
        "data": {"externalId": str(proxy_id)},
    }).encode()

    r1 = client.post("/webhooks/documenso", content=payload,
                     headers={"Content-Type": "application/json"})
    assert r1.status_code == 200
    r2 = client.post("/webhooks/documenso", content=payload,
                     headers={"Content-Type": "application/json"})
    assert r2.status_code == 200
    assert r2.json()["action"] == "already_processed"


def test_documenso_webhook_unknown_event_ignored():
    payload = json.dumps({"event": "document.viewed", "data": {"externalId": "999"}}).encode()
    resp = client.post("/webhooks/documenso", content=payload,
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 200
    assert resp.json()["action"] == "ignored"


# ---------------------------------------------------------------------------
# Email delivery (stub mode)
# ---------------------------------------------------------------------------

def test_deliver_proxy_stub_no_board_email():
    """Delivery works in stub mode even without a board email set."""
    grantor_headers, _, hoa_id, delegate_user_id = _setup_users_and_hoa()
    proxy = _create_proxy(grantor_headers, hoa_id, delegate_user_id)
    proxy_id = proxy["id"]

    # Sign first
    client.post(f"/proxies/{proxy_id}/sign", headers=grantor_headers)

    resp = client.post(f"/proxies/{proxy_id}/deliver", headers=grantor_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"


def test_deliver_proxy_with_board_email_stub(caplog):
    """With board_email set and stub provider, logs the delivery."""
    import logging
    grantor_headers, _, hoa_id, delegate_user_id = _setup_users_and_hoa()

    # Set board email
    client.patch(f"/hoas/{hoa_id}/board-email", headers=grantor_headers,
                 json={"board_email": "board@testhoa.org"})

    proxy = _create_proxy(grantor_headers, hoa_id, delegate_user_id)
    proxy_id = proxy["id"]
    client.post(f"/proxies/{proxy_id}/sign", headers=grantor_headers)

    with caplog.at_level(logging.INFO, logger="hoaware.email_service"):
        resp = client.post(f"/proxies/{proxy_id}/deliver", headers=grantor_headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    # Stub logs the email
    assert any("EMAIL STUB" in r.message for r in caplog.records)


def test_send_email_resend_called():
    """When EMAIL_PROVIDER=resend and RESEND_API_KEY is set, resend.Emails.send is called."""
    from hoaware.email_service import _send_email

    with patch("hoaware.email_service.load_settings") as mock_settings:
        settings = MagicMock()
        settings.email_provider = "resend"
        settings.resend_api_key = "re_test_key"
        settings.email_from = "noreply@hoaware.app"
        mock_settings.return_value = settings

        with patch("hoaware.email_service._send_via_resend") as mock_resend:
            result = _send_email(
                to=["board@example.org"],
                subject="Test",
                html="<p>Hello</p>",
            )

    assert result is True
    mock_resend.assert_called_once()


def test_send_email_smtp_called():
    from hoaware.email_service import _send_email

    with patch("hoaware.email_service.load_settings") as mock_settings:
        settings = MagicMock()
        settings.email_provider = "smtp"
        settings.smtp_host = "smtp.example.com"
        settings.smtp_port = 587
        settings.smtp_user = "user"
        settings.smtp_password = "pass"
        settings.email_from = "noreply@hoaware.app"
        mock_settings.return_value = settings

        with patch("hoaware.email_service._send_via_smtp") as mock_smtp:
            result = _send_email(
                to=["board@example.org"],
                subject="Test",
                html="<p>Hello</p>",
            )

    assert result is True
    mock_smtp.assert_called_once()


# ---------------------------------------------------------------------------
# HTML → PDF conversion
# ---------------------------------------------------------------------------

def test_html_to_pdf_returns_bytes():
    from hoaware.esign import _html_to_pdf
    pdf = _html_to_pdf("<html><body><h1>Proxy Form</h1><p>Test content</p></body></html>")
    assert isinstance(pdf, bytes)
    assert pdf[:4] == b"%PDF"  # PDF magic bytes


def test_html_to_pdf_empty_input():
    from hoaware.esign import _html_to_pdf
    pdf = _html_to_pdf("")
    assert isinstance(pdf, bytes)
    assert len(pdf) > 0


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def test_webhook_signature_no_secret_passes():
    """Without DOCUMENSO_WEBHOOK_SECRET configured, verification is skipped."""
    from hoaware.esign import verify_webhook_signature
    with patch("hoaware.esign.load_settings") as mock_settings:
        settings = MagicMock()
        settings.documenso_webhook_secret = None
        mock_settings.return_value = settings
        assert verify_webhook_signature(b"payload", None) is True


def test_webhook_signature_valid():
    import hashlib
    import hmac as hmac_lib
    from hoaware.esign import verify_webhook_signature

    secret = "my-webhook-secret"
    payload = b'{"event": "document.completed"}'
    sig = "sha256=" + hmac_lib.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    with patch("hoaware.esign.load_settings") as mock_settings:
        settings = MagicMock()
        settings.documenso_webhook_secret = secret
        mock_settings.return_value = settings
        assert verify_webhook_signature(payload, sig) is True


def test_webhook_signature_invalid():
    from hoaware.esign import verify_webhook_signature
    with patch("hoaware.esign.load_settings") as mock_settings:
        settings = MagicMock()
        settings.documenso_webhook_secret = "real-secret"
        mock_settings.return_value = settings
        assert verify_webhook_signature(b"payload", "sha256=badhex") is False
