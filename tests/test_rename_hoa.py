"""Tests for /admin/rename-hoa: pure rename + merge-on-collision."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["HOA_DB_PATH"] = _tmp_db.name
os.environ["JWT_SECRET"] = "test-secret-for-ci"
os.environ["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY", "sk-test-fake")
_tmp_db.close()

_tmp_docs = tempfile.mkdtemp(prefix="hoa_docs_rename_")
os.environ["HOA_DOCS_ROOT"] = _tmp_docs

from api.main import app  # noqa: E402
from hoaware import db  # noqa: E402
from hoaware.config import load_settings  # noqa: E402


client = TestClient(app)
AUTH = {"Authorization": "Bearer test-secret-for-ci"}


@pytest.fixture(autouse=True)
def _reset_db():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
        for table in [
            "proxy_audit", "proxy_assignments", "delegates", "membership_claims",
            "sessions", "users",
            "chunks", "documents", "hoa_locations", "hoas",
        ]:
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()
    yield


def _seed_hoa(name: str, *, with_doc: str | None = None, lat: float | None = None) -> int:
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        cur = conn.execute("INSERT INTO hoas (name) VALUES (?)", (name,))
        hoa_id = int(cur.lastrowid)
        if with_doc:
            conn.execute(
                "INSERT INTO documents (hoa_id, relative_path, checksum, bytes) VALUES (?, ?, ?, ?)",
                (hoa_id, with_doc, "deadbeef" + with_doc, 1024),
            )
        if lat is not None:
            conn.execute(
                "INSERT INTO hoa_locations (hoa_id, latitude, longitude) VALUES (?, ?, ?)",
                (hoa_id, lat, -84.0),
            )
        conn.commit()
    return hoa_id


def test_rename_requires_admin():
    hoa_id = _seed_hoa("dirty name HOA")
    r = client.post("/admin/rename-hoa", json={"hoa_id": hoa_id, "new_name": "Clean Name"})
    assert r.status_code == 403


def test_pure_rename():
    hoa_id = _seed_hoa("dirty name HOA", with_doc="ccr.pdf", lat=33.7)
    r = client.post(
        "/admin/rename-hoa",
        headers=AUTH,
        json={"hoa_id": hoa_id, "new_name": "Buckhead Homeowners Association"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["renamed"] == 1 and body["merged"] == 0 and body["errors"] == 0
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        row = conn.execute("SELECT name FROM hoas WHERE id = ?", (hoa_id,)).fetchone()
        assert row["name"] == "Buckhead Homeowners Association"
        # docs/locations preserved on the same hoa_id
        assert conn.execute(
            "SELECT count(*) c FROM documents WHERE hoa_id = ?", (hoa_id,)
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT count(*) c FROM hoa_locations WHERE hoa_id = ?", (hoa_id,)
        ).fetchone()["c"] == 1


def test_rename_to_same_name_is_noop():
    hoa_id = _seed_hoa("Already Clean")
    r = client.post(
        "/admin/rename-hoa",
        headers=AUTH,
        json={"hoa_id": hoa_id, "new_name": "Already Clean"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["noop"] == 1 and body["renamed"] == 0


def test_merge_into_existing():
    target = _seed_hoa("Buckhead Homeowners Association", with_doc="bylaws.pdf", lat=33.84)
    source = _seed_hoa(
        "LAWS OF Buckhead - Keystone HOA",
        with_doc="ccr.pdf",  # different path, should move
        lat=None,            # no source location
    )
    r = client.post(
        "/admin/rename-hoa",
        headers=AUTH,
        json={"hoa_id": source, "new_name": "Buckhead Homeowners Association"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["merged"] == 1 and body["errors"] == 0
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        # source row gone
        assert conn.execute("SELECT 1 FROM hoas WHERE id = ?", (source,)).fetchone() is None
        # target keeps both docs (bylaws + moved ccr)
        docs = {
            r["relative_path"]
            for r in conn.execute(
                "SELECT relative_path FROM documents WHERE hoa_id = ?", (target,)
            )
        }
        assert docs == {"bylaws.pdf", "ccr.pdf"}
        # target keeps its own location
        loc = conn.execute(
            "SELECT latitude FROM hoa_locations WHERE hoa_id = ?", (target,)
        ).fetchone()
        assert loc is not None and abs(loc["latitude"] - 33.84) < 1e-6


def test_merge_drops_duplicate_doc():
    target = _seed_hoa("Buckhead Homeowners Association", with_doc="ccr.pdf", lat=33.84)
    source = _seed_hoa(
        "LAWS OF Buckhead - Keystone HOA",
        with_doc="ccr.pdf",  # same path → must be dropped on merge
    )
    r = client.post(
        "/admin/rename-hoa",
        headers=AUTH,
        json={"hoa_id": source, "new_name": "Buckhead Homeowners Association"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["merged"] == 1
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        # only one ccr.pdf left, attached to target
        rows = conn.execute(
            "SELECT hoa_id, relative_path FROM documents WHERE relative_path = 'ccr.pdf'"
        ).fetchall()
        assert len(rows) == 1
        assert int(rows[0]["hoa_id"]) == target


def test_merge_moves_location_when_target_has_none():
    target = _seed_hoa("Buckhead Homeowners Association")  # no location
    source = _seed_hoa("LAWS OF Buckhead", lat=33.7)        # has location
    r = client.post(
        "/admin/rename-hoa",
        headers=AUTH,
        json={"hoa_id": source, "new_name": "Buckhead Homeowners Association"},
    )
    assert r.status_code == 200
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        loc = conn.execute(
            "SELECT latitude FROM hoa_locations WHERE hoa_id = ?", (target,)
        ).fetchone()
        assert loc is not None and abs(loc["latitude"] - 33.7) < 1e-6


def test_dry_run_does_not_mutate():
    target = _seed_hoa("Clean")
    source = _seed_hoa("dirty")
    r = client.post(
        "/admin/rename-hoa",
        headers=AUTH,
        json={"hoa_id": source, "new_name": "Clean", "dry_run": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True and body["results"][0]["status"] == "would_merge"
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        assert conn.execute(
            "SELECT name FROM hoas WHERE id = ?", (source,)
        ).fetchone()["name"] == "dirty"


def test_bulk_renames():
    a = _seed_hoa("dirty a")
    b = _seed_hoa("dirty b")
    r = client.post(
        "/admin/rename-hoa",
        headers=AUTH,
        json={
            "renames": [
                {"hoa_id": a, "new_name": "Alpha"},
                {"hoa_id": b, "new_name": "Beta"},
            ]
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["renamed"] == 2 and body["errors"] == 0
