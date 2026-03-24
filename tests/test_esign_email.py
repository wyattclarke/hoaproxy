"""Tests for Milestone 6: Real E-Signatures & Email Delivery."""

from __future__ import annotations

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
# Click-to-sign
# ---------------------------------------------------------------------------

def test_click_to_sign():
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
        settings.email_from = "noreply@hoaproxy.org"
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
        settings.email_from = "noreply@hoaproxy.org"
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


