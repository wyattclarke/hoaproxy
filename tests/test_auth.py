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


# ---------------------------------------------------------------------------
# Account update (PUT /auth/me)
# ---------------------------------------------------------------------------

def _register(email, password="password1234", display_name=None):
    """Register and return (token, headers)."""
    body = {"email": email, "password": password}
    if display_name:
        body["display_name"] = display_name
    data = client.post("/auth/register", json=body).json()
    token = data["token"]
    return token, {"Authorization": f"Bearer {token}"}


def test_update_display_name():
    _, headers = _register("upd-name@example.com", display_name="Old Name")
    resp = client.put("/auth/me", json={"display_name": "New Name"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "New Name"
    # Verify persistence
    me = client.get("/auth/me", headers=headers).json()
    assert me["display_name"] == "New Name"


def test_update_email():
    _, headers = _register("upd-email-orig@example.com")
    resp = client.put("/auth/me", json={"email": "upd-email-new@example.com"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["email"] == "upd-email-new@example.com"


def test_update_email_duplicate_rejected():
    _register("taken@example.com")
    _, headers = _register("wants-taken@example.com")
    resp = client.put("/auth/me", json={"email": "taken@example.com"}, headers=headers)
    assert resp.status_code == 409


def test_change_password_success():
    _, headers = _register("chgpw@example.com", password="oldpass1234")
    resp = client.put("/auth/me", json={
        "current_password": "oldpass1234",
        "new_password": "newpass5678",
    }, headers=headers)
    assert resp.status_code == 200
    # Old password fails
    login = client.post("/auth/login", json={"email": "chgpw@example.com", "password": "oldpass1234"})
    assert login.status_code == 401
    # New password works
    login = client.post("/auth/login", json={"email": "chgpw@example.com", "password": "newpass5678"})
    assert login.status_code == 200


def test_change_password_wrong_current():
    _, headers = _register("wrongcur@example.com", password="realpass123")
    resp = client.put("/auth/me", json={
        "current_password": "wrongpass123",
        "new_password": "newpass5678",
    }, headers=headers)
    assert resp.status_code == 403


def test_change_password_missing_current():
    _, headers = _register("misscur@example.com")
    resp = client.put("/auth/me", json={"new_password": "newpass5678"}, headers=headers)
    assert resp.status_code == 400


def test_change_password_too_short():
    _, headers = _register("shortpw@example.com", password="password1234")
    resp = client.put("/auth/me", json={
        "current_password": "password1234",
        "new_password": "short",
    }, headers=headers)
    assert resp.status_code == 400


def test_update_unauthenticated():
    resp = client.put("/auth/me", json={"display_name": "Hacker"})
    assert resp.status_code == 401


def test_update_name_and_email_together():
    _, headers = _register("combo@example.com", display_name="First Last")
    resp = client.put("/auth/me", json={
        "display_name": "Jane Smith",
        "email": "combo-new@example.com",
    }, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "Jane Smith"
    assert data["email"] == "combo-new@example.com"


def test_update_noop():
    """PUT with no fields still returns 200."""
    _, headers = _register("noop@example.com")
    resp = client.put("/auth/me", json={}, headers=headers)
    assert resp.status_code == 200


def test_forged_token_with_other_users_jti_rejected():
    """A token whose `sub` does not match the session's user_id must be
    rejected. Guards against impersonation if JWT_SECRET ever leaks: an
    attacker with their own valid jti cannot mint a token claiming sub=victim.
    """
    from jose import jwt as _jwt
    from datetime import datetime, timedelta, timezone

    # Two real users, each with a real session.
    victim_data = client.post("/auth/register", json={
        "email": "victim@example.com", "password": "password1234",
    }).json()
    attacker_data = client.post("/auth/register", json={
        "email": "attacker@example.com", "password": "password1234",
    }).json()

    settings = load_settings()
    # Decode attacker's real token to recover their jti.
    attacker_payload = _jwt.decode(
        attacker_data["token"], settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
    )
    attacker_jti = attacker_payload["jti"]

    # Forge a token: victim's user_id + attacker's live jti, signed with
    # the same secret.
    forged = _jwt.encode(
        {
            "sub": str(victim_data["user_id"]),
            "jti": attacker_jti,
            "exp": datetime.now(timezone.utc) + timedelta(days=1),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401
