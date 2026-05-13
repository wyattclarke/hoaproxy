"""SSR-injected SEO content on HOA profile pages.

Validates the unique body content + structured data we inject in
`_render_hoa_page()` so each HOA profile is distinct to Googlebot
instead of an identical JS shell.
"""

import json
import os
import re
import tempfile
from pathlib import Path

import pytest

# Module-level temp DB so the FastAPI app + db helpers see the same path.
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
_TMP_PATH = Path(_TMP.name)
os.environ["HOA_DB_PATH"] = str(_TMP_PATH)

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from hoaware import db  # noqa: E402


@pytest.fixture(autouse=True)
def _force_db_path():
    # Other test modules set HOA_DB_PATH at import time; pytest runs them in
    # alphabetical order, so by the time these tests run env may point at a
    # different temp DB. Re-pin per test so route handlers (which call
    # load_settings() at request time) read the DB this module seeded.
    prev = os.environ.get("HOA_DB_PATH")
    os.environ["HOA_DB_PATH"] = str(_TMP_PATH)
    yield
    if prev is None:
        os.environ.pop("HOA_DB_PATH", None)
    else:
        os.environ["HOA_DB_PATH"] = prev


@pytest.fixture(scope="module")
def client():
    # Pin env for the seeding step too; module-scope fixture runs before the
    # autouse function-scope fixture above on first call.
    prev = os.environ.get("HOA_DB_PATH")
    os.environ["HOA_DB_PATH"] = str(_TMP_PATH)
    try:
        with db.get_connection(_TMP_PATH) as conn:
            conn.executescript(db.SCHEMA)

            # Seed an HOA in San Marcos, TX with three documents in two categories.
            hoa_id = db.get_or_create_hoa(conn, "Blanco Vista Residential Owners Association, Inc.")
            db.upsert_hoa_location(
                conn,
                "Blanco Vista Residential Owners Association, Inc.",
                street="123 Main St",
                city="San Marcos",
                state="TX",
                postal_code="78666",
                country="US",
            )
            db.upsert_document(conn, hoa_id, "ccr.pdf", "sha-1", 1000, 10, category="ccr")
            db.upsert_document(conn, hoa_id, "ccr_2.pdf", "sha-2", 1000, 10, category="ccr")
            db.upsert_document(conn, hoa_id, "bylaws.pdf", "sha-3", 1000, 5, category="bylaws")

        yield TestClient(app)
    finally:
        _TMP_PATH.unlink(missing_ok=True)
        if prev is None:
            os.environ.pop("HOA_DB_PATH", None)
        else:
            os.environ["HOA_DB_PATH"] = prev


def test_profile_page_has_unique_body_content(client):
    resp = client.get("/hoa/tx/san-marcos/blanco-vista-residential-owners-association-inc")
    assert resp.status_code == 200
    body = resp.text

    # The promoted H1 is the HOA name (not the brand)
    assert '<h1 class="hoa-title" id="hoaTitle">Blanco Vista' in body

    # Visible body sentence — no longer requires JS
    assert "is a homeowners association in San Marcos, TX" in body
    # Mailing address rendered when street/postal present
    assert "123 Main St" in body and "78666" in body
    # Document inventory phrase reflects categories
    assert "2 CC&Rs" in body
    assert "set of bylaws" in body or "bylaws" in body


def test_profile_page_emits_breadcrumb_and_faq_jsonld(client):
    resp = client.get("/hoa/tx/san-marcos/blanco-vista-residential-owners-association-inc")
    assert resp.status_code == 200
    blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>',
        resp.text,
        re.DOTALL,
    )
    types = []
    by_type: dict[str, dict] = {}
    for raw in blocks:
        parsed = json.loads(raw)
        types.append(parsed.get("@type"))
        by_type[parsed.get("@type")] = parsed

    assert {"Organization", "BreadcrumbList", "FAQPage"} <= set(types)

    # BreadcrumbList: HOAproxy → Texas → San Marcos → HOA name
    crumbs = by_type["BreadcrumbList"]["itemListElement"]
    assert [c["name"] for c in crumbs] == [
        "HOAproxy",
        "Texas",
        "San Marcos",
        "Blanco Vista Residential Owners Association, Inc.",
    ]

    # FAQPage: three questions, each with an answer string
    faqs = by_type["FAQPage"]["mainEntity"]
    assert len(faqs) >= 3
    for q in faqs:
        assert q["@type"] == "Question"
        assert isinstance(q["acceptedAnswer"]["text"], str)
        assert len(q["acceptedAnswer"]["text"]) > 10


def test_profile_page_when_no_documents(client):
    """Empty-doc HOAs still get a unique SSR body (different copy from populated case)."""
    with db.get_connection(_TMP_PATH) as conn:
        db.get_or_create_hoa(conn, "Empty HOA Test")
        db.upsert_hoa_location(
            conn, "Empty HOA Test", city="Austin", state="TX",
        )
    resp = client.get("/hoa/tx/austin/empty-hoa-test")
    assert resp.status_code == 200
    # Invitation framing instead of zero-quantity admission
    assert "No governing documents have been uploaded" in resp.text
    assert "becomes searchable for everyone" in resp.text
    # Docless pages must be noindex,follow so they stay out of SERPs but
    # remain reachable for users coming in via the city index.
    assert '<meta name="robots" content="noindex,follow">' in resp.text
    # FAQ + breadcrumb still emitted
    assert "BreadcrumbList" in resp.text
    assert "FAQPage" in resp.text


def test_profile_page_with_documents_is_indexable(client):
    """HOAs with at least one document must NOT carry the noindex meta."""
    resp = client.get("/hoa/tx/san-marcos/blanco-vista-residential-owners-association-inc")
    assert resp.status_code == 200
    assert "noindex" not in resp.text


def test_profile_title_omits_brand_suffix(client):
    """Title is `{HOA} | {City}, {ST}` — no `| HOAproxy` tail to truncate."""
    resp = client.get("/hoa/tx/san-marcos/blanco-vista-residential-owners-association-inc")
    body = resp.text
    # Extract the <title> contents
    m = re.search(r"<title>([^<]+)</title>", body)
    assert m is not None
    title = m.group(1)
    assert title.endswith(", TX"), f"unexpected title tail: {title!r}"
    assert "HOAproxy" not in title


def test_state_index_has_intro_and_metros_and_jsonld(client):
    """Texas state index renders intro copy, metro grouping, top HOAs, JSON-LD."""
    # Seed enough TX cities to trigger metro grouping, plus extra docs so a
    # "Top HOAs" list materializes with >= 3 entries.
    with db.get_connection(_TMP_PATH) as conn:
        # Add a couple more TX HOAs in DFW + Houston so multiple metros populate.
        for name, city, n_docs in [
            ("Dallas Heights HOA", "Dallas", 5),
            ("Plano Park HOA", "Plano", 4),
            ("Houston Bayou HOA", "Houston", 6),
            ("Katy Crossing HOA", "Katy", 2),
        ]:
            hoa_id = db.get_or_create_hoa(conn, name)
            db.upsert_hoa_location(conn, name, city=city, state="TX")
            for i in range(n_docs):
                db.upsert_document(
                    conn, hoa_id, f"doc{i}.pdf", f"sha-{name}-{i}", 100, 1,
                    category="ccr",
                )

    resp = client.get("/hoa/tx/")
    assert resp.status_code == 200
    body = resp.text

    # State-specific intro paragraph (Texas has a curated entry) — now in
    # the about-card at the bottom rather than above the city list, but
    # still in static HTML and crawlable.
    assert "Texas Property Code" in body
    assert "Chapter 209" in body
    assert 'class="about-card"' in body
    # Metro grouping renders headings
    assert "Houston Metro" in body
    assert "Dallas&#x2013;Fort Worth" in body or "Dallas–Fort Worth" in body
    # JSON-LD blocks
    assert "BreadcrumbList" in body and "CollectionPage" in body
    # Canonical URL
    assert '<link rel="canonical" href="https://hoaproxy.org/hoa/tx/"' in body


def test_city_index_has_intro_featured_and_jsonld(client):
    resp = client.get("/hoa/tx/houston/")
    assert resp.status_code == 200
    body = resp.text
    assert "homeowners association" in body
    # JSON-LD
    assert "BreadcrumbList" in body and "CollectionPage" in body
    # Canonical
    assert 'rel="canonical"' in body
    assert "/hoa/tx/houston/" in body


def test_sitemap_index_lists_per_state_children(client):
    """`/sitemap.xml` is now a sitemap-index pointing to per-state child sitemaps."""
    # Bust the cache so seed data from this module is reflected
    from api import main as api_main
    api_main._sitemap_cache["ts"] = 0.0

    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    body = resp.text
    assert "<sitemapindex" in body
    # Static sitemap pointer is always present
    assert "https://hoaproxy.org/sitemap-static.xml" in body
    # TX has seeded HOAs with documents, so the TX child sitemap must appear
    assert "https://hoaproxy.org/sitemap-tx.xml" in body
    import re as _re
    sitemap_entries = _re.findall(r"<sitemap>.*?</sitemap>", body, _re.DOTALL)
    assert sitemap_entries, "expected at least one <sitemap> entry in index"
    missing_lastmod = [s for s in sitemap_entries if "<lastmod>" not in s]
    assert not missing_lastmod, f"{len(missing_lastmod)} sitemap(s) missing lastmod"


def test_per_state_child_sitemap_includes_only_indexable_hoas(client):
    """Child sitemap for a state lists state index, city indexes, and HOAs with docs.

    Docless HOAs are excluded — they're noindex'd on render so listing them
    in the sitemap would just waste crawl budget.
    """
    # Add a docless HOA in TX that should NOT appear
    with db.get_connection(_TMP_PATH) as conn:
        db.get_or_create_hoa(conn, "Sitemap Excluded Empty HOA")
        db.upsert_hoa_location(
            conn, "Sitemap Excluded Empty HOA", city="Austin", state="TX",
        )

    from api import main as api_main
    api_main._sitemap_cache["ts"] = 0.0

    resp = client.get("/sitemap-tx.xml")
    assert resp.status_code == 200
    body = resp.text
    # State and city indexes are present
    assert "<loc>https://hoaproxy.org/hoa/tx/</loc>" in body
    assert "<loc>https://hoaproxy.org/hoa/tx/san-marcos/</loc>" in body
    # The seeded HOA with documents is present
    assert "blanco-vista-residential-owners-association-inc" in body
    # The docless HOA is NOT
    assert "sitemap-excluded-empty-hoa" not in body
    # Every <url> still has a <lastmod>
    import re as _re
    url_entries = _re.findall(r"<url>.*?</url>", body, _re.DOTALL)
    assert url_entries
    missing_lastmod = [u for u in url_entries if "<lastmod>" not in u]
    assert not missing_lastmod


def test_sitemap_rejects_unknown_state(client):
    resp = client.get("/sitemap-zz.xml")
    assert resp.status_code == 404


def test_og_tags_present_on_all_seo_pages(client):
    """Open Graph + Twitter Card tags emitted on home, profile, state, city pages."""
    paths = [
        "/",
        "/hoa/tx/san-marcos/blanco-vista-residential-owners-association-inc",
        "/hoa/tx/",
        "/hoa/tx/san-marcos/",
    ]
    for path in paths:
        body = client.get(path).text
        for tag in (
            'property="og:type"',
            'property="og:title"',
            'property="og:description"',
            'property="og:image"',
            'name="twitter:card"',
        ):
            assert tag in body, f"missing {tag} on {path}"


def test_homepage_state_pills_are_crawlable_links(client):
    """Homepage SSRs state pills as <a href=/hoa/{state}/> grouped by region."""
    # Bypass module-level TTL cache so seed data is reflected immediately.
    from api import main as api_main
    api_main._INDEX_STATES_CACHE["ts"] = 0.0

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text

    # Texas (seeded above) is rendered as an anchor to /hoa/tx/
    assert '<a class="state-pill" href="/hoa/tx/"' in body
    # Wrapped in a region label (Texas → South)
    assert "South" in body
    # Regional grid wrapper present (replaces the legacy state-filter-row)
    assert 'class="state-region-grid"' in body
    # Static directory link is present in raw HTML — Googlebot can follow it
    # without running JS.
    assert 'href="/hoa/tx/"' in body
