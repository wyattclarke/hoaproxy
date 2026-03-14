"""Tests for Milestone 3: Proxy Form Template Engine."""

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
from hoaware.proxy_templates import render_proxy_form  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def _setup_db():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
    yield


def _seed_proxy_rules(jurisdiction, community_type="hoa"):
    """Seed some proxy rules for testing."""
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        # Create a legal source first
        source_id = db.upsert_legal_source(
            conn,
            jurisdiction=jurisdiction,
            community_type=community_type,
            entity_form="unknown",
            governing_law_bucket="proxy_voting",
            source_type="statute",
            citation=f"{jurisdiction} Proxy Statute",
            citation_url=f"https://example.com/{jurisdiction}/proxy",
        )

        rules = [
            {"rule_type": "proxy_allowed", "value_text": "Yes, proxy voting is permitted.",
             "citation": f"{jurisdiction} Code § 1234"},
            {"rule_type": "proxy_electronic_assignment_allowed", "value_text": "Yes, electronic assignment is permitted.",
             "citation": f"{jurisdiction} Code § 1234(b)"},
            {"rule_type": "proxy_validity_duration", "value_text": "11 months from the date of execution.",
             "citation": f"{jurisdiction} Code § 1235"},
            {"rule_type": "proxy_directed_option", "value_text": "Grantor may specify directed voting instructions.",
             "citation": f"{jurisdiction} Code § 1236"},
        ]

        if jurisdiction == "CA":
            rules.append({"rule_type": "proxy_holder_restrictions",
                          "value_text": "Proxy holder must be a member of the association.",
                          "citation": "Cal. Civ. Code § 5130"})

        if jurisdiction == "FL":
            rules.append({"rule_type": "proxy_form_requirement",
                          "value_text": "Proxy must be in writing and signed by the unit owner.",
                          "citation": "Fla. Stat. § 718.112(2)(b)"})

        db.replace_legal_rules_for_scope(
            conn,
            jurisdiction=jurisdiction,
            community_type=community_type,
            entity_form="unknown",
            topic_family="proxy_voting",
            rules=[{**r, "source_id": source_id} for r in rules],
        )


def test_render_basic_form():
    html = render_proxy_form(
        "XX",
        grantor_name="Jane Doe",
        delegate_name="Bob Smith",
        hoa_name="Test HOA",
    )
    assert "Jane Doe" in html
    assert "Bob Smith" in html
    assert "Test HOA" in html
    assert "Proxy Authorization Form" in html
    assert "record owner(s)" in html
    assert "Signature of owner or authorized voter" in html


def test_render_directed_proxy():
    html = render_proxy_form(
        "XX",
        direction="directed",
        voting_instructions=[
            {"agenda_item": "Budget Approval", "vote": "For", "notes": "With amendments"},
        ],
    )
    assert "General (Undirected) Proxy" in html
    assert "Signature of owner or authorized voter" in html


def test_render_undirected_proxy():
    html = render_proxy_form("XX", direction="undirected")
    assert "General (Undirected) Proxy" in html


def test_render_with_ca_rules():
    _seed_proxy_rules("CA")
    html = render_proxy_form(
        "CA",
        grantor_name="Alice",
        delegate_name="Bob",
        hoa_name="Sunset HOA",
        meeting_date="2026-06-01",
    )
    assert "CA" in html
    assert "Proxy holder must be a member" in html
    assert "11 month" in html or "2026" in html


def test_render_with_fl_rules():
    _seed_proxy_rules("FL")
    html = render_proxy_form(
        "FL",
        grantor_name="Charlie",
        delegate_name="Diana",
        hoa_name="Palm HOA",
    )
    assert "FL" in html
    assert "writing and signed" in html


def test_render_with_tx_rules():
    _seed_proxy_rules("TX")
    html = render_proxy_form(
        "TX",
        grantor_name="Eve",
        delegate_name="Frank",
        hoa_name="Lone Star HOA",
        meeting_date="2026-07-15",
    )
    assert "TX" in html
    assert "electronic" in html.lower()


def test_render_with_co_rules():
    _seed_proxy_rules("CO")
    html = render_proxy_form("CO", grantor_name="Grace", delegate_name="Hank")
    assert "CO" in html


def test_render_with_va_rules():
    _seed_proxy_rules("VA")
    html = render_proxy_form("VA", grantor_name="Ivy", delegate_name="Jack")
    assert "VA" in html


def test_preview_endpoint():
    resp = client.get("/proxy-templates/preview?jurisdiction=XX&community_type=hoa")
    assert resp.status_code == 200
    assert "Proxy Authorization Form" in resp.text


def test_render_generic_form_includes_core_proxy_sections():
    _seed_proxy_rules("AZ")
    html = render_proxy_form(
        "AZ",
        grantor_name="Jordan Owner",
        grantor_unit="123 Main St",
        delegate_name="Casey Neighbor",
        hoa_name="Parkway Community Association",
        direction="undirected",
    )
    assert "Parkway Community Association" in html
    assert "general proxy until this proxy expires or is revoked" in html
    assert "Revocation." in html
    assert "General (Undirected) Proxy" in html
