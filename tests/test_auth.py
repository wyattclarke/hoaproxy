"""Tests for Milestone 1: Authentication & User Identity."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# Use a temp DB for tests
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["HOA_DB_PATH"] = _tmp.name
os.environ["JWT_SECRET"] = "test-secret-for-ci"
_tmp.close()

from api.main import app  # noqa: E402
from hoaware import db  # noqa: E402
from hoaware.config import load_settings  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _setup_db():
    """Ensure tables exist before each test."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
    yield


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_register_success():
    resp = client.post("/auth/register", json={
        "email": "alice@example.com",
        "password": "securepass123",
        "display_name": "Alice",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data
    assert data["user_id"] > 0


def test_register_duplicate_email():
    client.post("/auth/register", json={
        "email": "dup@example.com",
        "password": "password1234",
    })
    resp = client.post("/auth/register", json={
        "email": "dup@example.com",
        "password": "password5678",
    })
    assert resp.status_code == 409


def test_register_short_password():
    resp = client.post("/auth/register", json={
        "email": "short@example.com",
        "password": "abc",
    })
    assert resp.status_code == 422  # validation error


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def test_login_success():
    client.post("/auth/register", json={
        "email": "login@example.com",
        "password": "password1234",
    })
    resp = client.post("/auth/login", json={
        "email": "login@example.com",
        "password": "password1234",
    })
    assert resp.status_code == 200
    assert "token" in resp.json()


def test_login_wrong_password():
    client.post("/auth/register", json={
        "email": "wrong@example.com",
        "password": "password1234",
    })
    resp = client.post("/auth/login", json={
        "email": "wrong@example.com",
        "password": "wrongpassword",
    })
    assert resp.status_code == 401


def test_login_nonexistent_email():
    resp = client.post("/auth/login", json={
        "email": "nonexist@example.com",
        "password": "password1234",
    })
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Auth me / logout
# ---------------------------------------------------------------------------

def test_me_authenticated():
    reg = client.post("/auth/register", json={
        "email": "me@example.com",
        "password": "password1234",
        "display_name": "TestUser",
    }).json()
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {reg['token']}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "me@example.com"
    assert data["display_name"] == "TestUser"
    assert isinstance(data["hoas"], list)


def test_me_unauthenticated():
    resp = client.get("/auth/me")
    assert resp.status_code == 401


def test_logout():
    reg = client.post("/auth/register", json={
        "email": "logout@example.com",
        "password": "password1234",
    }).json()
    token = reg["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Logout
    resp = client.post("/auth/logout", headers=headers)
    assert resp.status_code == 200

    # Token should no longer work
    resp = client.get("/auth/me", headers=headers)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Membership claims
# ---------------------------------------------------------------------------

def test_claim_membership():
    # Create user
    reg = client.post("/auth/register", json={
        "email": "member@example.com",
        "password": "password1234",
    }).json()
    headers = {"Authorization": f"Bearer {reg['token']}"}

    # Create an HOA to claim
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, "Test HOA")

    # Claim membership
    resp = client.post(f"/user/hoas/{hoa_id}/claim", json={"unit_number": "101"}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["hoa_name"] == "Test HOA"
    assert data["unit_number"] == "101"
    assert data["status"] == "self_declared"


def test_claim_membership_duplicate():
    reg = client.post("/auth/register", json={
        "email": "dupclam@example.com",
        "password": "password1234",
    }).json()
    headers = {"Authorization": f"Bearer {reg['token']}"}

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, "Dup HOA")

    client.post(f"/user/hoas/{hoa_id}/claim", json={}, headers=headers)
    resp = client.post(f"/user/hoas/{hoa_id}/claim", json={}, headers=headers)
    assert resp.status_code == 409


def test_claim_membership_nonexistent_hoa():
    reg = client.post("/auth/register", json={
        "email": "nohoa@example.com",
        "password": "password1234",
    }).json()
    headers = {"Authorization": f"Bearer {reg['token']}"}
    resp = client.post("/user/hoas/99999/claim", json={}, headers=headers)
    assert resp.status_code == 404


def test_list_user_hoas():
    reg = client.post("/auth/register", json={
        "email": "lister@example.com",
        "password": "password1234",
    }).json()
    headers = {"Authorization": f"Bearer {reg['token']}"}

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, "List HOA")

    client.post(f"/user/hoas/{hoa_id}/claim", json={}, headers=headers)
    resp = client.get("/user/hoas", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert any(c["hoa_name"] == "List HOA" for c in data)


def test_me_includes_hoas():
    reg = client.post("/auth/register", json={
        "email": "mehoas@example.com",
        "password": "password1234",
    }).json()
    headers = {"Authorization": f"Bearer {reg['token']}"}

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, "Me HOA")

    client.post(f"/user/hoas/{hoa_id}/claim", json={"unit_number": "A1"}, headers=headers)
    resp = client.get("/auth/me", headers=headers)
    data = resp.json()
    assert len(data["hoas"]) >= 1
    assert any(h["hoa_name"] == "Me HOA" for h in data["hoas"])
