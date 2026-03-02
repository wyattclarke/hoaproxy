from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.main import app
from hoaware import db


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


if __name__ == "__main__":
    unittest.main()
