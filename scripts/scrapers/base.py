"""Shared utilities for HOA scrapers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def title_case_name(name: str) -> str:
    """Convert 'ACACIA PUEBLO HOMEOWNERS ASSOCIATION' → 'Acacia Pueblo Homeowners Association'."""
    lower_words = {"of", "the", "at", "in", "for", "and", "or", "no", "no."}
    parts = name.split()
    return " ".join(
        w.capitalize() if i == 0 or w.lower() not in lower_words else w.lower()
        for i, w in enumerate(parts)
    )


def compute_centroid(geometry: dict) -> tuple[float, float] | None:
    """Compute centroid (lat, lon) from a GeoJSON geometry."""
    points: list[tuple[float, float]] = []

    def collect(coords: object) -> None:
        if not isinstance(coords, list):
            return
        if coords and isinstance(coords[0], (int, float)) and len(coords) >= 2:
            points.append((float(coords[1]), float(coords[0])))  # lat, lon
            return
        for child in coords:
            collect(child)

    collect(geometry.get("coordinates"))
    if not points:
        return None
    avg_lat = sum(p[0] for p in points) / len(points)
    avg_lon = sum(p[1] for p in points) / len(points)
    return avg_lat, avg_lon


def write_import_file(
    records: list[dict],
    source: str,
    output_path: str | Path,
) -> Path:
    """Write records in the standard bulk-import JSON format.

    Each record dict should have keys matching BulkImportRecord fields.
    Returns the output path.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "source": source,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "records": records,
    }
    with open(output_path, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {len(records)} records → {output_path}")
    return output_path
