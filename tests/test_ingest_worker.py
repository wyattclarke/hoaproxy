"""Phase 2 ingest worker tests."""

import json
import time

import pytest

from hoaware import db
from hoaware.config import load_settings


@pytest.fixture(autouse=True)
def _reset_db(monkeypatch, tmp_path):
    monkeypatch.setenv("HOA_DB_PATH", str(tmp_path / "worker.db"))
    monkeypatch.setenv("HOA_DOCS_ROOT", str(tmp_path / "docs"))
    monkeypatch.setenv("JWT_SECRET", "test-secret-for-ci")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
        conn.commit()


def test_pending_ingest_schema_creates_table_and_indexes():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        # Smoke-test: the table exists and the indexes are in place.
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_ingest'"
        ).fetchall()
        assert rows
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='pending_ingest'"
        ).fetchall()
        names = {r["name"] for r in idx}
        assert "idx_pending_ingest_status_enqueued" in names


def test_enqueue_and_claim_round_trip():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.enqueue_pending_ingest(
            conn,
            job_id="job-1",
            bundle_uri="gs://hoaproxy-ingest-ready/v1/KS/x/y/z",
            state="KS",
            source="ingest-ready-gcs",
        )
        row = db.claim_next_pending_ingest(conn)
        assert row is not None
        assert row["job_id"] == "job-1"
        assert row["status"] == "in_progress"
        assert row["attempts"] == 1
        assert row["started_at"] is not None
        # No more pending rows.
        assert db.claim_next_pending_ingest(conn) is None


def test_enqueue_is_idempotent_on_job_id():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        for _ in range(3):
            db.enqueue_pending_ingest(
                conn,
                job_id="dup",
                bundle_uri="gs://b/p",
                state="FL",
                source="ingest-ready-gcs",
            )
        count = conn.execute("SELECT COUNT(*) AS n FROM pending_ingest").fetchone()
        assert count["n"] == 1


def test_mark_failed_re_enqueues_until_max_attempts():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.enqueue_pending_ingest(
            conn,
            job_id="retry",
            bundle_uri="gs://b/p",
            state="FL",
            source="ingest-ready-gcs",
        )
        # First failure → still retryable (attempts=1, max=3 → re-enqueue).
        db.claim_next_pending_ingest(conn)
        status = db.mark_pending_ingest_failed(conn, "retry", error="boom", max_attempts=3)
        assert status == "pending"
        row = db.get_pending_ingest(conn, "retry")
        assert row["status"] == "pending"
        assert row["error"] == "boom"

        # Second failure (attempts=2).
        db.claim_next_pending_ingest(conn)
        db.mark_pending_ingest_failed(conn, "retry", error="boom", max_attempts=3)

        # Third failure (attempts=3) → dead.
        db.claim_next_pending_ingest(conn)
        status = db.mark_pending_ingest_failed(conn, "retry", error="boom", max_attempts=3)
        assert status == "dead"
        row = db.get_pending_ingest(conn, "retry")
        assert row["status"] == "dead"


def test_reset_dead_pending_ingest_flips_to_pending():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.enqueue_pending_ingest(
            conn,
            job_id="dead-1",
            bundle_uri="gs://b/p",
            state="FL",
            source="ingest-ready-gcs",
        )
        # Burn through max_attempts.
        for _ in range(3):
            db.claim_next_pending_ingest(conn)
            db.mark_pending_ingest_failed(conn, "dead-1", error="x", max_attempts=3)
        assert db.get_pending_ingest(conn, "dead-1")["status"] == "dead"

        n = db.reset_dead_pending_ingest(conn)
        assert n == 1
        assert db.get_pending_ingest(conn, "dead-1")["status"] == "pending"


def test_claim_order_is_oldest_first():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.enqueue_pending_ingest(conn, job_id="a", bundle_uri="gs://b/p1", state="FL", source="x")
        time.sleep(0.01)
        db.enqueue_pending_ingest(conn, job_id="b", bundle_uri="gs://b/p2", state="FL", source="x")
        first = db.claim_next_pending_ingest(conn)
        assert first["job_id"] == "a"
        second = db.claim_next_pending_ingest(conn)
        assert second["job_id"] == "b"


def test_count_pending_by_status():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.enqueue_pending_ingest(conn, job_id="a", bundle_uri="gs://b/p1", state="FL", source="x")
        db.enqueue_pending_ingest(conn, job_id="b", bundle_uri="gs://b/p2", state="FL", source="x")
        db.enqueue_pending_ingest(conn, job_id="c", bundle_uri="gs://b/p3", state="FL", source="x")
        db.claim_next_pending_ingest(conn)  # a → in_progress
        db.mark_pending_ingest_done(conn, "a", result={"ok": True})
        counts = db.count_pending_ingest_by_status(conn)
        assert counts.get("done") == 1
        assert counts.get("pending") == 2


def test_mark_done_persists_result_json():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.enqueue_pending_ingest(conn, job_id="x", bundle_uri="gs://b/p1", state="FL", source="x")
        db.claim_next_pending_ingest(conn)
        db.mark_pending_ingest_done(conn, "x", result={"hoa": "Foo", "indexed": 5})
        row = db.get_pending_ingest(conn, "x")
        assert row["status"] == "done"
        decoded = json.loads(row["result_json"])
        assert decoded["hoa"] == "Foo"
        assert decoded["indexed"] == 5


def test_ingest_worker_processes_local_upload_sidecar(monkeypatch, tmp_path):
    """Worker drains a `local://` URI by invoking _process_local_upload_sidecar."""
    from hoaware import ingest_worker

    settings = load_settings()
    sidecar_dir = settings.docs_root / ".pending_ingest"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = settings.docs_root / "Example HOA" / "ccr.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.0\n")
    sidecar_path = sidecar_dir / "abcdef.json"
    sidecar_path.write_text(json.dumps({
        "job_id": "abcdef",
        "hoa_name": "Example HOA",
        "files": [{
            "path": str(pdf_path.resolve()),
            "metadata": {
                "category": "ccr",
                "text_extractable": True,
                "source_url": None,
                "pre_extracted_pages": [{"number": 1, "text": "Declaration of Covenants"}],
                "docai_pages": 0,
            },
        }],
    }))

    called = {}

    def fake_local_processor(path, *, settings):
        called["path"] = path
        return {"status": "imported", "hoa": "Example HOA", "indexed": 1}

    def fake_prepared_processor(prefix, **kw):
        raise AssertionError("prepared bundle processor should not run for local URIs")

    monkeypatch.setattr(
        ingest_worker,
        "_import_processors",
        lambda: {"gs": fake_prepared_processor, "local": fake_local_processor},
    )

    with db.get_connection(settings.db_path) as conn:
        db.enqueue_pending_ingest(
            conn,
            job_id="abcdef",
            bundle_uri=f"local://{sidecar_path.resolve()}",
            state="??",
            source="upload",
        )
        row = db.claim_next_pending_ingest(conn)

    result = ingest_worker.process_job(
        row, settings=settings, processors=ingest_worker._import_processors()
    )
    assert called["path"] == sidecar_path.resolve()
    assert result["status"] == "imported"


def test_ingest_worker_rejects_unknown_scheme():
    from hoaware import ingest_worker

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.enqueue_pending_ingest(
            conn, job_id="bad", bundle_uri="ftp://example.com/x", state="FL", source="x"
        )
        row = db.claim_next_pending_ingest(conn)
    with pytest.raises(ValueError, match="unsupported"):
        ingest_worker.process_job(
            row,
            settings=settings,
            processors={"gs": lambda *a, **k: None, "local": lambda *a, **k: None},
        )
