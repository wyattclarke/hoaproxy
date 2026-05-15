"""Tests for v2 prepared bundle schema (chunks sidecar)."""
from __future__ import annotations

import pytest

from hoaware import prepared_ingest as pi
from hoaware.chunker import PageContent


def _valid_chunks_payload(
    *,
    sha: str = "a" * 64,
    model: str = "text-embedding-3-small",
    dims: int = 1536,
    nchunks: int = 2,
):
    return {
        "schema_version": 2,
        "doc_sha256": sha,
        "produced_at": "2026-05-15T03:12:09Z",
        "chunker": {"max_chars": 1800, "overlap_chars": 200},
        "embedder": {"provider": "openai", "model": model, "dimensions": dims},
        "chunks": [
            {
                "idx": i,
                "text": f"chunk {i} text body",
                "page_start": 1,
                "page_end": 1,
                "embedding": [0.0] * dims,
            }
            for i in range(nchunks)
        ],
    }


class TestValidateChunksSidecar:
    def test_accepts_valid_payload(self):
        v = pi.validate_chunks_sidecar(_valid_chunks_payload())
        assert v.schema_version == 2
        assert len(v.chunks) == 2
        assert v.embedder_dimensions == 1536
        assert v.embedder_model == "text-embedding-3-small"

    def test_rejects_wrong_schema_version(self):
        payload = _valid_chunks_payload()
        payload["schema_version"] = 1
        with pytest.raises(pi.PreparedIngestError, match="schema_version"):
            pi.validate_chunks_sidecar(payload)

    def test_rejects_model_mismatch(self):
        payload = _valid_chunks_payload(model="text-embedding-3-small")
        with pytest.raises(pi.PreparedIngestError, match="model"):
            pi.validate_chunks_sidecar(payload, expected_model="text-embedding-3-large")

    def test_rejects_dimensions_mismatch(self):
        payload = _valid_chunks_payload(dims=512)
        # The validator enforces expected_dimensions; payload says 512 but
        # we expect 1536 → reject.
        with pytest.raises(pi.PreparedIngestError, match="dimensions"):
            pi.validate_chunks_sidecar(payload)

    def test_rejects_sha_mismatch(self):
        payload = _valid_chunks_payload(sha="a" * 64)
        with pytest.raises(pi.PreparedIngestError, match="sha256"):
            pi.validate_chunks_sidecar(payload, expected_sha256="b" * 64)

    def test_rejects_empty_chunks(self):
        payload = _valid_chunks_payload(nchunks=0)
        payload["chunks"] = []
        with pytest.raises(pi.PreparedIngestError, match="non-empty list"):
            pi.validate_chunks_sidecar(payload)

    def test_rejects_wrong_embedding_length(self):
        payload = _valid_chunks_payload()
        payload["chunks"][0]["embedding"] = [0.0] * 100  # wrong length
        with pytest.raises(pi.PreparedIngestError, match="length 1536"):
            pi.validate_chunks_sidecar(payload)


class TestValidateBundleV2:
    def _bundle(self, *, schema_version: int = 2, with_chunks_path: bool = True):
        bundle = {
            "schema_version": schema_version,
            "bundle_id": "abc123",
            "source_manifest_uri": "gs://hoaproxy-bank/v1/FL/palm-beach/x/manifest.json",
            "state": "FL",
            "hoa_name": "Example HOA",
            "documents": [
                {
                    "sha256": "a" * 64,
                    "filename": "ccr.pdf",
                    "pdf_gcs_path": "gs://hoaproxy-ingest-ready/v1/FL/palm-beach/x/abc/docs/" + "a" * 64 + ".pdf",
                    "text_gcs_path": "gs://hoaproxy-ingest-ready/v1/FL/palm-beach/x/abc/texts/" + "a" * 64 + ".json",
                    "category": "ccr",
                    "text_extractable": True,
                    "page_count": 12,
                    "docai_pages": 0,
                },
            ],
        }
        if with_chunks_path:
            bundle["documents"][0]["chunks_gcs_path"] = (
                "gs://hoaproxy-ingest-ready/v1/FL/palm-beach/x/abc/chunks-"
                + "a" * 64 + ".json"
            )
        return bundle

    def test_v2_with_chunks_path_parses(self):
        b = pi.validate_bundle(self._bundle())
        assert b.schema_version == 2
        assert b.documents[0].chunks_gcs_path is not None

    def test_v1_still_accepted(self):
        b = pi.validate_bundle(self._bundle(schema_version=1, with_chunks_path=False))
        assert b.schema_version == 1
        assert b.documents[0].chunks_gcs_path is None

    def test_v3_rejected(self):
        with pytest.raises(pi.PreparedIngestError, match="unsupported schema_version"):
            pi.validate_bundle(self._bundle(schema_version=3))


class TestBakeChunksSidecar:
    def test_produces_validatable_dict(self):
        from hoaware.prepare.embed import bake_chunks_sidecar

        class StubOpenAI:
            class embeddings:
                @staticmethod
                def create(*, model, input):
                    return type("R", (), {
                        "data": [
                            type("d", (), {"embedding": [0.01] * 1536})()
                            for _ in input
                        ]
                    })()

        pages = [PageContent(number=1, text="Article 1. Lorem ipsum dolor sit amet. " * 100)]
        sidecar = bake_chunks_sidecar(
            doc_sha256="a" * 64,
            pages=pages,
            embedding_model="text-embedding-3-small",
            openai_client=StubOpenAI(),
        )
        v = pi.validate_chunks_sidecar(
            sidecar, expected_sha256="a" * 64, expected_model="text-embedding-3-small"
        )
        assert len(v.chunks) >= 1
        assert v.embedder_model == "text-embedding-3-small"

    def test_empty_pages_raises(self):
        from hoaware.prepare.embed import bake_chunks_sidecar
        with pytest.raises(ValueError, match="non-empty"):
            bake_chunks_sidecar(
                doc_sha256="a" * 64,
                pages=[],
                embedding_model="text-embedding-3-small",
                openai_client=None,
            )
