"""Tests for Milestone 2: Delegate Registration."""

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
    yield


def _register_and_claim(email, hoa_name):
    """Helper: register user, create HOA, claim membership. Returns (headers, hoa_id)."""
    reg = client.post("/auth/register", json={
        "email": email, "password": "password1234", "display_name": email.split("@")[0],
    }).json()
    headers = {"Authorization": f"Bearer {reg['token']}"}

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, hoa_name)

    client.post(f"/user/hoas/{hoa_id}/claim", json={}, headers=headers)
    return headers, hoa_id


def test_register_delegate():
    headers, hoa_id = _register_and_claim("del1@example.com", "Del HOA 1")
    resp = client.post("/delegates/register", json={
        "hoa_id": hoa_id, "bio": "I care about transparency.",
    }, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["bio"] == "I care about transparency."
    assert data["hoa_id"] == hoa_id


def test_register_delegate_no_membership():
    reg = client.post("/auth/register", json={
        "email": "nomem@example.com", "password": "password1234",
    }).json()
    headers = {"Authorization": f"Bearer {reg['token']}"}
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, "No Mem HOA")
    resp = client.post("/delegates/register", json={"hoa_id": hoa_id}, headers=headers)
    assert resp.status_code == 403


def test_register_delegate_duplicate():
    headers, hoa_id = _register_and_claim("dup_del@example.com", "Dup Del HOA")
    client.post("/delegates/register", json={"hoa_id": hoa_id}, headers=headers)
    resp = client.post("/delegates/register", json={"hoa_id": hoa_id}, headers=headers)
    assert resp.status_code == 409


def test_get_delegate_profile():
    headers, hoa_id = _register_and_claim("profile@example.com", "Profile HOA")
    reg_resp = client.post("/delegates/register", json={
        "hoa_id": hoa_id, "bio": "Reform agenda",
    }, headers=headers)
    delegate_id = reg_resp.json()["id"]
    # Public endpoint - no auth required
    resp = client.get(f"/delegates/{delegate_id}")
    assert resp.status_code == 200
    assert resp.json()["bio"] == "Reform agenda"


def test_list_hoa_delegates():
    headers, hoa_id = _register_and_claim("list_del@example.com", "List Del HOA")
    client.post("/delegates/register", json={"hoa_id": hoa_id, "bio": "First"}, headers=headers)
    resp = client.get(f"/hoas/{hoa_id}/delegates")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


def test_update_delegate():
    headers, hoa_id = _register_and_claim("upd_del@example.com", "Upd Del HOA")
    reg_resp = client.post("/delegates/register", json={
        "hoa_id": hoa_id, "bio": "Old bio",
    }, headers=headers)
    delegate_id = reg_resp.json()["id"]
    resp = client.patch(f"/delegates/{delegate_id}", json={"bio": "New bio"}, headers=headers)
    assert resp.status_code == 200
    # Verify update
    profile = client.get(f"/delegates/{delegate_id}").json()
    assert profile["bio"] == "New bio"


def test_update_delegate_not_owner():
    headers1, hoa_id = _register_and_claim("own1@example.com", "Own HOA")
    reg_resp = client.post("/delegates/register", json={"hoa_id": hoa_id}, headers=headers1)
    delegate_id = reg_resp.json()["id"]

    # Another user
    reg2 = client.post("/auth/register", json={
        "email": "own2@example.com", "password": "password1234",
    }).json()
    headers2 = {"Authorization": f"Bearer {reg2['token']}"}

    resp = client.patch(f"/delegates/{delegate_id}", json={"bio": "Hacked"}, headers=headers2)
    assert resp.status_code == 403
