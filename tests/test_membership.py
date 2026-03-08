"""Tests for Milestone 1: Membership Claims (subset of auth flow)."""

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


def _register(email: str) -> dict:
    resp = client.post("/auth/register", json={"email": email, "password": "password1234"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _make_hoa(name: str) -> int:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        return db.get_or_create_hoa(conn, name)


def test_claim_creates_record():
    headers = _register("mc1@example.com")
    hoa_id = _make_hoa("MC HOA 1")
    resp = client.post(f"/user/hoas/{hoa_id}/claim", json={"unit_number": "42"}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "self_declared"
    assert data["unit_number"] == "42"
    assert data["hoa_id"] == hoa_id


def test_claim_nonexistent_hoa_returns_404():
    headers = _register("mc2@example.com")
    resp = client.post("/user/hoas/99999/claim", json={}, headers=headers)
    assert resp.status_code == 404


def test_duplicate_claim_returns_409():
    headers = _register("mc3@example.com")
    hoa_id = _make_hoa("MC HOA 2")
    client.post(f"/user/hoas/{hoa_id}/claim", json={}, headers=headers)
    resp = client.post(f"/user/hoas/{hoa_id}/claim", json={}, headers=headers)
    assert resp.status_code == 409


def test_claim_requires_auth():
    hoa_id = _make_hoa("MC HOA 3")
    resp = client.post(f"/user/hoas/{hoa_id}/claim", json={})
    assert resp.status_code == 401


def test_list_memberships():
    headers = _register("mc4@example.com")
    hoa_id = _make_hoa("MC HOA 4")
    client.post(f"/user/hoas/{hoa_id}/claim", json={"unit_number": "7B"}, headers=headers)
    resp = client.get("/user/hoas", headers=headers)
    assert resp.status_code == 200
    items = resp.json()
    assert any(c["hoa_id"] == hoa_id for c in items)


def test_claim_by_name():
    headers = _register("mc5@example.com")
    _make_hoa("Sunrise HOA")
    resp = client.post("/user/hoas/claim-by-name", json={"hoa_name": "Sunrise HOA"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["hoa_name"] == "Sunrise HOA"
