"""Tests for Resident Proposals feature."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["HOA_DB_PATH"] = _tmp.name
os.environ["JWT_SECRET"] = "test-secret-proposals"
_tmp.close()

from api.main import app  # noqa: E402
from hoaware import db  # noqa: E402
from hoaware.config import load_settings  # noqa: E402

client = TestClient(app)

CLEANUP_TABLES = [
    "proposal_upvotes",
    "proposal_cosigners",
    "proposals",
    "membership_claims",
    "sessions",
    "users",
]


@pytest.fixture(autouse=True)
def _clean_db():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
        for table in CLEANUP_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
    yield


def _reg(email, display_name="Test"):
    r = client.post("/auth/register", json={"email": email, "password": "password1234", "display_name": display_name})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"Authorization": f"Bearer {data['token']}"}, data["user_id"]


def _setup_users_and_hoa():
    """Create 3 users, one HOA, memberships. Returns (h1, uid1, h2, uid2, h3, uid3, hoa_id)."""
    h1, uid1 = _reg("user1@example.com", "User One")
    h2, uid2 = _reg("user2@example.com", "User Two")
    h3, uid3 = _reg("user3@example.com", "User Three")
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, "Proposal Test HOA")
    for h in (h1, h2, h3):
        client.post(f"/user/hoas/{hoa_id}/claim", json={"unit_number": "101"}, headers=h)
    return h1, uid1, h2, uid2, h3, uid3, hoa_id


# ---------------------------------------------------------------------------
# Creation tests
# ---------------------------------------------------------------------------

def test_create_proposal_happy_path():
    h1, uid1, h2, uid2, h3, uid3, hoa_id = _setup_users_and_hoa()
    r = client.post("/proposals", json={
        "hoa_id": hoa_id,
        "title": "Add a dog park",
        "description": "A dedicated space for dogs would improve community wellness.",
        "category": "Amenities",
    }, headers=h1)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "private"
    assert data["share_code"] is not None
    assert len(data["share_code"]) == 4


def test_create_proposal_non_member_rejected():
    _, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    outsider, _ = _reg("outsider@example.com")
    r = client.post("/proposals", json={
        "hoa_id": hoa_id,
        "title": "Bad proposal",
        "description": "I am not a member of this HOA.",
    }, headers=outsider)
    assert r.status_code == 403


def test_create_proposal_bad_category():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    r = client.post("/proposals", json={
        "hoa_id": hoa_id,
        "title": "Something",
        "description": "With a bad category value.",
        "category": "NotACategory",
    }, headers=h1)
    assert r.status_code == 422


def test_create_proposal_title_too_short():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    r = client.post("/proposals", json={
        "hoa_id": hoa_id,
        "title": "Hi",
        "description": "Short title should fail.",
    }, headers=h1)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Share code tests
# ---------------------------------------------------------------------------

def test_share_code_format():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    r = client.post("/proposals", json={
        "hoa_id": hoa_id,
        "title": "Proposal with share code",
        "description": "Testing share code format and uniqueness.",
    }, headers=h1)
    code = r.json()["share_code"]
    assert len(code) == 4
    # No ambiguous chars
    assert not any(c in code for c in "0OI1")
    # All uppercase alphanumeric from our alphabet
    alphabet = set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
    assert all(c in alphabet for c in code)


def test_share_codes_unique():
    h1, _, h2, _, _, _, hoa_id = _setup_users_and_hoa()
    # Need to withdraw first proposal before creating second
    r1 = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Proposal One", "description": "First proposal text here.",
    }, headers=h1)
    # Withdraw so user can create another
    client.delete(f"/proposals/{r1.json()['id']}", headers=h1)
    r2 = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Proposal Two", "description": "Second proposal text here.",
    }, headers=h1)
    assert r1.json()["share_code"] != r2.json()["share_code"]


# ---------------------------------------------------------------------------
# One-active limit
# ---------------------------------------------------------------------------

def test_second_proposal_rejected_while_first_private():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "First proposal", "description": "Already active proposal.",
    }, headers=h1)
    r = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Second proposal", "description": "Should be rejected by one-active limit.",
    }, headers=h1)
    assert r.status_code == 409


def test_withdraw_frees_active_slot():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    r1 = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "First proposal", "description": "Will be withdrawn.",
    }, headers=h1)
    client.delete(f"/proposals/{r1.json()['id']}", headers=h1)
    r2 = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "New proposal", "description": "After withdrawing the first one.",
    }, headers=h1)
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# Co-sign tests
# ---------------------------------------------------------------------------

def test_cosign_happy_path():
    h1, _, h2, _, _, _, hoa_id = _setup_users_and_hoa()
    proposal = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Need co-signers", "description": "This proposal needs community support.",
    }, headers=h1).json()
    code = proposal["share_code"]
    r = client.post(f"/proposals/cosign/{code}", headers=h2)
    assert r.status_code == 200
    assert r.json()["cosigner_count"] == 1


def test_cosign_own_proposal_rejected():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    proposal = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "My idea", "description": "Cannot cosign my own proposal.",
    }, headers=h1).json()
    r = client.post(f"/proposals/cosign/{proposal['share_code']}", headers=h1)
    assert r.status_code == 403


def test_cosign_wrong_hoa_rejected():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    proposal = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "My idea", "description": "Cross-HOA cosign should fail.",
    }, headers=h1).json()
    outsider, _ = _reg("outsider2@example.com")
    r = client.post(f"/proposals/cosign/{proposal['share_code']}", headers=outsider)
    assert r.status_code == 404


def test_cosign_duplicate_rejected():
    h1, _, h2, _, _, _, hoa_id = _setup_users_and_hoa()
    proposal = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "My idea", "description": "Duplicate cosign should be rejected.",
    }, headers=h1).json()
    client.post(f"/proposals/cosign/{proposal['share_code']}", headers=h2)
    r = client.post(f"/proposals/cosign/{proposal['share_code']}", headers=h2)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Publication trigger
# ---------------------------------------------------------------------------

def test_two_cosigners_publishes_proposal():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    proposal = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Community garden", "description": "We should build a community garden.",
    }, headers=h1).json()
    code = proposal["share_code"]
    r1 = client.post(f"/proposals/cosign/{code}", headers=h2)
    assert r1.json()["status"] == "private"  # still 1 co-signer
    r2 = client.post(f"/proposals/cosign/{code}", headers=h3)
    assert r2.json()["status"] == "public"
    assert r2.json()["published_at"] is not None


def test_withdraw_cosigner_reverts_to_private():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    proposal = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Community garden", "description": "We should build a community garden.",
    }, headers=h1).json()
    code = proposal["share_code"]
    client.post(f"/proposals/cosign/{code}", headers=h2)
    client.post(f"/proposals/cosign/{code}", headers=h3)
    # Now withdraw one cosigner
    r = client.delete(f"/proposals/{proposal['id']}/cosign", headers=h3)
    assert r.status_code == 200
    p = client.get(f"/proposals/{proposal['id']}", headers=h1).json()
    assert p["status"] == "private"
    assert p["published_at"] is None


# ---------------------------------------------------------------------------
# Listing tests
# ---------------------------------------------------------------------------

def test_public_feed_excludes_private():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    # private proposal
    client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Private idea", "description": "Nobody cosigned this yet.",
    }, headers=h1)
    r = client.get(f"/hoas/{hoa_id}/proposals", headers=h2)
    assert r.status_code == 200
    assert len(r.json()) == 0


def test_public_feed_sorted_by_upvotes():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    # Create and publish two proposals
    p1 = _publish_proposal(h1, h2, h3, hoa_id, "Low upvotes", "First proposal here.")
    p2 = _publish_proposal(h3, h1, h2, hoa_id, "High upvotes", "Second proposal here.")
    # Upvote p2 with h1
    client.post(f"/proposals/{p2}/upvote", headers=h1)
    feed = client.get(f"/hoas/{hoa_id}/proposals", headers=h1).json()
    assert len(feed) == 2
    assert feed[0]["id"] == p2  # higher votes first


def test_archived_excluded_by_default():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    pid = _publish_proposal(h1, h2, h3, hoa_id, "Will be archived", "This will be withdrawn.")
    client.delete(f"/proposals/{pid}", headers=h1)
    r = client.get(f"/hoas/{hoa_id}/proposals", headers=h2)
    assert all(p["id"] != pid for p in r.json())


def test_archived_included_with_param():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    pid = _publish_proposal(h1, h2, h3, hoa_id, "Will be archived", "This will be withdrawn.")
    client.delete(f"/proposals/{pid}", headers=h1)
    r = client.get(f"/hoas/{hoa_id}/proposals?include_archived=true", headers=h2)
    assert any(p["id"] == pid for p in r.json())


def test_non_member_cannot_list():
    _, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    outsider, _ = _reg("outsider3@example.com")
    r = client.get(f"/hoas/{hoa_id}/proposals", headers=outsider)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Get single
# ---------------------------------------------------------------------------

def test_get_single_private_only_creator_sees_it():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    p = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Secret proposal", "description": "Only creator should see this.",
    }, headers=h1).json()
    # Creator can see
    r = client.get(f"/proposals/{p['id']}", headers=h1)
    assert r.status_code == 200
    # Other member (not cosigner) cannot see
    r2 = client.get(f"/proposals/{p['id']}", headers=h2)
    assert r2.status_code == 404


def test_get_single_author_name_not_in_public_response():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    pid = _publish_proposal(h1, h2, h3, hoa_id, "Public proposal", "This is a public proposal.")
    r = client.get(f"/proposals/{pid}", headers=h2)
    data = r.json()
    assert "creator_display_name" not in data
    assert "grantor_name" not in data


# ---------------------------------------------------------------------------
# My Proposals
# ---------------------------------------------------------------------------

def test_my_proposals_includes_share_code_for_private():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "My private proposal", "description": "This is my proposal.",
    }, headers=h1)
    r = client.get("/proposals/mine", headers=h1)
    assert r.status_code == 200
    proposals = r.json()
    assert len(proposals) == 1
    assert proposals[0]["share_code"] is not None
    assert proposals[0]["status"] == "private"


# ---------------------------------------------------------------------------
# Upvote tests
# ---------------------------------------------------------------------------

def test_upvote_happy_path():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    pid = _publish_proposal(h1, h2, h3, hoa_id, "Upvoteable", "Great proposal text here.")
    r = client.post(f"/proposals/{pid}/upvote", headers=h2)
    assert r.status_code == 200
    assert r.json()["upvote_count"] == 1


def test_upvote_on_private_rejected():
    h1, _, h2, _, _, _, hoa_id = _setup_users_and_hoa()
    p = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Still private", "description": "This proposal is still private.",
    }, headers=h1).json()
    r = client.post(f"/proposals/{p['id']}/upvote", headers=h2)
    assert r.status_code in (400, 403, 404)


def test_upvote_duplicate_rejected():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    pid = _publish_proposal(h1, h2, h3, hoa_id, "Vote twice", "Testing duplicate upvote rejection.")
    client.post(f"/proposals/{pid}/upvote", headers=h2)
    r = client.post(f"/proposals/{pid}/upvote", headers=h2)
    assert r.status_code == 409


def test_upvote_non_member_rejected():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    pid = _publish_proposal(h1, h2, h3, hoa_id, "Members only", "Non-member upvote test.")
    outsider, _ = _reg("outsider4@example.com")
    r = client.post(f"/proposals/{pid}/upvote", headers=outsider)
    assert r.status_code == 403


def test_withdraw_upvote_happy_path():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    pid = _publish_proposal(h1, h2, h3, hoa_id, "Withdraw upvote", "Testing upvote withdrawal.")
    client.post(f"/proposals/{pid}/upvote", headers=h2)
    r = client.delete(f"/proposals/{pid}/upvote", headers=h2)
    assert r.status_code == 200
    feed = client.get(f"/hoas/{hoa_id}/proposals", headers=h2).json()
    p = next(x for x in feed if x["id"] == pid)
    assert p["upvote_count"] == 0


def test_withdraw_upvote_not_found():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    pid = _publish_proposal(h1, h2, h3, hoa_id, "No upvote", "No upvote to withdraw.")
    r = client.delete(f"/proposals/{pid}/upvote", headers=h2)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Withdraw proposal tests
# ---------------------------------------------------------------------------

def test_withdraw_proposal_happy_path():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    p = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Will withdraw", "description": "This proposal will be withdrawn.",
    }, headers=h1).json()
    r = client.delete(f"/proposals/{p['id']}", headers=h1)
    assert r.status_code == 200


def test_withdraw_proposal_non_creator_rejected():
    h1, _, h2, _, _, _, hoa_id = _setup_users_and_hoa()
    p = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Someone else", "description": "Non-creator withdrawal test.",
    }, headers=h1).json()
    r = client.delete(f"/proposals/{p['id']}", headers=h2)
    assert r.status_code == 403


def test_withdraw_already_archived_rejected():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    p = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Already archived", "description": "This will be archived twice.",
    }, headers=h1).json()
    client.delete(f"/proposals/{p['id']}", headers=h1)
    r = client.delete(f"/proposals/{p['id']}", headers=h1)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Archive sweep
# ---------------------------------------------------------------------------

def test_archive_sweep():
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa_id = db.get_or_create_hoa(conn, "Sweep Test HOA")
        u1 = db.create_user(conn, email="sweep1@example.com", password_hash="x")
        u2 = db.create_user(conn, email="sweep2@example.com", password_hash="x")
        db.create_membership_claim(conn, user_id=u1, hoa_id=hoa_id)
        db.create_membership_claim(conn, user_id=u2, hoa_id=hoa_id)
        # Old published proposal (61 days ago)
        pid_old = db.create_proposal(conn, hoa_id=hoa_id, creator_user_id=u1,
                                     title="Old", description="Old proposal")
        conn.execute(
            "UPDATE proposals SET status='public', published_at=datetime('now', '-61 days') WHERE id=?",
            (pid_old,)
        )
        # Recent published proposal
        pid_new = db.create_proposal(conn, hoa_id=hoa_id, creator_user_id=u2,
                                     title="New", description="New proposal")
        conn.execute(
            "UPDATE proposals SET status='public', published_at=datetime('now', '-5 days') WHERE id=?",
            (pid_new,)
        )
        conn.commit()
        count = db.archive_stale_proposals(conn, days=60)
        assert count == 1
        old = db.get_proposal(conn, pid_old)
        new = db.get_proposal(conn, pid_new)
        assert old["status"] == "archived"
        assert new["status"] == "public"


# ---------------------------------------------------------------------------
# Isolation tests
# ---------------------------------------------------------------------------

def test_different_hoa_cannot_see_proposals():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    # Create a second HOA with a different member
    other_user, _ = _reg("other@example.com")
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa2 = db.get_or_create_hoa(conn, "Other HOA")
    client.post(f"/user/hoas/{hoa2}/claim", json={}, headers=other_user)
    # Create proposal in HOA 1
    p = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "HOA 1 proposal", "description": "Only for HOA 1 members.",
    }, headers=h1).json()
    # Other HOA member cannot access it
    r = client.get(f"/proposals/{p['id']}", headers=other_user)
    assert r.status_code in (403, 404)


def test_share_code_from_other_hoa_returns_404():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    p = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "HOA 1 proposal", "description": "Only for HOA 1 members.",
    }, headers=h1).json()
    # User from different HOA
    other_user, _ = _reg("other2@example.com")
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        hoa2 = db.get_or_create_hoa(conn, "Other HOA 2")
    client.post(f"/user/hoas/{hoa2}/claim", json={}, headers=other_user)
    r = client.post(f"/proposals/cosign/{p['share_code']}", headers=other_user)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Location tests
# ---------------------------------------------------------------------------

def test_create_proposal_with_location():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    r = client.post("/proposals", json={
        "hoa_id": hoa_id,
        "title": "Fix the broken sidewalk",
        "description": "The sidewalk on the north side is cracked and a tripping hazard.",
        "lat": 35.7796,
        "lng": -78.6382,
        "location_description": "North sidewalk near building 4",
    }, headers=h1)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["lat"] == pytest.approx(35.7796)
    assert data["lng"] == pytest.approx(-78.6382)
    assert data["location_description"] == "North sidewalk near building 4"


def test_create_proposal_without_location():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    r = client.post("/proposals", json={
        "hoa_id": hoa_id,
        "title": "Paint the clubhouse",
        "description": "The clubhouse exterior paint is peeling badly.",
    }, headers=h1)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["lat"] is None
    assert data["lng"] is None
    assert data["location_description"] is None


def test_create_proposal_partial_coords_rejected():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    r = client.post("/proposals", json={
        "hoa_id": hoa_id,
        "title": "Partial location test",
        "description": "Only lat provided, no lng — should fail.",
        "lat": 35.7796,
    }, headers=h1)
    assert r.status_code == 422


def test_create_proposal_invalid_lat_rejected():
    h1, _, _, _, _, _, hoa_id = _setup_users_and_hoa()
    r = client.post("/proposals", json={
        "hoa_id": hoa_id,
        "title": "Bad coordinates test",
        "description": "Latitude out of range should be rejected.",
        "lat": 999.0,
        "lng": -78.6382,
    }, headers=h1)
    assert r.status_code == 422


def test_location_preserved_in_feed():
    h1, _, h2, _, h3, _, hoa_id = _setup_users_and_hoa()
    r = client.post("/proposals", json={
        "hoa_id": hoa_id,
        "title": "Location in feed test",
        "description": "This proposal has a location that should appear in the public feed.",
        "lat": 35.78,
        "lng": -78.64,
        "location_description": "Corner of Cary Pkwy and Appomattox",
    }, headers=h1)
    code = r.json()["share_code"]
    client.post(f"/proposals/cosign/{code}", headers=h2)
    client.post(f"/proposals/cosign/{code}", headers=h3)
    feed = client.get(f"/hoas/{hoa_id}/proposals", headers=h2).json()
    assert len(feed) == 1
    assert feed[0]["lat"] == pytest.approx(35.78)
    assert feed[0]["lng"] == pytest.approx(-78.64)
    assert feed[0]["location_description"] == "Corner of Cary Pkwy and Appomattox"


def test_cosign_public_proposal_by_id():
    """A 4th member can co-sign a public proposal by ID (named support)."""
    h1, uid1, h2, uid2, h3, uid3, hoa_id = _setup_users_and_hoa()
    h4, uid4 = _reg("user4@example.com", "User Four")
    client.post(f"/user/hoas/{hoa_id}/claim", json={"unit_number": "104"}, headers=h4)

    # Create and publish via share-code cosigning
    p = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Public cosign test", "description": "Testing named support on public proposals.",
    }, headers=h1).json()
    code = p["share_code"]
    client.post(f"/proposals/cosign/{code}", headers=h2)
    client.post(f"/proposals/cosign/{code}", headers=h3)

    # Now co-sign the public proposal by ID
    r = client.post(f"/proposals/{p['id']}/cosign", headers=h4)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["user_cosigned"] is True
    assert data["cosigner_count"] == 3
    assert "User Four" in data["cosigners"]

    # Duplicate should be rejected
    r2 = client.post(f"/proposals/{p['id']}/cosign", headers=h4)
    assert r2.status_code == 409

    # Withdraw co-signature
    r3 = client.delete(f"/proposals/{p['id']}/cosign", headers=h4)
    assert r3.status_code == 200

    # Verify cosigner list updated
    r4 = client.get(f"/proposals/{p['id']}", headers=h4)
    assert r4.json()["cosigner_count"] == 2
    assert "User Four" not in r4.json()["cosigners"]


def test_cosign_public_proposal_creator_rejected():
    """Creator cannot co-sign their own public proposal."""
    h1, uid1, h2, uid2, h3, uid3, hoa_id = _setup_users_and_hoa()
    p = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Own cosign test", "description": "Creator should not be able to cosign own proposal.",
    }, headers=h1).json()
    code = p["share_code"]
    client.post(f"/proposals/cosign/{code}", headers=h2)
    client.post(f"/proposals/cosign/{code}", headers=h3)

    r = client.post(f"/proposals/{p['id']}/cosign", headers=h1)
    assert r.status_code == 403


def test_cosign_public_proposal_non_member_rejected():
    """Non-member cannot co-sign a public proposal."""
    h1, uid1, h2, uid2, h3, uid3, hoa_id = _setup_users_and_hoa()
    outsider, _ = _reg("outsider@example.com", "Outsider")

    p = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Non-member cosign test", "description": "Outsiders should be blocked from cosigning.",
    }, headers=h1).json()
    code = p["share_code"]
    client.post(f"/proposals/cosign/{code}", headers=h2)
    client.post(f"/proposals/cosign/{code}", headers=h3)

    r = client.post(f"/proposals/{p['id']}/cosign", headers=outsider)
    assert r.status_code == 403


def test_cosign_private_proposal_by_id_rejected():
    """Cannot co-sign a private proposal by ID (must use share code)."""
    h1, uid1, h2, uid2, h3, uid3, hoa_id = _setup_users_and_hoa()
    p = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": "Private cosign test", "description": "Should require share code for private proposals.",
    }, headers=h1).json()

    r = client.post(f"/proposals/{p['id']}/cosign", headers=h2)
    assert r.status_code == 400


def _publish_proposal(creator_h, cosigner1_h, cosigner2_h, hoa_id, title, description):
    """Create a proposal and get it to 'public' status with 2 co-signers."""
    p = client.post("/proposals", json={
        "hoa_id": hoa_id, "title": title, "description": description,
    }, headers=creator_h).json()
    code = p["share_code"]
    client.post(f"/proposals/cosign/{code}", headers=cosigner1_h)
    client.post(f"/proposals/cosign/{code}", headers=cosigner2_h)
    return p["id"]
