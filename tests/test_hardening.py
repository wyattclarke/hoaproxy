"""Tests for Milestone 8: Security hardening, legal pages, structured logging."""

from __future__ import annotations

import os
import tempfile
import time
from collections import defaultdict
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["HOA_DB_PATH"] = _tmp.name
os.environ["JWT_SECRET"] = "test-secret-hardening"
_tmp.close()

from api.main import app, _rate_buckets, _RATE_WINDOW  # noqa: E402
from hoaware import db  # noqa: E402
from hoaware.config import load_settings  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _setup_db():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
        for table in [
            "participation_records", "proxy_audit", "proxy_assignments",
            "delegates", "membership_claims", "sessions", "users",
        ]:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    # Clear rate buckets between tests so tests don't interfere
    _rate_buckets.clear()
    yield
    _rate_buckets.clear()


# ---------------------------------------------------------------------------
# Rate limiter tests
# ---------------------------------------------------------------------------

def test_rate_limiter_returns_429_after_limit():
    """Rate limiter blocks requests after limit from a non-testclient IP."""
    from api.main import _check_rate_limit, _RATE_LIMIT

    # Create a mock request with a real-looking IP
    class FakeClient:
        host = "1.2.3.4"

    class FakeRequest:
        client = FakeClient()

    req = FakeRequest()

    # Fill up the bucket exactly to the limit
    for _ in range(_RATE_LIMIT):
        _check_rate_limit(req, limit=_RATE_LIMIT)

    # Next call should raise 429
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        _check_rate_limit(req, limit=_RATE_LIMIT)
    assert exc_info.value.status_code == 429


def test_rate_limiter_skips_testclient():
    """TestClient requests are always allowed (skip rate limiting)."""
    from api.main import _check_rate_limit

    class FakeClient:
        host = "testclient"

    class FakeRequest:
        client = FakeClient()

    req = FakeRequest()
    # Should never raise, even called many times
    for _ in range(100):
        _check_rate_limit(req, limit=5)


# ---------------------------------------------------------------------------
# Legal pages
# ---------------------------------------------------------------------------

def test_terms_page_returns_200():
    resp = client.get("/terms")
    assert resp.status_code == 200
    assert "Terms of Service" in resp.text


def test_privacy_page_returns_200():
    resp = client.get("/privacy")
    assert resp.status_code == 200
    assert "Privacy Policy" in resp.text


def test_registration_page_contains_tos():
    resp = client.get("/register")
    assert resp.status_code == 200
    assert "Terms of Service" in resp.text


def test_terms_page_has_not_legal_advice_notice():
    resp = client.get("/terms")
    assert resp.status_code == 200
    assert "NOT LEGAL ADVICE" in resp.text.upper() or "not legal advice" in resp.text.lower()


def test_privacy_page_has_retention_info():
    resp = client.get("/privacy")
    assert resp.status_code == 200
    assert "90 days" in resp.text


# ---------------------------------------------------------------------------
# Proxy form "not legal advice" notice
# ---------------------------------------------------------------------------

def test_proxy_form_contains_not_legal_advice():
    """The proxy base template must contain a 'not legal advice' disclaimer."""
    from hoaware.proxy_templates import render_proxy_form
    html = render_proxy_form("CA", "hoa")
    assert "not constitute legal advice" in html.lower() or "not legal advice" in html.lower()


# ---------------------------------------------------------------------------
# Data expiry sweep
# ---------------------------------------------------------------------------

def test_expiry_sweep_marks_expired_assignments():
    """Proxy assignments with past expires_at should be marked 'expired' by sweep."""
    from api.main import _run_expiry_sweep

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        # Create minimal user and HOA records
        cur = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            ("sweep_grantor@example.com", "x"),
        )
        grantor_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            ("sweep_delegate@example.com", "x"),
        )
        delegate_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO hoas (name) VALUES (?)",
            ("Sweep Test HOA",),
        )
        hoa_id = cur.lastrowid

        # Insert a proxy assignment with expires_at in the past
        cur = conn.execute(
            """
            INSERT INTO proxy_assignments
            (grantor_user_id, delegate_user_id, hoa_id, jurisdiction, community_type,
             direction, status, expires_at)
            VALUES (?, ?, ?, 'CA', 'hoa', 'directed', 'draft', '2000-01-01')
            """,
            (grantor_id, delegate_id, hoa_id),
        )
        proxy_id = cur.lastrowid
        conn.commit()

    # Run the expiry sweep
    _run_expiry_sweep()

    # Verify the status was updated to 'expired'
    with db.get_connection(settings.db_path) as conn:
        row = conn.execute(
            "SELECT status FROM proxy_assignments WHERE id = ?", (proxy_id,)
        ).fetchone()

    assert row is not None
    assert row["status"] == "expired"


def test_expiry_sweep_does_not_touch_terminal_statuses():
    """Assignments already in a terminal status should not be changed."""
    from api.main import _run_expiry_sweep

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            ("sweep2_grantor@example.com", "x"),
        )
        grantor_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            ("sweep2_delegate@example.com", "x"),
        )
        delegate_id = cur.lastrowid
        cur = conn.execute("INSERT INTO hoas (name) VALUES (?)", ("Sweep Test HOA 2",))
        hoa_id = cur.lastrowid

        cur = conn.execute(
            """
            INSERT INTO proxy_assignments
            (grantor_user_id, delegate_user_id, hoa_id, jurisdiction, community_type,
             direction, status, expires_at)
            VALUES (?, ?, ?, 'CA', 'hoa', 'directed', 'revoked', '2000-01-01')
            """,
            (grantor_id, delegate_id, hoa_id),
        )
        proxy_id = cur.lastrowid
        conn.commit()

    _run_expiry_sweep()

    with db.get_connection(settings.db_path) as conn:
        row = conn.execute(
            "SELECT status FROM proxy_assignments WHERE id = ?", (proxy_id,)
        ).fetchone()

    assert row["status"] == "revoked"  # unchanged


# ---------------------------------------------------------------------------
# Config: PROXY_RETENTION_DAYS
# ---------------------------------------------------------------------------

def test_proxy_retention_days_default():
    settings = load_settings()
    assert settings.proxy_retention_days == 90


def test_proxy_retention_days_env(monkeypatch):
    monkeypatch.setenv("PROXY_RETENTION_DAYS", "30")
    settings = load_settings()
    assert settings.proxy_retention_days == 30
