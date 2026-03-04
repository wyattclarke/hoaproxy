"""Tests for Milestone 4: Proxy Assignment & E-Signature MVP."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

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
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
        # Clean data between tests
        for table in ["proxy_audit", "proxy_assignments", "delegates", "membership_claims", "sessions", "users"]:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    yield


def _setup_users_and_hoa():
    """Create grantor user, delegate user, HOA, memberships, and delegate registration.
    Returns (grantor_headers, delegate_headers, hoa_id, delegate_user_id).
    """
    # Register grantor
    grantor_reg = client.post("/auth/register", json={
        "email": "grantor@example.com", "password": "password1234", "display_name": "Grantor",
    }).json()
    grantor_headers = {"Authorization": f"Bearer {grantor_reg['token']}"}

    # Register delegate user
    delegate_reg = client.post("/auth/register", json={
        "email": "delegate@example.com", "password": "password1234", "display_name": "Delegate",
    }).json()
    delegate_headers = {"Authorization": f"Bearer {delegate_reg['token']}"}

    # Create HOA
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, "Proxy Test HOA")

    # Both claim membership
    client.post(f"/user/hoas/{hoa_id}/claim", json={"unit_number": "101"}, headers=grantor_headers)
    client.post(f"/user/hoas/{hoa_id}/claim", json={"unit_number": "202"}, headers=delegate_headers)

    # Delegate registers as delegate
    client.post("/delegates/register", json={"hoa_id": hoa_id, "bio": "Reform!"}, headers=delegate_headers)

    return grantor_headers, delegate_headers, hoa_id, delegate_reg["user_id"]


def test_create_proxy():
    grantor_h, _, hoa_id, delegate_uid = _setup_users_and_hoa()
    resp = client.post("/proxies", json={
        "delegate_user_id": delegate_uid,
        "hoa_id": hoa_id,
        "direction": "directed",
        "for_meeting_date": "2026-05-01",
    }, headers=grantor_h)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "draft"
    assert data["direction"] == "directed"
    assert data["delegate_user_id"] == delegate_uid


def test_create_proxy_self_assign_fails():
    # Delegate tries to assign proxy to themselves
    _, delegate_h, hoa_id, delegate_uid = _setup_users_and_hoa()
    resp = client.post("/proxies", json={
        "delegate_user_id": delegate_uid,
        "hoa_id": hoa_id,
    }, headers=delegate_h)
    assert resp.status_code == 400


def test_sign_proxy():
    grantor_h, _, hoa_id, delegate_uid = _setup_users_and_hoa()
    proxy = client.post("/proxies", json={
        "delegate_user_id": delegate_uid, "hoa_id": hoa_id,
    }, headers=grantor_h).json()
    resp = client.post(f"/proxies/{proxy['id']}/sign", json={}, headers=grantor_h)
    assert resp.status_code == 200
    assert resp.json()["status"] == "signed"


def test_deliver_proxy():
    grantor_h, _, hoa_id, delegate_uid = _setup_users_and_hoa()
    proxy = client.post("/proxies", json={
        "delegate_user_id": delegate_uid, "hoa_id": hoa_id,
    }, headers=grantor_h).json()
    client.post(f"/proxies/{proxy['id']}/sign", json={}, headers=grantor_h)
    resp = client.post(f"/proxies/{proxy['id']}/deliver", json={}, headers=grantor_h)
    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"


def test_deliver_unsigned_fails():
    grantor_h, _, hoa_id, delegate_uid = _setup_users_and_hoa()
    proxy = client.post("/proxies", json={
        "delegate_user_id": delegate_uid, "hoa_id": hoa_id,
    }, headers=grantor_h).json()
    resp = client.post(f"/proxies/{proxy['id']}/deliver", json={}, headers=grantor_h)
    assert resp.status_code == 400


def test_revoke_proxy():
    grantor_h, _, hoa_id, delegate_uid = _setup_users_and_hoa()
    proxy = client.post("/proxies", json={
        "delegate_user_id": delegate_uid, "hoa_id": hoa_id,
    }, headers=grantor_h).json()
    client.post(f"/proxies/{proxy['id']}/sign", json={}, headers=grantor_h)
    resp = client.post(f"/proxies/{proxy['id']}/revoke", json={"reason": "Changed my mind"}, headers=grantor_h)
    assert resp.status_code == 200
    assert resp.json()["status"] == "revoked"


def test_revoke_already_revoked_fails():
    grantor_h, _, hoa_id, delegate_uid = _setup_users_and_hoa()
    proxy = client.post("/proxies", json={
        "delegate_user_id": delegate_uid, "hoa_id": hoa_id,
    }, headers=grantor_h).json()
    client.post(f"/proxies/{proxy['id']}/revoke", json={}, headers=grantor_h)
    resp = client.post(f"/proxies/{proxy['id']}/revoke", json={}, headers=grantor_h)
    assert resp.status_code == 400


def test_full_lifecycle():
    """draft → signed → delivered lifecycle."""
    grantor_h, delegate_h, hoa_id, delegate_uid = _setup_users_and_hoa()

    # Create
    proxy = client.post("/proxies", json={
        "delegate_user_id": delegate_uid, "hoa_id": hoa_id,
        "direction": "directed", "for_meeting_date": "2026-06-15",
    }, headers=grantor_h).json()
    assert proxy["status"] == "draft"

    # Sign
    signed = client.post(f"/proxies/{proxy['id']}/sign", json={}, headers=grantor_h).json()
    assert signed["status"] == "signed"
    assert signed["signed_at"] is not None

    # Deliver
    delivered = client.post(f"/proxies/{proxy['id']}/deliver", json={}, headers=grantor_h).json()
    assert delivered["status"] == "delivered"
    assert delivered["delivered_at"] is not None

    # Grantor can see in their list
    mine = client.get("/proxies/mine", headers=grantor_h).json()
    assert any(p["id"] == proxy["id"] for p in mine)

    # Delegate can see incoming
    delegated = client.get("/proxies/delegated", headers=delegate_h).json()
    assert any(p["id"] == proxy["id"] for p in delegated)


def test_get_proxy_form():
    grantor_h, _, hoa_id, delegate_uid = _setup_users_and_hoa()
    proxy = client.post("/proxies", json={
        "delegate_user_id": delegate_uid, "hoa_id": hoa_id,
    }, headers=grantor_h).json()
    resp = client.get(f"/proxies/{proxy['id']}/form", headers=grantor_h)
    assert resp.status_code == 200
    assert "Proxy Authorization Form" in resp.text


def test_proxy_stats():
    grantor_h, _, hoa_id, delegate_uid = _setup_users_and_hoa()
    client.post("/proxies", json={
        "delegate_user_id": delegate_uid, "hoa_id": hoa_id,
    }, headers=grantor_h)
    resp = client.get(f"/hoas/{hoa_id}/proxy-stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
