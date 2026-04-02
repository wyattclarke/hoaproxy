from __future__ import annotations

import json
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

from api.main import (
    _extract_geojson_polygons,
    _point_in_polygon,
    _suggestions_for_point,
    app,
)
from hoaware import db


class _MockResponse:
    def __init__(self, payload: list[dict]):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> list[dict]:
        return self._payload


class TestUniversalLookup(unittest.TestCase):
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

        master_boundary = {
            "type": "Polygon",
            "coordinates": [
                [
                    [-78.001, 34.999],
                    [-77.999, 34.999],
                    [-77.999, 35.001],
                    [-78.001, 35.001],
                    [-78.001, 34.999],
                ]
            ],
        }

        with db.get_connection(self.db_path) as conn:
            self._seed_hoa(conn, "Master HOA", master_boundary, 35.0, -78.0)
            self._seed_hoa(conn, "Point HOA A", None, 35.005, -78.0)
            self._seed_hoa(conn, "Point HOA B", None, 35.0, -77.99)
            self._seed_hoa(conn, "Far HOA", None, 36.0, -79.0)

    def tearDown(self) -> None:
        self.env_patcher.stop()
        self.tempdir.cleanup()

    def _seed_hoa(
        self,
        conn,
        name: str,
        boundary_geojson: dict | None,
        latitude: float | None,
        longitude: float | None,
    ) -> None:
        hoa_id = db.get_or_create_hoa(conn, name)
        db.upsert_document(
            conn,
            hoa_id,
            f"{name}/governing.pdf",
            checksum=f"sum-{name}",
            byte_size=100,
            page_count=1,
        )
        db.upsert_hoa_location(
            conn,
            name,
            city="Raleigh",
            state="NC",
            latitude=latitude,
            longitude=longitude,
            boundary_geojson=json.dumps(boundary_geojson) if boundary_geojson else None,
            source="manual",
        )

    def test_lookup_returns_hoa_and_address_suggestions(self) -> None:
        geocode_payload = [
            {
                "display_name": "123 Main St, Raleigh, NC 27601, USA",
                "lat": "35.0",
                "lon": "-78.0",
            }
        ]
        with patch("api.main.requests.get", return_value=_MockResponse(geocode_payload)):
            response = self.client.post("/lookup/universal", json={"query": "Master", "max_suggestions": 12})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(any(item["hoa"] == "Master HOA" for item in body["hoa_matches"]))
        self.assertTrue(body["address_lookup"]["resolved"])

        suggestions = {item["hoa"]: item for item in body["address_suggestions"]}
        self.assertEqual(suggestions["Master HOA"]["match_type"], "inside_boundary")
        self.assertTrue(suggestions["Master HOA"]["default_selected"])
        self.assertEqual(suggestions["Point HOA A"]["match_type"], "nearby_point")
        self.assertFalse(suggestions["Point HOA A"]["default_selected"])
        self.assertEqual(suggestions["Point HOA B"]["match_type"], "nearby_point")
        self.assertNotIn("Far HOA", suggestions)

    def test_address_only_promotes_all_suggestions(self) -> None:
        """When query is an address (no HOA name match), all address
        suggestions should be promoted to hoa_matches."""
        geocode_payload = [
            {
                "display_name": "123 Main St, Raleigh, NC 27601, USA",
                "lat": "35.0",
                "lon": "-78.0",
            }
        ]
        with patch("api.main.requests.get", return_value=_MockResponse(geocode_payload)):
            response = self.client.post(
                "/lookup/universal",
                json={"query": "123 Main St, Raleigh, NC 27601", "max_suggestions": 12},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        hoa_match_names = [item["hoa"] for item in body["hoa_matches"]]
        self.assertIn("Master HOA", hoa_match_names)
        self.assertIn("Point HOA A", hoa_match_names)
        self.assertIn("Point HOA B", hoa_match_names)
        self.assertEqual(body["hoa_matches"][0]["match_reason"], "inside_boundary")
        # Far HOA should still be excluded
        self.assertNotIn("Far HOA", hoa_match_names)

    def test_point_in_polygon_excludes_hole(self) -> None:
        geo = {
            "type": "Polygon",
            "coordinates": [
                [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
                [[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]],
            ],
        }
        polygons = _extract_geojson_polygons(geo)
        self.assertEqual(len(polygons), 1)
        self.assertFalse(_point_in_polygon(2.0, 2.0, polygons[0]))
        self.assertTrue(_point_in_polygon(0.5, 0.5, polygons[0]))

    def test_multipolygon_contains(self) -> None:
        geo = {
            "type": "MultiPolygon",
            "coordinates": [
                [[[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]]],
                [[[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]],
            ],
        }
        polygons = _extract_geojson_polygons(geo)
        self.assertEqual(len(polygons), 2)
        self.assertTrue(any(_point_in_polygon(5.0, 5.0, polygon) for polygon in polygons))

    def test_near_boundary_threshold(self) -> None:
        boundary = {
            "type": "Polygon",
            "coordinates": [
                [
                    [-0.001, -0.001],
                    [0.001, -0.001],
                    [0.001, 0.001],
                    [-0.001, 0.001],
                    [-0.001, -0.001],
                ]
            ],
        }
        rows = [{"hoa": "Boundary HOA", "boundary_geojson": boundary, "latitude": None, "longitude": None}]
        inside = _suggestions_for_point(0.0, 0.001 + (240.0 / 111320.0), rows, 10)
        outside = _suggestions_for_point(0.0, 0.001 + (260.0 / 111320.0), rows, 10)
        self.assertEqual(inside[0]["match_type"], "near_boundary")
        self.assertEqual(outside, [])

    def test_nearby_point_threshold(self) -> None:
        rows = [{"hoa": "Point HOA", "boundary_geojson": None, "latitude": 0.01445, "longitude": 0.0}]
        near = _suggestions_for_point(0.0, 0.0, rows, 10)
        rows_far = [{"hoa": "Point HOA", "boundary_geojson": None, "latitude": 0.01449, "longitude": 0.0}]
        far = _suggestions_for_point(0.0, 0.0, rows_far, 10)
        self.assertEqual(near[0]["match_type"], "nearby_point")
        self.assertEqual(far, [])

    def test_ranks_smaller_inside_polygon_first(self) -> None:
        large = {
            "type": "Polygon",
            "coordinates": [
                [[-0.01, -0.01], [0.01, -0.01], [0.01, 0.01], [-0.01, 0.01], [-0.01, -0.01]]
            ],
        }
        small = {
            "type": "Polygon",
            "coordinates": [
                [[-0.001, -0.001], [0.001, -0.001], [0.001, 0.001], [-0.001, 0.001], [-0.001, -0.001]]
            ],
        }
        rows = [
            {"hoa": "Large HOA", "boundary_geojson": large, "latitude": None, "longitude": None},
            {"hoa": "Small HOA", "boundary_geojson": small, "latitude": None, "longitude": None},
        ]
        suggestions = _suggestions_for_point(0.0, 0.0, rows, 10)
        self.assertEqual(suggestions[0]["hoa"], "Small HOA")
        self.assertEqual(suggestions[1]["hoa"], "Large HOA")


if __name__ == "__main__":
    unittest.main()
