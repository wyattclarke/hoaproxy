"""Phase 2 ASYNC_INGEST_ENABLED route bifurcation tests."""

import hashlib
import io
import json

import pytest
from fastapi.testclient import TestClient

from api import main as api_main  # noqa: E402
from api.main import app  # noqa: E402
from hoaware import db, prepared_ingest  # noqa: E402
from hoaware.config import load_settings  # noqa: E402

# Reuse the FakeBucket scaffolding from the prepared_ingest test.
from tests.test_prepared_ingest import FakeBucket, _make_text_pdf, _bundle_payload  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_db(monkeypatch, tmp_path):
    monkeypatch.setenv("HOA_DB_PATH", str(tmp_path / "async.db"))
    monkeypatch.setenv("HOA_DOCS_ROOT", str(tmp_path / "docs"))
    monkeypatch.setenv("JWT_SECRET", "test-secret-for-ci")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    monkeypatch.setenv("ASYNC_INGEST_ENABLED", "1")
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
        conn.commit()


def test_upload_async_returns_202_shape_with_job_id_and_status_url(monkeypatch):
    """When ASYNC_INGEST_ENABLED=1, /upload returns queued+job_id, no sync ingest."""
    # Authenticated /upload requires a real user; create one and grab a JWT.
    settings = load_settings()
    from hoaware.auth import create_access_token, hash_password
    with db.get_connection(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash, verified_at) VALUES (?, ?, datetime('now'))",
            ("upload@example.test", hash_password("password123")),
        )
        user_id = conn.execute("SELECT id FROM users WHERE email = ?", ("upload@example.test",)).fetchone()["id"]
        token, jti, expires = create_access_token(int(user_id))
        db.create_session(conn, user_id=int(user_id), token_jti=jti, expires_at=str(expires))

    # The async path must NOT invoke ingest_pdf_paths or the prepared GCS path.
    def _no_ingest(*args, **kwargs):
        raise AssertionError("async /upload must not call ingest_pdf_paths in-process")

    monkeypatch.setattr(api_main, "ingest_pdf_paths", _no_ingest)
    monkeypatch.setattr(api_main, "_ingest_uploaded_files", _no_ingest)

    pdf_bytes = _make_text_pdf("Async upload test")
    r = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "hoa": "Async Test HOA",
            "categories": "ccr",
            "text_extractable": "true",
        },
        files={"files": ("ccr.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["queued"] is True
    assert payload["job_id"] is not None
    assert payload["status_url"] == f"/ingest/status/{payload['job_id']}"

    # pending_ingest row exists in the expected shape.
    with db.get_connection(settings.db_path) as conn:
        row = db.get_pending_ingest(conn, payload["job_id"])
        assert row is not None
        assert row["status"] == "pending"
        assert row["bundle_uri"].startswith("local://")
        assert row["source"] == "upload"


def test_admin_ingest_ready_gcs_async_path_enqueues_without_running_docai(monkeypatch):
    """The flagged-on /admin/ingest-ready-gcs path becomes a pure enqueue."""
    settings = load_settings()
    pdf_bytes = _make_text_pdf("ignored - async path must not download PDF")
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    prefix = "v1/KS/johnson/example/bundle1"
    bucket = FakeBucket(objects={
        f"{prefix}/status.json": json.dumps({
            "status": "ready", "claimed_by": None, "claimed_at": None,
            "imported_at": None, "error": None,
        }),
        f"{prefix}/bundle.json": json.dumps(_bundle_payload(sha)),
        f"{prefix}/texts/{sha}.json": json.dumps({"pages": [{"number": 1, "text": "x"}], "docai_pages": 0}),
        f"{prefix}/docs/{sha}.pdf": pdf_bytes,
    })
    monkeypatch.setattr(api_main, "_prepared_gcs_bucket", lambda bucket_name=None: bucket)

    def _no_docai(*args, **kwargs):
        raise AssertionError("async /admin/ingest-ready-gcs path must not run any ingest body")

    monkeypatch.setattr(api_main, "ingest_pdf_paths", _no_docai)
    monkeypatch.setattr(api_main, "_process_prepared_bundle", _no_docai)

    r = client.post(
        "/admin/ingest-ready-gcs?state=KS&limit=1",
        headers={"Authorization": "Bearer test-secret-for-ci"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["async"] is True
    assert body["enqueued"] == 1
    assert len(body["job_ids"]) == 1

    with db.get_connection(settings.db_path) as conn:
        row = db.get_pending_ingest(conn, body["job_ids"][0])
        assert row is not None
        assert row["status"] == "pending"
        assert row["bundle_uri"].startswith("gs://")
        assert row["source"] == "ingest-ready-gcs"

    # The GCS-side status.json has been flipped to 'claimed' so duplicate
    # call wouldn't re-enqueue.
    status = json.loads(bucket.blob(f"{prefix}/status.json").download_as_bytes())
    assert status["status"] == "claimed"


def test_admin_ingest_ready_gcs_async_skips_duplicate_enqueue(monkeypatch):
    """Re-calling the flagged-on path with the same prefix must not double-enqueue."""
    settings = load_settings()
    pdf_bytes = _make_text_pdf("dedup")
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    prefix = "v1/KS/johnson/example/bundle1"
    bucket = FakeBucket(objects={
        f"{prefix}/status.json": json.dumps({
            "status": "ready", "claimed_by": None, "claimed_at": None,
            "imported_at": None, "error": None,
        }),
        f"{prefix}/bundle.json": json.dumps(_bundle_payload(sha)),
        f"{prefix}/texts/{sha}.json": json.dumps({"pages": [{"number": 1, "text": "x"}], "docai_pages": 0}),
        f"{prefix}/docs/{sha}.pdf": pdf_bytes,
    })
    monkeypatch.setattr(api_main, "_prepared_gcs_bucket", lambda bucket_name=None: bucket)

    r1 = client.post(
        "/admin/ingest-ready-gcs?state=KS&limit=1",
        headers={"Authorization": "Bearer test-secret-for-ci"},
    )
    assert r1.status_code == 200
    assert r1.json()["enqueued"] == 1

    # Reset the GCS-side status back to 'ready' so list_ready_bundle_prefixes
    # would surface it again, then call. Even though it surfaces, the
    # bundle_uri-keyed pending_ingest check should skip it.
    blob = bucket.blob(f"{prefix}/status.json")
    blob._data = json.dumps({
        "status": "ready", "claimed_by": None, "claimed_at": None,
        "imported_at": None, "error": None,
    }).encode("utf-8")
    blob.generation += 1

    r2 = client.post(
        "/admin/ingest-ready-gcs?state=KS&limit=1",
        headers={"Authorization": "Bearer test-secret-for-ci"},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["enqueued"] == 0
    assert any(item.get("reason") == "already_enqueued" for item in body2["skipped"])


def test_ingest_status_endpoint_returns_job_row(monkeypatch):
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.enqueue_pending_ingest(
            conn,
            job_id="status-test",
            bundle_uri="gs://b/p",
            state="FL",
            source="ingest-ready-gcs",
        )
    r = client.get("/ingest/status/status-test")
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == "status-test"
    assert body["status"] == "pending"
    assert body["state"] == "FL"
    assert body["source"] == "ingest-ready-gcs"


def test_ingest_status_returns_404_for_unknown():
    r = client.get("/ingest/status/not-a-job-id")
    assert r.status_code == 404


def test_admin_retry_dead_flips_dead_back_to_pending():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.enqueue_pending_ingest(conn, job_id="dead-1", bundle_uri="gs://b/p", state="FL", source="x")
        for _ in range(3):
            db.claim_next_pending_ingest(conn)
            db.mark_pending_ingest_failed(conn, "dead-1", error="boom", max_attempts=3)
        assert db.get_pending_ingest(conn, "dead-1")["status"] == "dead"

    r = client.post(
        "/admin/ingest/retry-dead",
        headers={"Authorization": "Bearer test-secret-for-ci"},
    )
    assert r.status_code == 200
    assert r.json()["reset_count"] == 1

    with db.get_connection(settings.db_path) as conn:
        assert db.get_pending_ingest(conn, "dead-1")["status"] == "pending"


def test_admin_queue_stats_returns_counts():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.enqueue_pending_ingest(conn, job_id="a", bundle_uri="gs://b/p1", state="FL", source="x")
        db.enqueue_pending_ingest(conn, job_id="b", bundle_uri="gs://b/p2", state="CA", source="x")
    r = client.get(
        "/admin/ingest/queue-stats",
        headers={"Authorization": "Bearer test-secret-for-ci"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["pending"] == 2
    states = {row["state"]: row["n"] for row in body["by_state"]}
    assert states["FL"] == 1
    assert states["CA"] == 1


def test_upload_sync_path_still_works_when_flag_off(monkeypatch):
    """With ASYNC_INGEST_ENABLED=0, /upload runs the legacy background-task path."""
    monkeypatch.setenv("ASYNC_INGEST_ENABLED", "0")
    settings = load_settings()
    from hoaware.auth import create_access_token, hash_password
    with db.get_connection(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash, verified_at) VALUES (?, ?, datetime('now'))",
            ("syncpath@example.test", hash_password("password123")),
        )
        user_id = conn.execute("SELECT id FROM users WHERE email = ?", ("syncpath@example.test",)).fetchone()["id"]
        token, jti, expires = create_access_token(int(user_id))
        db.create_session(conn, user_id=int(user_id), token_jti=jti, expires_at=str(expires))

    called = {"sync_ingest_invoked": False}

    def _fake_sync(hoa_name, saved_paths, metadata_by_path=None):
        called["sync_ingest_invoked"] = True

    monkeypatch.setattr(api_main, "_ingest_uploaded_files", _fake_sync)

    pdf_bytes = _make_text_pdf("Sync upload")
    r = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "hoa": "Sync Test HOA",
            "categories": "ccr",
            "text_extractable": "true",
        },
        files={"files": ("ccr.pdf", pdf_bytes, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["queued"] is True
    assert payload["job_id"] is None
    assert payload["status_url"] is None

    # BackgroundTasks should have invoked the sync ingest (TestClient runs them).
    assert called["sync_ingest_invoked"] is True
