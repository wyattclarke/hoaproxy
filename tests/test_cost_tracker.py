"""Tests for the cost tracker: DB functions and admin endpoints."""

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
        # Clean up between tests
        conn.execute("DELETE FROM api_usage_log")
        conn.execute("DELETE FROM fixed_costs")
        conn.commit()
    yield


# ---------------------------------------------------------------------------
# DB-level tests
# ---------------------------------------------------------------------------


def test_log_api_usage():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        row_id = db.log_api_usage(
            conn,
            service="openai_embedding",
            operation="embed",
            units=1500,
            unit_type="tokens",
            est_cost_usd=0.00003,
            metadata={"model": "text-embedding-3-small"},
        )
        assert row_id > 0


def test_get_usage_summary():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.log_api_usage(conn, service="openai_embedding", operation="embed",
                         units=1000, unit_type="tokens", est_cost_usd=0.00002)
        db.log_api_usage(conn, service="openai_embedding", operation="embed",
                         units=2000, unit_type="tokens", est_cost_usd=0.00004)
        db.log_api_usage(conn, service="openai_chat", operation="chat_completion",
                         units=500, unit_type="tokens", est_cost_usd=0.001)
        summary = db.get_usage_summary(conn)
    assert len(summary) == 2
    embed_row = next(r for r in summary if r["service"] == "openai_embedding")
    assert embed_row["total_units"] == 3000


def test_get_usage_daily():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.log_api_usage(conn, service="openai_chat", operation="chat_completion",
                         units=100, unit_type="tokens", est_cost_usd=0.001)
        rows = db.get_usage_daily(conn)
    assert len(rows) >= 1
    assert rows[0]["service"] == "openai_chat"


def test_fixed_cost_crud():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        cost_id = db.create_fixed_cost(
            conn, service="render", description="Starter Plus", amount_usd=25.0
        )
        assert cost_id > 0

        costs = db.list_fixed_costs(conn)
        assert len(costs) == 1
        assert costs[0]["service"] == "render"
        assert costs[0]["monthly_equiv"] == 25.0

        updated = db.update_fixed_cost(conn, cost_id, amount_usd=35.0)
        assert updated["monthly_equiv"] == 35.0

        db.delete_fixed_cost(conn, cost_id)
        active = db.list_fixed_costs(conn, active_only=True)
        assert len(active) == 0
        all_costs = db.list_fixed_costs(conn, active_only=False)
        assert len(all_costs) == 1
        assert all_costs[0]["active"] == 0


def test_fixed_cost_yearly_conversion():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        cost_id = db.create_fixed_cost(
            conn, service="cloudflare", description="Domain",
            amount_usd=10.0, frequency="yearly",
        )
        costs = db.list_fixed_costs(conn)
        assert costs[0]["monthly_equiv"] == 0.83


# ---------------------------------------------------------------------------
# Admin endpoint tests
# ---------------------------------------------------------------------------


def test_admin_costs_requires_auth():
    resp = client.get("/admin/costs")
    assert resp.status_code == 403


def test_admin_costs_summary():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.log_api_usage(conn, service="openai_embedding", operation="embed",
                         units=1000, unit_type="tokens", est_cost_usd=0.00002)
        db.create_fixed_cost(conn, service="render", description="Starter",
                             amount_usd=25.0)

    resp = client.get("/admin/costs", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert "metered" in data
    assert "fixed" in data
    assert data["total_fixed_usd"] == 25.0
    assert data["total_usd"] >= 25.0
    assert "openai_embedding" in data["metered"]


def test_admin_costs_daily():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        db.log_api_usage(conn, service="openai_chat", operation="chat_completion",
                         units=500, unit_type="tokens", est_cost_usd=0.001)

    resp = client.get("/admin/costs/daily", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["daily"]) >= 1


def test_admin_fixed_cost_crud():
    # Create
    resp = client.post("/admin/costs/fixed", headers=ADMIN_HEADERS, json={
        "service": "render",
        "description": "Starter Plus",
        "amount_usd": 25.0,
        "frequency": "monthly",
    })
    assert resp.status_code == 200
    cost_id = resp.json()["id"]

    # List
    resp = client.get("/admin/costs/fixed", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert len(resp.json()["fixed_costs"]) == 1

    # Update
    resp = client.put(f"/admin/costs/fixed/{cost_id}", headers=ADMIN_HEADERS, json={
        "amount_usd": 35.0,
    })
    assert resp.status_code == 200
    assert resp.json()["monthly_equiv"] == 35.0

    # Delete (deactivate)
    resp = client.delete(f"/admin/costs/fixed/{cost_id}", headers=ADMIN_HEADERS)
    assert resp.status_code == 200

    # Verify deactivated
    resp = client.get("/admin/costs/fixed", headers=ADMIN_HEADERS)
    assert len(resp.json()["fixed_costs"]) == 0


def test_admin_costs_with_month_filter():
    resp = client.get("/admin/costs?month=2099-01", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["period"] == "2099-01"
    assert data["total_metered_usd"] == 0


def test_cost_tracker_log_helpers():
    """Verify the convenience wrappers in cost_tracker.py log to the DB."""
    from hoaware.cost_tracker import log_embedding_usage, log_chat_usage, log_email_usage

    log_embedding_usage(5000, model="text-embedding-3-small")
    log_chat_usage(1000, 200, model="gpt-5-mini")
    log_email_usage("resend", recipient_count=1)

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        summary = db.get_usage_summary(conn)

    services = {r["service"] for r in summary}
    assert "openai_embedding" in services
    assert "openai_chat" in services
    assert "resend" in services
