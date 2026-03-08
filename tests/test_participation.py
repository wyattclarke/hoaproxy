"""Tests for Milestone 7: Participation Dashboard & Magic Number."""

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
from hoaware.participation import (  # noqa: E402
    add_participation_record,
    calculate_magic_number,
    get_participation_records,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def _setup_db():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
        for table in [
            "participation_records",
            "proxy_audit",
            "proxy_assignments",
            "delegates",
            "membership_claims",
            "sessions",
            "users",
            "hoas",
        ]:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    yield


def _make_hoa(name: str = "Test HOA") -> int:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        return db.get_or_create_hoa(conn, name)


def _register(email: str) -> dict:
    resp = client.post("/auth/register", json={"email": email, "password": "password1234"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


# ---------------------------------------------------------------------------
# Direct DB function tests
# ---------------------------------------------------------------------------

def test_add_participation_record_basic():
    settings = load_settings()
    hoa_id = _make_hoa("DB HOA")
    with db.get_connection(settings.db_path) as conn:
        record_id = add_participation_record(
            conn,
            hoa_id=hoa_id,
            meeting_date="2025-06-01",
            meeting_type="annual",
            total_units=100,
            votes_cast=30,
        )
    assert isinstance(record_id, int)
    assert record_id > 0


def test_get_participation_records_order():
    settings = load_settings()
    hoa_id = _make_hoa("Order HOA")
    with db.get_connection(settings.db_path) as conn:
        add_participation_record(conn, hoa_id=hoa_id, meeting_date="2023-01-01", meeting_type="annual", total_units=100, votes_cast=20)
        add_participation_record(conn, hoa_id=hoa_id, meeting_date="2025-01-01", meeting_type="annual", total_units=100, votes_cast=40)
        add_participation_record(conn, hoa_id=hoa_id, meeting_date="2024-01-01", meeting_type="special", total_units=100, votes_cast=30)
        records = get_participation_records(conn, hoa_id)
    assert len(records) == 3
    # Newest first
    assert records[0]["meeting_date"] == "2025-01-01"
    assert records[1]["meeting_date"] == "2024-01-01"
    assert records[2]["meeting_date"] == "2023-01-01"


# ---------------------------------------------------------------------------
# calculate_magic_number tests
# ---------------------------------------------------------------------------

def test_magic_number_zero_records():
    settings = load_settings()
    hoa_id = _make_hoa("Empty HOA")
    with db.get_connection(settings.db_path) as conn:
        result = calculate_magic_number(conn, hoa_id)
    assert result["data_points"] == 0
    assert result["confidence"] == "low"
    assert result["proxies_to_swing"] == 0


def test_magic_number_one_record():
    settings = load_settings()
    hoa_id = _make_hoa("One HOA")
    with db.get_connection(settings.db_path) as conn:
        add_participation_record(conn, hoa_id=hoa_id, meeting_date="2025-01-01", meeting_type="annual", total_units=100, votes_cast=40)
        result = calculate_magic_number(conn, hoa_id)
    assert result["data_points"] == 1
    assert result["confidence"] == "low"
    assert result["average_votes_cast"] == 40
    assert result["total_units"] == 100
    # ceil(40/2) + 1 = 21
    assert result["proxies_to_swing"] == 21
    assert abs(result["average_participation_rate"] - 0.4) < 0.001


def test_magic_number_three_records():
    settings = load_settings()
    hoa_id = _make_hoa("Three HOA")
    with db.get_connection(settings.db_path) as conn:
        add_participation_record(conn, hoa_id=hoa_id, meeting_date="2023-01-01", meeting_type="annual", total_units=100, votes_cast=30)
        add_participation_record(conn, hoa_id=hoa_id, meeting_date="2024-01-01", meeting_type="annual", total_units=100, votes_cast=40)
        add_participation_record(conn, hoa_id=hoa_id, meeting_date="2025-01-01", meeting_type="annual", total_units=100, votes_cast=50)
        result = calculate_magic_number(conn, hoa_id)
    assert result["data_points"] == 3
    assert result["confidence"] == "medium"
    # avg = (30+40+50)/3 = 40
    assert result["average_votes_cast"] == 40
    # ceil(40/2)+1 = 21
    assert result["proxies_to_swing"] == 21


def test_magic_number_six_records():
    settings = load_settings()
    hoa_id = _make_hoa("Six HOA")
    with db.get_connection(settings.db_path) as conn:
        for i, vc in enumerate([20, 25, 30, 35, 40, 45]):
            add_participation_record(
                conn, hoa_id=hoa_id, meeting_date=f"202{i}-01-01",
                meeting_type="annual", total_units=100, votes_cast=vc,
            )
        result = calculate_magic_number(conn, hoa_id)
    assert result["data_points"] == 6
    assert result["confidence"] == "high"
    # avg = (20+25+30+35+40+45)/6 = 32.5 -> rounds to 32 or 33
    assert result["average_votes_cast"] in (32, 33)


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

def test_post_participation_requires_auth():
    hoa_id = _make_hoa("API HOA")
    resp = client.post(f"/hoas/{hoa_id}/participation", json={
        "meeting_date": "2025-06-01",
        "meeting_type": "annual",
        "total_units": 100,
        "votes_cast": 30,
    })
    assert resp.status_code == 401


def test_post_participation_requires_membership():
    hoa_id = _make_hoa("Membership HOA")
    headers = _register("nomember@example.com")
    resp = client.post(f"/hoas/{hoa_id}/participation", json={
        "meeting_date": "2025-06-01",
        "meeting_type": "annual",
        "total_units": 100,
        "votes_cast": 30,
    }, headers=headers)
    assert resp.status_code == 403


def test_post_participation_success():
    hoa_id = _make_hoa("Post HOA")
    headers = _register("member_post@example.com")
    client.post(f"/user/hoas/{hoa_id}/claim", json={"unit_number": "1"}, headers=headers)
    resp = client.post(f"/hoas/{hoa_id}/participation", json={
        "meeting_date": "2025-06-01",
        "meeting_type": "annual",
        "total_units": 150,
        "votes_cast": 45,
        "quorum_required": 38,
        "quorum_met": True,
        "notes": "Good turnout",
    }, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["hoa_id"] == hoa_id
    assert "id" in data


def test_get_participation_public():
    hoa_id = _make_hoa("Get HOA")
    headers = _register("member_get@example.com")
    client.post(f"/user/hoas/{hoa_id}/claim", json={"unit_number": "1"}, headers=headers)
    client.post(f"/hoas/{hoa_id}/participation", json={
        "meeting_date": "2025-06-01",
        "meeting_type": "annual",
        "total_units": 100,
        "votes_cast": 30,
    }, headers=headers)
    # Public (no auth)
    resp = client.get(f"/hoas/{hoa_id}/participation")
    assert resp.status_code == 200
    records = resp.json()
    assert len(records) == 1
    assert records[0]["meeting_date"] == "2025-06-01"
    assert records[0]["votes_cast"] == 30


def test_get_magic_number_no_data_returns_404():
    hoa_id = _make_hoa("Empty MN HOA")
    resp = client.get(f"/hoas/{hoa_id}/magic-number")
    assert resp.status_code == 404


def test_get_magic_number_with_data():
    hoa_id = _make_hoa("MN Data HOA")
    headers = _register("member_mn@example.com")
    client.post(f"/user/hoas/{hoa_id}/claim", json={"unit_number": "1"}, headers=headers)
    for i, vc in enumerate([20, 30, 40, 50]):
        client.post(f"/hoas/{hoa_id}/participation", json={
            "meeting_date": f"202{i+1}-06-01",
            "meeting_type": "annual",
            "total_units": 100,
            "votes_cast": vc,
        }, headers=headers)
    resp = client.get(f"/hoas/{hoa_id}/magic-number")
    assert resp.status_code == 200
    data = resp.json()
    assert data["hoa_id"] == hoa_id
    assert data["data_points"] == 4
    assert "proxies_to_swing" in data
    assert "average_participation_rate" in data
    assert "confidence" in data
