"""Tests for scaled /hoas/summary (pagination/filter), /hoas/states, /hoas/resolve/{slug}."""
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


def _seed(conn, name: str, city: str | None = None, state: str | None = None) -> int:
    hoa_id = db.get_or_create_hoa(conn, name)
    db.upsert_document(conn, hoa_id, f"{name}/doc.pdf", checksum=f"sum-{name}", byte_size=100, page_count=1)
    if city or state:
        db.upsert_hoa_location(conn, name, city=city, state=state)
    return hoa_id


class TestHoaSummaryScale(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        tmp = Path(self.tempdir.name)
        self.db_path = tmp / "hoa.db"
        self.docs_root = tmp / "docs"
        self.docs_root.mkdir(parents=True, exist_ok=True)
        self.env_patcher = patch.dict(
            os.environ,
            {"HOA_DB_PATH": str(self.db_path), "HOA_DOCS_ROOT": str(self.docs_root)},
            clear=False,
        )
        self.env_patcher.start()
        self.client = TestClient(app)

        with db.get_connection(self.db_path) as conn:
            _seed(conn, "Alpha HOA", city="Charlotte", state="NC")
            _seed(conn, "Beta HOA", city="Raleigh", state="NC")
            _seed(conn, "Gamma HOA", city="Atlanta", state="GA")
            _seed(conn, "Delta HOA")  # no location

    def tearDown(self) -> None:
        self.env_patcher.stop()
        self.tempdir.cleanup()

    # --- /hoas/summary ---

    def test_summary_returns_page_shape(self) -> None:
        r = self.client.get("/hoas/summary")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("results", data)
        self.assertIn("total", data)
        self.assertIsInstance(data["results"], list)
        self.assertIsInstance(data["total"], int)

    def test_summary_total_equals_all_hoas(self) -> None:
        r = self.client.get("/hoas/summary")
        data = r.json()
        self.assertEqual(data["total"], 4)

    def test_summary_default_limit(self) -> None:
        r = self.client.get("/hoas/summary")
        data = r.json()
        # With 4 HOAs the results should be ≤ 50
        self.assertLessEqual(len(data["results"]), 50)

    def test_summary_limit_and_offset(self) -> None:
        r1 = self.client.get("/hoas/summary?limit=2&offset=0")
        r2 = self.client.get("/hoas/summary?limit=2&offset=2")
        d1 = r1.json()
        d2 = r2.json()
        self.assertEqual(len(d1["results"]), 2)
        self.assertEqual(len(d2["results"]), 2)
        # Together they equal total
        self.assertEqual(d1["total"], 4)
        names = {x["hoa"] for x in d1["results"]} | {x["hoa"] for x in d2["results"]}
        self.assertEqual(names, {"Alpha HOA", "Beta HOA", "Gamma HOA", "Delta HOA"})

    def test_summary_filter_by_q_name(self) -> None:
        r = self.client.get("/hoas/summary?q=Alpha")
        data = r.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["results"][0]["hoa"], "Alpha HOA")

    def test_summary_filter_by_q_city(self) -> None:
        r = self.client.get("/hoas/summary?q=Charlotte")
        data = r.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["results"][0]["hoa"], "Alpha HOA")

    def test_summary_filter_by_state(self) -> None:
        r = self.client.get("/hoas/summary?state=NC")
        data = r.json()
        self.assertEqual(data["total"], 2)
        hoas = {x["hoa"] for x in data["results"]}
        self.assertEqual(hoas, {"Alpha HOA", "Beta HOA"})

    def test_summary_filter_no_results(self) -> None:
        r = self.client.get("/hoas/summary?q=ZZZnonexistent")
        data = r.json()
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["results"], [])

    def test_summary_max_limit_capped(self) -> None:
        r = self.client.get("/hoas/summary?limit=9999")
        self.assertEqual(r.status_code, 200)
        # Should not raise, and limit is capped at 500

    # --- /hoas/states ---

    def test_states_returns_list(self) -> None:
        r = self.client.get("/hoas/states")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsInstance(data, list)

    def test_states_contains_seeded_states(self) -> None:
        r = self.client.get("/hoas/states")
        data = r.json()
        states = [d["state"] for d in data]
        self.assertIn("GA", states)
        self.assertIn("NC", states)

    def test_states_has_count(self) -> None:
        r = self.client.get("/hoas/states")
        data = r.json()
        nc = next(d for d in data if d["state"] == "NC")
        self.assertEqual(nc["count"], 2)

    def test_states_sorted_alpha(self) -> None:
        r = self.client.get("/hoas/states")
        data = r.json()
        states = [d["state"] for d in data]
        self.assertEqual(states, sorted(states))

    def test_states_no_duplicates(self) -> None:
        r = self.client.get("/hoas/states")
        data = r.json()
        states = [d["state"] for d in data]
        self.assertEqual(len(states), len(set(states)))

    # --- /hoas/resolve/{slug} ---

    def test_resolve_exact_match(self) -> None:
        r = self.client.get("/hoas/resolve/Alpha HOA")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["hoa_name"], "Alpha HOA")
        self.assertIsInstance(data["hoa_id"], int)
        self.assertEqual(data["city"], "Charlotte")
        self.assertEqual(data["state"], "NC")

    def test_resolve_slug_match(self) -> None:
        r = self.client.get("/hoas/resolve/alpha-hoa")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["hoa_name"], "Alpha HOA")

    def test_resolve_slug_underscore(self) -> None:
        r = self.client.get("/hoas/resolve/alpha_hoa")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["hoa_name"], "Alpha HOA")

    def test_resolve_not_found(self) -> None:
        r = self.client.get("/hoas/resolve/nonexistent-hoa-xyz")
        self.assertEqual(r.status_code, 404)

    def test_resolve_returns_hoa_id(self) -> None:
        r = self.client.get("/hoas/resolve/gamma-hoa")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("hoa_id", data)
        self.assertGreater(data["hoa_id"], 0)


if __name__ == "__main__":
    unittest.main()
