"""Tests for the generic bulk import endpoint."""

import json
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
ADMIN_HEADERS = {"Authorization": "Bearer test-secret-for-ci"}


@pytest.fixture(autouse=True)
def _setup_db():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
        # Clean in FK-safe order: dependents first, then hoas
        for table in [
            "proposal_upvotes", "proposals",
            "participation_records", "proxy_audit", "proxy_assignments",
            "delegates", "membership_claims", "sessions", "users",
            "hoa_locations", "hoas",
        ]:
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()
    yield


def test_bulk_import_requires_auth():
    resp = client.post("/admin/bulk-import", json={
        "source": "test", "records": [{"name": "Test HOA"}],
    })
    assert resp.status_code == 403


def test_bulk_import_basic():
    records = [
        {"name": "Alpha HOA", "metadata_type": "timeshare", "city": "Phoenix", "state": "AZ"},
        {"name": "Beta HOA", "city": "Tucson", "state": "AZ"},
        {"name": "Gamma HOA", "city": "Flagstaff", "state": "AZ"},
    ]
    resp = client.post("/admin/bulk-import", json={
        "source": "test_source", "records": records,
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["imported"] == 3
    assert data["skipped"] == 0

    # Verify in DB
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT l.city, l.source, l.metadata_type FROM hoa_locations l JOIN hoas h ON l.hoa_id = h.id ORDER BY h.name"
        ).fetchall()
    assert len(rows) == 3
    assert rows[0]["city"] == "Phoenix"
    assert rows[0]["source"] == "test_source"
    assert rows[0]["metadata_type"] == "timeshare"


def test_bulk_import_partial_update():
    """COALESCE behavior: existing non-null fields are preserved when new value is null."""
    resp = client.post("/admin/bulk-import", json={
        "source": "s1",
        "records": [{"name": "Keep HOA", "city": "Denver", "state": "CO"}],
    }, headers=ADMIN_HEADERS)
    assert resp.json()["imported"] == 1

    # Second import: city=None should preserve existing city
    resp = client.post("/admin/bulk-import", json={
        "source": "s2",
        "records": [{"name": "Keep HOA", "state": "CO"}],
    }, headers=ADMIN_HEADERS)
    assert resp.json()["imported"] == 1

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        row = conn.execute(
            "SELECT l.city, l.source FROM hoa_locations l JOIN hoas h ON l.hoa_id = h.id WHERE h.name = ?",
            ("Keep HOA",),
        ).fetchone()
    assert row["city"] == "Denver"  # preserved
    assert row["source"] == "s2"  # updated


def test_bulk_import_empty_name_skipped():
    resp = client.post("/admin/bulk-import", json={
        "source": "test",
        "records": [
            {"name": "Real HOA"},
            {"name": ""},
            {"name": "   "},
        ],
    }, headers=ADMIN_HEADERS)
    data = resp.json()
    assert data["imported"] == 1
    assert data["skipped"] == 2


def test_bulk_import_max_batch_size():
    records = [{"name": f"HOA {i}"} for i in range(5001)]
    resp = client.post("/admin/bulk-import", json={
        "source": "test", "records": records,
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 400
    assert "5000" in resp.json()["detail"]


def test_bulk_import_with_boundary():
    boundary = {
        "type": "Polygon",
        "coordinates": [[[-110.0, 32.0], [-110.0, 32.1], [-109.9, 32.1], [-109.9, 32.0], [-110.0, 32.0]]],
    }
    resp = client.post("/admin/bulk-import", json={
        "source": "test",
        "records": [{"name": "Boundary HOA", "boundary_geojson": boundary, "latitude": 32.05, "longitude": -109.95}],
    }, headers=ADMIN_HEADERS)
    assert resp.json()["imported"] == 1

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        row = conn.execute(
            "SELECT l.boundary_geojson FROM hoa_locations l JOIN hoas h ON l.hoa_id = h.id WHERE h.name = ?",
            ("Boundary HOA",),
        ).fetchone()
    stored = json.loads(row["boundary_geojson"])
    assert stored["type"] == "Polygon"
    assert len(stored["coordinates"][0]) == 5


def test_bulk_import_state_uppercased():
    resp = client.post("/admin/bulk-import", json={
        "source": "test",
        "records": [{"name": "Lower HOA", "state": "az"}],
    }, headers=ADMIN_HEADERS)
    assert resp.json()["imported"] == 1

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        row = conn.execute(
            "SELECT l.state FROM hoa_locations l JOIN hoas h ON l.hoa_id = h.id WHERE h.name = ?",
            ("Lower HOA",),
        ).fetchone()
    assert row["state"] == "AZ"


def test_bulk_import_centroid_from_boundary():
    """When lat/lon are missing but boundary is present, centroid is computed."""
    boundary = {
        "type": "Polygon",
        "coordinates": [[[-110.0, 32.0], [-110.0, 32.1], [-109.9, 32.1], [-109.9, 32.0], [-110.0, 32.0]]],
    }
    resp = client.post("/admin/bulk-import", json={
        "source": "test",
        "records": [{"name": "Centroid HOA", "boundary_geojson": boundary}],
    }, headers=ADMIN_HEADERS)
    assert resp.json()["imported"] == 1

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        row = conn.execute(
            "SELECT l.latitude, l.longitude FROM hoa_locations l JOIN hoas h ON l.hoa_id = h.id WHERE h.name = ?",
            ("Centroid HOA",),
        ).fetchone()
    assert row["latitude"] is not None
    assert row["longitude"] is not None
    # Centroid of the box should be approximately (32.05, -109.95)
    assert abs(row["latitude"] - 32.05) < 0.01
    assert abs(row["longitude"] - (-109.95)) < 0.01


def test_bulk_import_rejects_unknown_metadata_type():
    resp = client.post("/admin/bulk-import", json={
        "source": "test",
        "records": [{"name": "Bad Type HOA", "metadata_type": "business-park"}],
    }, headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["imported"] == 0
    assert len(data["errors"]) == 1
    assert "metadata_type" in data["errors"][0]
