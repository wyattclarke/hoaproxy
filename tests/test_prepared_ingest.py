import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as gcs_exceptions

from api import main as api_main  # noqa: E402
from api.main import app  # noqa: E402
from hoaware import db, prepared_ingest  # noqa: E402
from hoaware.config import load_settings  # noqa: E402
from hoaware.doc_classifier import classify_from_text  # noqa: E402

client = TestClient(app)


class FakeBlob:
    def __init__(self, bucket, name, data=None, generation=1):
        self.bucket = bucket
        self.name = name
        self._data = data.encode("utf-8") if isinstance(data, str) else data
        self.generation = generation
        self.last_if_generation_match = None
        self.force_precondition_failure = False

    def exists(self):
        return self._data is not None

    def reload(self):
        if self._data is None:
            raise gcs_exceptions.NotFound("missing")

    def download_as_bytes(self, *args, **kwargs):
        if self._data is None:
            raise gcs_exceptions.NotFound("missing")
        return self._data

    def upload_from_string(self, data, *args, **kwargs):
        self.last_if_generation_match = kwargs.get("if_generation_match")
        if self.force_precondition_failure:
            raise gcs_exceptions.PreconditionFailed("stale")
        expected = kwargs.get("if_generation_match")
        if expected is not None and expected != self.generation:
            raise gcs_exceptions.PreconditionFailed("stale")
        self._data = data.encode("utf-8") if isinstance(data, str) else data
        self.generation += 1


class FakeBucket:
    def __init__(self, name="hoaproxy-ingest-ready", objects=None):
        self.name = name
        self._objects = {}
        for key, value in (objects or {}).items():
            self._objects[key] = FakeBlob(self, key, value)

    def blob(self, name):
        if name not in self._objects:
            self._objects[name] = FakeBlob(self, name)
        return self._objects[name]

    def list_blobs(self, prefix=""):
        return [blob for name, blob in sorted(self._objects.items()) if name.startswith(prefix)]


def _make_text_pdf(text: str = "Declaration of Covenants") -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, text)
    return bytes(pdf.output())


@pytest.fixture(autouse=True)
def _reset_db(monkeypatch, tmp_path):
    monkeypatch.setenv("HOA_DB_PATH", str(tmp_path / "prepared.db"))
    monkeypatch.setenv("HOA_DOCS_ROOT", str(tmp_path / "docs"))
    monkeypatch.setenv("JWT_SECRET", "test-secret-for-ci")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        conn.executescript(db.SCHEMA)
        conn.commit()


def _bundle_payload(sha, *, text_path=None):
    prefix = "v1/KS/johnson/example/bundle1"
    return {
        "schema_version": 1,
        "bundle_id": "bundle1",
        "source_manifest_uri": "gs://hoaproxy-bank/v1/KS/johnson/example/manifest.json",
        "state": "KS",
        "county": "Johnson",
        "hoa_name": "Example Homes Association",
        "metadata_type": "hoa",
        "website_url": "https://example.org",
        "address": {"city": "Overland Park", "state": "KS", "county": "Johnson"},
        "geometry": {"boundary_geojson": None, "latitude": None, "longitude": None},
        "documents": [
            {
                "sha256": sha,
                "filename": "declaration.pdf",
                "pdf_gcs_path": f"gs://hoaproxy-ingest-ready/{prefix}/docs/{sha}.pdf",
                "text_gcs_path": text_path
                or f"gs://hoaproxy-ingest-ready/{prefix}/texts/{sha}.json",
                "source_url": "https://source.example/declaration.pdf",
                "category": "ccr",
                "text_extractable": False,
                "page_count": 1,
                "docai_pages": 0,
                "filter_reason": "valid_governing_doc",
            }
        ],
        "rejected_documents": [],
        "created_at": "2026-05-05T00:00:00Z",
    }


def test_validate_bundle_requires_bank_source_and_allowed_category():
    sha = "a" * 64
    bundle = prepared_ingest.validate_bundle(_bundle_payload(sha), expected_state="KS")
    assert bundle.state == "KS"
    assert bundle.documents[0].category == "ccr"

    bad = _bundle_payload(sha)
    bad["documents"][0]["category"] = "membership_list"
    with pytest.raises(prepared_ingest.PreparedIngestError):
        prepared_ingest.validate_bundle(bad, expected_state="KS")


def test_claim_ready_bundle_uses_generation_precondition():
    prefix = "v1/KS/johnson/example/bundle1"
    bucket = FakeBucket(objects={
        f"{prefix}/status.json": json.dumps({
            "status": "ready",
            "claimed_by": None,
            "claimed_at": None,
            "imported_at": None,
            "error": None,
        })
    })
    blob = bucket.blob(f"{prefix}/status.json")
    blob.generation = 7

    assert prepared_ingest.claim_ready_bundle(bucket, prefix, claimed_by="test-worker") is True
    assert blob.last_if_generation_match == 7
    status = json.loads(blob.download_as_bytes())
    assert status["status"] == "claimed"
    assert status["claimed_by"] == "test-worker"


def test_admin_ingest_ready_gcs_uses_pre_extracted_pages(monkeypatch):
    from hoaware import ingest as ingest_mod

    pdf_bytes = _make_text_pdf("ignored because sidecar wins")
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    prefix = "v1/KS/johnson/example/bundle1"
    sidecar = {
        "pages": [
            {"number": 1, "text": "Article I. Prepared OCR text imported from GCS."}
        ],
        "docai_pages": 0,
    }
    bucket = FakeBucket(objects={
        f"{prefix}/status.json": json.dumps({"status": "ready", "claimed_by": None, "claimed_at": None, "imported_at": None, "error": None}),
        f"{prefix}/bundle.json": json.dumps(_bundle_payload(sha)),
        f"{prefix}/texts/{sha}.json": json.dumps(sidecar),
        f"{prefix}/docs/{sha}.pdf": pdf_bytes,
    })

    monkeypatch.setattr(api_main, "_prepared_gcs_bucket", lambda bucket_name=None: bucket)

    def _boom(*args, **kwargs):
        raise AssertionError("extract_pages must not run for prepared GCS bundles")

    monkeypatch.setattr(ingest_mod, "extract_pages", _boom)
    monkeypatch.setattr(ingest_mod, "batch_embeddings", lambda texts, *, client, model: [[0.0] * 1536 for _ in texts])
    monkeypatch.setattr(ingest_mod, "build_client", lambda *a, **kw: object())
    monkeypatch.setattr(ingest_mod, "ensure_collection", lambda *a, **kw: None)
    monkeypatch.setattr(ingest_mod, "upsert_chunks", lambda *a, **kw: ["pid"] * len(a[2]))
    monkeypatch.setattr(ingest_mod, "delete_points", lambda *a, **kw: None)
    monkeypatch.setattr(ingest_mod, "points_exist", lambda *a, **kw: False)

    r = client.post(
        "/admin/ingest-ready-gcs?state=KS&limit=1",
        headers={"Authorization": "Bearer test-secret-for-ci"},
    )
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["status"] == "imported"
    assert result["indexed"] == 1

    settings = load_settings()
    with db.get_connection(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT chunks.text FROM chunks "
            "JOIN documents ON documents.id = chunks.document_id "
            "JOIN hoas ON hoas.id = documents.hoa_id "
            "WHERE hoas.name = ?",
            ("Example Homes Association",),
        ).fetchall()
    assert rows
    assert "Prepared OCR text imported from GCS" in "\n".join(row["text"] for row in rows)


def test_admin_ingest_ready_gcs_fails_missing_sidecar_without_ingest(monkeypatch):
    pdf_bytes = _make_text_pdf("should not be ingested")
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    prefix = "v1/KS/johnson/example/bundle1"
    bucket = FakeBucket(objects={
        f"{prefix}/status.json": json.dumps({"status": "ready", "claimed_by": None, "claimed_at": None, "imported_at": None, "error": None}),
        f"{prefix}/bundle.json": json.dumps(_bundle_payload(sha)),
        f"{prefix}/docs/{sha}.pdf": pdf_bytes,
    })
    monkeypatch.setattr(api_main, "_prepared_gcs_bucket", lambda bucket_name=None: bucket)

    def _boom(*args, **kwargs):
        raise AssertionError("ingest must not run when text sidecar is missing")

    monkeypatch.setattr(api_main, "ingest_pdf_paths", _boom)

    r = client.post(
        "/admin/ingest-ready-gcs?state=KS&limit=1",
        headers={"Authorization": "Bearer test-secret-for-ci"},
    )
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["status"] == "failed"
    assert "missing GCS object" in result["error"]

    status = json.loads(bucket.blob(f"{prefix}/status.json").download_as_bytes())
    assert status["status"] == "failed"


def test_nominatim_geometry_selection_prefers_neighborhood_polygon():
    from scripts import prepare_bank_for_ingest as prep

    selected = prep._select_nominatim_geometry([
        {
            "class": "boundary",
            "type": "administrative",
            "display_name": "Johnson County, Kansas",
            "geojson": {"type": "Polygon", "coordinates": []},
            "lat": "38.9",
            "lon": "-94.8",
        },
        {
            "class": "place",
            "type": "neighbourhood",
            "display_name": "Example Estates, Overland Park, Kansas",
            "geojson": {"type": "Polygon", "coordinates": [[[]]]},
            "lat": "38.91",
            "lon": "-94.72",
            "osm_id": 123,
        },
    ])
    assert selected is not None
    assert selected["location_quality"] == "polygon"
    assert selected["osm_id"] == 123


def test_geo_query_candidates_include_city_and_county():
    from scripts import prepare_bank_for_ingest as prep

    queries = prep._geo_query_candidates(
        hoa_name="Example Estates Homeowners Association",
        address={"city": "Overland Park", "county": "Johnson"},
        state="KS",
        county_slug="johnson",
    )
    assert "Example Estates Homeowners Association, Overland Park, KS" in queries
    assert "Example Estates, Johnson County, KS" in queries


def test_prepared_location_fields_honor_quality_hint():
    sha = "b" * 64
    payload = _bundle_payload(sha)
    payload["geometry"] = {
        "latitude": 38.91,
        "longitude": -94.72,
        "location_quality": "zip_centroid",
    }
    bundle = prepared_ingest.validate_bundle(payload, expected_state="KS")
    fields = api_main._prepared_bundle_location_fields(bundle)
    assert fields["latitude"] == 38.91
    assert fields["longitude"] == -94.72
    assert fields["location_quality"] == "zip_centroid"


def test_classifier_recognizes_declaration_of_restrictions():
    result = classify_from_text(
        "DECLARATION OF RESTRICTIONS\n"
        "This declaration establishes covenants and restrictions for the subdivision."
    )
    assert result is not None
    assert result["category"] == "ccr"


def test_first_page_review_uses_existing_text_before_docai():
    from scripts import prepare_bank_for_ingest as prep

    pdf_bytes = _make_text_pdf("DECLARATION OF RESTRICTIONS for Example Estates")
    text, docai_pages = prep._extract_first_page_review_text(
        pdf_bytes=pdf_bytes,
        text_extractable=True,
    )
    assert "DECLARATION OF RESTRICTIONS" in text
    assert docai_pages == 0


def test_reject_reason_defers_low_value_and_unknown_until_after_page_one_review():
    from scripts import prepare_bank_for_ingest as prep

    common = {
        "precheck": {"page_count": 2},
        "include_low_value": False,
        "live_shas": set(),
        "prepared_shas": set(),
        "sha256": "c" * 64,
    }
    assert prep._reject_reason(category="minutes", hard_only=True, **common) is None
    assert prep._reject_reason(category=None, hard_only=True, **common) is None
    assert prep._reject_reason(category="minutes", hard_only=False, **common) == "low_value:minutes"
    assert (
        prep._reject_reason(category=None, hard_only=False, **common)
        == "unsupported_category:unknown"
    )
