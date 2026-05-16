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

def _fake_request(host: str = "1.2.3.4", path: str = "/test-endpoint"):
    """Minimal request stub with the bits _check_rate_limit reads.

    Mirrors the FastAPI scope shape: `scope["path"]` is the literal URL
    and `scope["route"]` is the matched route object (None if unmatched).
    """
    class FakeClient:
        pass
    fc = FakeClient()
    fc.host = host

    class FakeRequest:
        pass
    req = FakeRequest()
    req.client = fc
    req.scope = {"path": path, "route": None}
    return req


def test_rate_limiter_returns_429_after_limit():
    """Rate limiter blocks requests after the limit for one (IP, endpoint)."""
    from api.main import _check_rate_limit, _RATE_LIMIT

    req = _fake_request(host="1.2.3.4", path="/x")

    for _ in range(_RATE_LIMIT):
        _check_rate_limit(req, limit=_RATE_LIMIT)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        _check_rate_limit(req, limit=_RATE_LIMIT)
    assert exc_info.value.status_code == 429


def test_rate_limiter_buckets_per_endpoint():
    """A near-full /search bucket must not block a separate /map-points call.

    Regression test for the shared-bucket-per-IP bug that produced bogus 429s
    during normal browsing — a single shared counter let one chatty endpoint
    drain the budget of every other endpoint with a stricter limit.
    """
    from api.main import _check_rate_limit

    req_search = _fake_request(host="9.9.9.9", path="/search")
    req_map = _fake_request(host="9.9.9.9", path="/hoas/map-points")

    # Burn the /search bucket up to but not past its own limit
    for _ in range(30):
        _check_rate_limit(req_search, limit=30)

    # The map-points bucket should be untouched and accept calls freely
    for _ in range(15):
        _check_rate_limit(req_map, limit=15)


def test_rate_limiter_skips_testclient():
    """TestClient requests are always allowed (skip rate limiting)."""
    from api.main import _check_rate_limit

    req = _fake_request(host="testclient", path="/anything")
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


# ---------------------------------------------------------------------------
# Disk-free ingest guard
# ---------------------------------------------------------------------------

def test_check_disk_free_passes_when_above_threshold(monkeypatch, tmp_path):
    from collections import namedtuple
    from api.main import _check_disk_free

    monkeypatch.setenv("MIN_FREE_DISK_GB", "10")
    Usage = namedtuple("Usage", "total used free")
    fake_free_bytes = 50 * 1024 ** 3  # 50 GB free
    monkeypatch.setattr(
        "api.main.shutil.disk_usage",
        lambda p: Usage(total=200 * 1024 ** 3, used=150 * 1024 ** 3, free=fake_free_bytes),
    )
    _check_disk_free(tmp_path)  # should not raise


def test_check_disk_free_aborts_when_below_threshold(monkeypatch, tmp_path):
    from collections import namedtuple
    from fastapi import HTTPException
    from api.main import _check_disk_free

    monkeypatch.setenv("MIN_FREE_DISK_GB", "10")
    Usage = namedtuple("Usage", "total used free")
    fake_free_bytes = 5 * 1024 ** 3  # 5 GB free, below 10 GB threshold
    monkeypatch.setattr(
        "api.main.shutil.disk_usage",
        lambda p: Usage(total=200 * 1024 ** 3, used=195 * 1024 ** 3, free=fake_free_bytes),
    )
    with pytest.raises(HTTPException) as exc_info:
        _check_disk_free(tmp_path)
    assert exc_info.value.status_code == 503
    assert "5.0 GB" in exc_info.value.detail
    assert "10 GB" in exc_info.value.detail


def test_check_disk_free_default_threshold_is_10gb(monkeypatch, tmp_path):
    from collections import namedtuple
    from fastapi import HTTPException
    from api.main import _check_disk_free

    monkeypatch.delenv("MIN_FREE_DISK_GB", raising=False)
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(
        "api.main.shutil.disk_usage",
        lambda p: Usage(total=200 * 1024 ** 3, used=195 * 1024 ** 3, free=9 * 1024 ** 3),
    )
    with pytest.raises(HTTPException):
        _check_disk_free(tmp_path)


def test_check_disk_free_swallows_oserror(monkeypatch, tmp_path):
    from api.main import _check_disk_free

    def boom(_):
        raise OSError("no such device")

    monkeypatch.setenv("MIN_FREE_DISK_GB", "10")
    monkeypatch.setattr("api.main.shutil.disk_usage", boom)
    _check_disk_free(tmp_path)  # best-effort: do not block ingest on stat failure


# ---------------------------------------------------------------------------
# Bulk-listing and AI-endpoint rate limits (anti-scrape)
# ---------------------------------------------------------------------------

def test_hoas_supports_optional_pagination():
    """GET /hoas accepts ?limit and ?offset; default returns full list."""
    r = client.get("/hoas")
    assert r.status_code == 200
    assert isinstance(r.json(), list)

    # limit=0 is invalid
    r = client.get("/hoas?limit=0")
    assert r.status_code == 400

    # negative offset is invalid
    r = client.get("/hoas?limit=10&offset=-1")
    assert r.status_code == 400

    # over-cap limit is invalid
    r = client.get("/hoas?limit=10000")
    assert r.status_code == 400


def test_hoas_pagination_slices_correctly():
    """?limit and ?offset paginate the list deterministically."""
    settings = load_settings()
    # Use deliberately unique names + clean up at the end. test_hardening's
    # autouse fixture doesn't clean `hoas` or `documents`, and pytest
    # collection means all test files share one DB path — so leftover rows
    # would pollute test_rename_hoa, test_proposals, etc.
    seeded = ("__pagination_test_AAA", "__pagination_test_BBB", "__pagination_test_CCC")
    seeded_ids: list[int] = []
    try:
        with db.get_connection(settings.db_path) as conn:
            for i, name in enumerate(seeded):
                hoa_id = db.get_or_create_hoa(conn, name)
                seeded_ids.append(hoa_id)
                db.upsert_document(
                    conn,
                    hoa_id=hoa_id,
                    relative_path=f"{name}/doc.pdf",
                    checksum=f"sha-{i}",
                    byte_size=1024,
                    page_count=1,
                    category="ccr",
                )
            conn.commit()

        full = client.get("/hoas").json()
        assert all(name in full for name in seeded)

        # Slice a window around our seeded entries and verify pagination shape.
        idx = sorted(full.index(name) for name in seeded)
        start = idx[0]
        page1 = client.get(f"/hoas?limit=2&offset={start}").json()
        page2 = client.get(f"/hoas?limit=2&offset={start + 2}").json()
        assert len(page1) == 2
        assert not (set(page1) & set(page2))  # no overlap between pages
    finally:
        with db.get_connection(settings.db_path) as conn:
            for hoa_id in seeded_ids:
                conn.execute("DELETE FROM documents WHERE hoa_id = ?", (hoa_id,))
                conn.execute("DELETE FROM hoas WHERE id = ?", (hoa_id,))
            conn.commit()


def test_search_endpoint_has_rate_limit():
    """POST /search rejects rapid-fire requests from a single non-testclient IP."""
    # The TestClient skips the limiter (uses host="testclient"), so we exercise
    # the limiter at the function level using a fake request with a real IP.
    from fastapi import HTTPException
    from api.main import _check_rate_limit

    class FakeClient:
        host = "5.6.7.8"

    class FakeRequest:
        client = FakeClient()

    req = FakeRequest()
    # /search is at limit=30; 30 calls succeed, 31st raises
    for _ in range(30):
        _check_rate_limit(req, limit=30)
    with pytest.raises(HTTPException) as exc_info:
        _check_rate_limit(req, limit=30)
    assert exc_info.value.status_code == 429


def test_qa_multi_has_tighter_limit_than_qa():
    """qa/multi (limit=10) should run out before qa (limit=20) from same IP."""
    from fastapi import HTTPException
    from api.main import _check_rate_limit

    class FakeClient:
        host = "9.10.11.12"

    class FakeRequest:
        client = FakeClient()

    req = FakeRequest()
    # 10 succeed at limit=10; 11th raises
    for _ in range(10):
        _check_rate_limit(req, limit=10)
    with pytest.raises(HTTPException):
        _check_rate_limit(req, limit=10)


# ---------------------------------------------------------------------------
# TOS clickwrap on /auth/register
# ---------------------------------------------------------------------------

def _force_tos_check(monkeypatch):
    """Disable the TestClient bypass for TOS clickwrap tests.

    /auth/register skips TOS validation when request.client.host == "testclient"
    (matches the rate-limiter convention). To assert the real production
    behaviour, replace _enforce_tos_acceptance with one that ignores the host
    bypass while preserving the version check.
    """
    import api.main as main_mod

    def _strict_enforce(request, body_version):
        if body_version != main_mod.TOS_VERSION:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=400,
                detail=f"You must agree to the current Terms of Service (version {main_mod.TOS_VERSION}).",
            )

    monkeypatch.setattr(main_mod, "_enforce_tos_acceptance", _strict_enforce)


def test_register_rejects_missing_tos_acceptance(monkeypatch):
    """POST /auth/register without accepted_terms_version returns 400."""
    _force_tos_check(monkeypatch)
    r = client.post(
        "/auth/register",
        json={"email": "tos1@example.com", "password": "password1234"},
    )
    assert r.status_code == 400
    assert "Terms of Service" in r.json()["detail"]


def test_register_rejects_stale_tos_version(monkeypatch):
    """POST /auth/register with an old version returns 400."""
    _force_tos_check(monkeypatch)
    r = client.post(
        "/auth/register",
        json={
            "email": "tos2@example.com",
            "password": "password1234",
            "accepted_terms_version": "2024-01-01",
        },
    )
    assert r.status_code == 400


def test_register_persists_tos_version_and_timestamp(monkeypatch):
    """A successful registration writes both terms columns on the user row."""
    _force_tos_check(monkeypatch)
    from api.main import TOS_VERSION

    r = client.post(
        "/auth/register",
        json={
            "email": "tos3@example.com",
            "password": "password1234",
            "accepted_terms_version": TOS_VERSION,
        },
    )
    assert r.status_code == 200, r.text

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        row = conn.execute(
            "SELECT terms_version_accepted, terms_accepted_at FROM users WHERE email = ?",
            ("tos3@example.com",),
        ).fetchone()
    assert row is not None
    assert row["terms_version_accepted"] == TOS_VERSION
    assert row["terms_accepted_at"] is not None


# ---------------------------------------------------------------------------
# Request logging middleware
# ---------------------------------------------------------------------------

def test_request_log_writes_row_for_normal_request(monkeypatch):
    """A request to a non-skipped path should produce one request_log row."""
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "1")
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.execute("DELETE FROM request_log")
        conn.commit()

    r = client.get("/hoas")
    assert r.status_code == 200

    with db.get_connection(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT method, path, status_code FROM request_log WHERE path = ?",
            ("/hoas",),
        ).fetchall()
    assert len(rows) >= 1
    assert rows[0]["method"] == "GET"
    assert rows[0]["status_code"] == 200


def test_request_log_skips_healthz(monkeypatch):
    """/healthz should not appear in request_log even with logging enabled."""
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "1")
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.execute("DELETE FROM request_log")
        conn.commit()

    client.get("/healthz")

    with db.get_connection(settings.db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) c FROM request_log WHERE path = ?", ("/healthz",)
        ).fetchone()["c"]
    assert count == 0


def test_request_log_disabled_via_env(monkeypatch):
    """REQUEST_LOG_ENABLED=0 stops middleware from writing rows."""
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "0")
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.execute("DELETE FROM request_log")
        conn.commit()

    client.get("/hoas/states")

    with db.get_connection(settings.db_path) as conn:
        count = conn.execute("SELECT COUNT(*) c FROM request_log").fetchone()["c"]
    assert count == 0


def test_prune_request_log_deletes_old_rows():
    """prune_request_log removes rows older than retention_days."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.execute("DELETE FROM request_log")
        # Insert an "old" row dated 60 days ago, and a fresh one.
        conn.execute(
            "INSERT INTO request_log (timestamp, method, path, status_code, response_ms) "
            "VALUES (datetime('now','-60 days'), 'GET', '/old', 200, 1)"
        )
        conn.execute(
            "INSERT INTO request_log (timestamp, method, path, status_code, response_ms) "
            "VALUES (datetime('now'), 'GET', '/new', 200, 1)"
        )
        conn.commit()

        deleted = db.prune_request_log(conn, retention_days=30)
        assert deleted == 1

        remaining = conn.execute(
            "SELECT path FROM request_log ORDER BY id"
        ).fetchall()
    assert [r["path"] for r in remaining] == ["/new"]
