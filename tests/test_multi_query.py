from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from openai import APITimeoutError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.main import app
from hoaware import db
from hoaware.config import load_settings
from hoaware.qa import QAProviderError, QATemporaryError, _create_chat_completion


class TestMultiQueryEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tempdir.name)
        self.db_path = tmp / "hoa.db"
        self.docs_root = tmp / "docs"
        self.docs_root.mkdir(parents=True, exist_ok=True)
        self.env_patcher = patch.dict(
            os.environ,
            {
                "HOA_DB_PATH": str(self.db_path),
                "HOA_DOCS_ROOT": str(self.docs_root),
                "OPENAI_API_KEY": "test-key",
            },
            clear=False,
        )
        self.env_patcher.start()
        self.client = TestClient(app)

        with db.get_connection(self.db_path) as conn:
            self._seed_hoa(conn, "Master HOA")
            self._seed_hoa(conn, "Neighborhood HOA")

    def tearDown(self) -> None:
        self.env_patcher.stop()
        self.tempdir.cleanup()

    def _seed_hoa(self, conn, name: str) -> None:
        hoa_id = db.get_or_create_hoa(conn, name)
        db.upsert_document(
            conn,
            hoa_id,
            f"{name}/doc.pdf",
            checksum=f"sum-{name}",
            byte_size=100,
            page_count=1,
        )

    def test_search_multi_requires_hoas(self) -> None:
        response = self.client.post("/search/multi", json={"hoas": [], "query": "rules", "k": 8})
        self.assertEqual(response.status_code, 400)
        self.assertIn("hoas is required", response.json()["detail"])

    def test_qa_multi_requires_hoas(self) -> None:
        response = self.client.post("/qa/multi", json={"hoas": [], "question": "rules?", "k": 8})
        self.assertEqual(response.status_code, 400)
        self.assertIn("hoas is required", response.json()["detail"])

    def test_search_multi_includes_hoa_in_results(self) -> None:
        fake_matches = [
            {
                "score": 0.91,
                "payload": {
                    "hoa": "Master HOA",
                    "document": "Master HOA/doc.pdf",
                    "start_page": 1,
                    "end_page": 1,
                    "text": "Parking rules apply.",
                },
            },
            {
                "score": 0.74,
                "payload": {
                    "hoa": "Neighborhood HOA",
                    "document": "Neighborhood HOA/doc.pdf",
                    "start_page": 3,
                    "end_page": 4,
                    "text": "Pool rules apply.",
                },
            },
        ]
        with patch("api.main.retrieve_context_multi", return_value=fake_matches):
            response = self.client.post(
                "/search/multi",
                json={"hoas": ["Master HOA", "Neighborhood HOA"], "query": "rules", "k": 8},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["results"][0]["hoa"], "Master HOA")
        self.assertEqual(body["results"][1]["hoa"], "Neighborhood HOA")

    def test_qa_multi_includes_hoa_in_sources(self) -> None:
        with patch(
            "api.main.get_answer_multi",
            return_value=(
                "Combined answer",
                [
                    {"hoa": "Master HOA", "document": "Master HOA/doc.pdf", "pages": "1–1"},
                    {"hoa": "Neighborhood HOA", "document": "Neighborhood HOA/doc.pdf", "pages": "3–4"},
                ],
                [{"score": 0.9, "payload": {"hoa": "Master HOA"}}],
            ),
        ):
            response = self.client.post(
                "/qa/multi",
                json={"hoas": ["Master HOA", "Neighborhood HOA"], "question": "What rules apply?", "k": 8},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["answer"], "Combined answer")
        self.assertEqual(body["sources"][0]["hoa"], "Master HOA")
        self.assertEqual(body["sources"][1]["hoa"], "Neighborhood HOA")

    def test_qa_returns_503_for_temporary_provider_failure(self) -> None:
        with patch("api.main.get_answer", side_effect=QATemporaryError("provider 502")):
            response = self.client.post(
                "/qa",
                json={"hoa": "Master HOA", "question": "What rules apply?", "k": 6},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("temporarily unavailable", response.json()["detail"])

    def test_qa_returns_502_for_non_retryable_provider_failure(self) -> None:
        with patch("api.main.get_answer", side_effect=QAProviderError("Q&A provider returned HTTP 401")):
            response = self.client.post(
                "/qa",
                json={"hoa": "Master HOA", "question": "What rules apply?", "k": 6},
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("Q&A provider returned HTTP 401", response.json()["detail"])

    def test_chat_completion_uses_high_quality_low_latency_fallback_after_transient_failure(self) -> None:
        class FakeCompletions:
            def __init__(self, *, fail_first: bool = False) -> None:
                self.calls = 0
                self.models: list[str] = []
                self.fail_first = fail_first

            def create(self, **kwargs):
                self.calls += 1
                self.models.append(kwargs["model"])
                if self.fail_first and self.calls == 1:
                    request = httpx.Request("POST", "https://qa.example.test/chat/completions")
                    raise APITimeoutError(request=request)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

        primary_completions = FakeCompletions(fail_first=True)
        fallback_completions = FakeCompletions()
        primary_client = SimpleNamespace(chat=SimpleNamespace(completions=primary_completions))
        fallback_client = SimpleNamespace(chat=SimpleNamespace(completions=fallback_completions))

        with patch("hoaware.qa.time.sleep"):
            result = _create_chat_completion(
                primary_client,
                model="deepseek/deepseek-v4-flash",
                messages=[],
                fallback_client=fallback_client,
                fallback_model="openai/gpt-oss-120b",
            )

        self.assertEqual(primary_completions.calls, 1)
        self.assertEqual(fallback_completions.calls, 1)
        self.assertEqual(primary_completions.models, ["deepseek/deepseek-v4-flash"])
        self.assertEqual(fallback_completions.models, ["openai/gpt-oss-120b"])
        self.assertEqual(result.model, "openai/gpt-oss-120b")
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.completion.choices[0].message.content, "ok")

    def test_default_qa_fallback_is_high_quality_fast_open_weight_model(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GROQ_API_KEY": "test-groq-key",
            },
            clear=False,
        ):
            os.environ.pop("QA_FALLBACK_API_BASE_URL", None)
            os.environ.pop("QA_FALLBACK_API_KEY", None)
            os.environ.pop("QA_FALLBACK_MODEL", None)
            settings = load_settings()

        self.assertEqual(settings.qa_fallback_api_base_url, "https://api.groq.com/openai/v1")
        self.assertEqual(settings.qa_fallback_api_key, "test-groq-key")
        self.assertEqual(settings.qa_fallback_model, "openai/gpt-oss-120b")


if __name__ == "__main__":
    unittest.main()
