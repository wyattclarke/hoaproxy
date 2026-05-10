#!/usr/bin/env python3
"""Pull Maricopa + Pima subdivision-polygon layers to local cache.

Two ArcGIS REST sources:

  Maricopa: https://gis.mcassessor.maricopa.gov/arcgis/rest/services/
            Subdivisions/MapServer/0  (~31,471 features, 1000/page)
            name field: SUBNAME

  Pima:     https://gisdata.pima.gov/arcgis1/rest/services/GISOpenData/
            LandRecords/MapServer/15  (~6,447 features, 2000/page)
            name field: SUB_NAME
            **Requires Mozilla User-Agent** — default UA returns 403.

Outputs:

  state_scrapers/az/data/maricopa_subdivisions.gpkg
  state_scrapers/az/data/pima_subdivisions.gpkg
  state_scrapers/az/data/az_subdpoly.jsonl  — per-row name/county/centroid

The JSONL is the seed file consumed by az_build_subdpoly_county_queries.py.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import geopandas as gpd
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "state_scrapers" / "az" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

SOURCES = {
    "maricopa": {
        "url": "https://gis.mcassessor.maricopa.gov/arcgis/rest/services/Subdivisions/MapServer/0/query",
        "name_field": "SUBNAME",
        "page_size": 1000,
        "out_sr": "4326",
        "metric_crs": "EPSG:32612",  # UTM 12N — works statewide for AZ
    },
    "pima": {
        "url": "https://gisdata.pima.gov/arcgis1/rest/services/GISOpenData/LandRecords/MapServer/15/query",
        "name_field": "SUB_NAME",
        "page_size": 2000,
        "out_sr": "4326",
        "metric_crs": "EPSG:32612",
    },
}

# Names that look like real subdivisions but are noise for HOA matching.
# Reject all-numeric, "PARCEL N", "LOT 1", commercial/industrial-only plats.
NOISE_NAME_RE = re.compile(
    r"^(\d+|PARCEL\s+\d+|LOT\s+\d+|TR\s+|TRACT\s+\d+|"
    r".*\bCOMMERCIAL\b.*|.*\bINDUSTRIAL\b.*|.*\bBUSINESS\s+PARK\b.*|"
    r".*\bSHOPPING\s+CENTER\b.*|.*\bMINI\s+STORAGE\b.*)$",
    re.IGNORECASE,
)

UA = "Mozilla/5.0 (compatible; hoaproxy-az-subdpoly/0.1)"


def fetch_page(url: str, offset: int, page_size: int, out_sr: str) -> dict:
    params = {
        "where": "1=1",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": out_sr,
        "f": "geojson",
        "resultOffset": str(offset),
        "resultRecordCount": str(page_size),
    }
    full = f"{url}?{urlencode(params)}"
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            req = Request(full, headers={"User-Agent": UA})
            with urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            last_exc = exc
            wait = 2 ** attempt
            print(f"  retry {attempt+1}/5 after {wait}s: {exc}", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"fetch_page failed at offset={offset}: {last_exc}")


def download_county(slug: str, src: dict, force: bool = False) -> Path:
    out = DATA_DIR / f"{slug}_subdivisions.gpkg"
    if out.exists() and out.stat().st_size > 1000 and not force:
        print(f"[{slug}] reusing cached {out.name}")
        return out

    features: list[dict] = []
    offset = 0
    page_size = src["page_size"]
    while True:
        print(f"[{slug}] fetching offset={offset} ...")
        page = fetch_page(src["url"], offset, page_size, src["out_sr"])
        feats = page.get("features", [])
        if not feats:
            break
        features.extend(feats)
        if len(feats) < page_size:
            break
        offset += page_size
        time.sleep(0.4)
    if not features:
        raise RuntimeError(f"[{slug}] empty download")
    print(f"[{slug}] total features: {len(features)}")

    gdf = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
    name_field = src["name_field"]
    if name_field in gdf.columns and name_field != "NAME":
        gdf = gdf.rename(columns={name_field: "NAME"})
    gdf.to_file(out, driver="GPKG")
    print(f"[{slug}] wrote {out} ({len(gdf)} rows)")
    return out


def export_jsonl(slug: str, gpkg: Path, jsonl_handle, name_field: str) -> tuple[int, int]:
    """Append one row per named subdivision to the open JSONL handle.

    Returns (kept, dropped_noise).
    """
    gdf = gpd.read_file(gpkg)
    # GPKG lowercases column names. Look for the source-level name_field
    # case-insensitively, falling back to a few common candidates.
    candidates = [name_field, "NAME", "name", "subname", "sub_name", "SUB_NAME", "SUBNAME"]
    name_col = None
    lower_to_actual = {c.lower(): c for c in gdf.columns}
    for cand in candidates:
        if cand.lower() in lower_to_actual:
            name_col = lower_to_actual[cand.lower()]
            break
    if name_col is None:
        raise RuntimeError(
            f"[{slug}] no name column in {gpkg} (have: {list(gdf.columns)})"
        )
    if name_col != "NAME":
        gdf = gdf.rename(columns={name_col: "NAME"})
    gdf["NAME"] = gdf["NAME"].fillna("").astype(str).str.strip()
    gdf = gdf[gdf["NAME"] != ""]
    metric = gdf.to_crs("EPSG:32612")
    kept = 0
    dropped = 0
    seen_names: set[str] = set()  # dedup within county by exact NAME
    for i in range(len(gdf)):
        name = gdf["NAME"].iloc[i]
        if NOISE_NAME_RE.match(name):
            dropped += 1
            continue
        # Dedup multi-row subdivisions: union geometry by exact NAME
        if name in seen_names:
            continue
        seen_names.add(name)
        same = metric[metric["NAME"] == name]
        if len(same) == 0:
            continue
        if len(same) == 1:
            geom_metric = same.iloc[0].geometry
        else:
            try:
                geom_metric = same.unary_union
            except Exception:
                # Topology errors — repair geometries with buffer(0) and retry.
                try:
                    repaired = same.geometry.buffer(0)
                    geom_metric = repaired.union_all()
                except Exception:
                    # Fall back to the largest single geometry by area.
                    same_with_area = same.assign(_a=same.geometry.area)
                    geom_metric = same_with_area.sort_values("_a", ascending=False).iloc[0].geometry
        if geom_metric is None or geom_metric.is_empty:
            continue
        cent_metric = geom_metric.centroid
        cent_ll = gpd.GeoSeries([cent_metric], crs="EPSG:32612").to_crs("EPSG:4326").iloc[0]
        # Project geom back to lat/lon for boundary
        same_ll = gdf[gdf["NAME"] == name]
        if len(same_ll) > 1:
            try:
                geom_ll = same_ll.unary_union
            except Exception:
                try:
                    geom_ll = same_ll.geometry.buffer(0).union_all()
                except Exception:
                    same_ll_with_area = same_ll.assign(_a=same_ll.geometry.area)
                    geom_ll = same_ll_with_area.sort_values("_a", ascending=False).iloc[0].geometry
        else:
            geom_ll = same_ll.iloc[0].geometry
        from shapely.geometry import mapping
        area_acres = geom_metric.area / 4046.86
        row = {
            "name": name,
            "county": slug,
            "centroid": {"lat": float(cent_ll.y), "lon": float(cent_ll.x)},
            "area_acres": round(area_acres, 2),
            "boundary_geojson": mapping(geom_ll),
            "source": "subdpoly-arcgis",
            "source_county": slug,
        }
        jsonl_handle.write(json.dumps(row, sort_keys=True) + "\n")
        kept += 1
    return kept, dropped


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--counties", default="maricopa,pima",
                   help="Comma-separated AZ county slugs (default: maricopa,pima)")
    p.add_argument("--force", action="store_true",
                   help="Re-download even if GPKG cache exists")
    p.add_argument("--no-jsonl", action="store_true",
                   help="Skip writing the unified JSONL seed file")
    args = p.parse_args()

    requested = [c.strip() for c in args.counties.split(",") if c.strip()]
    for c in requested:
        if c not in SOURCES:
            print(f"unknown county slug: {c} (have: {list(SOURCES)})", file=sys.stderr)
            return 1

    started = time.time()
    gpkgs: list[tuple[str, Path]] = []
    for slug in requested:
        src = SOURCES[slug]
        gpkg = download_county(slug, src, force=args.force)
        gpkgs.append((slug, gpkg))

    if not args.no_jsonl:
        out_jsonl = DATA_DIR / "az_subdpoly.jsonl"
        total_kept = 0
        total_dropped = 0
        with open(out_jsonl, "w") as fh:
            for slug, gpkg in gpkgs:
                kept, dropped = export_jsonl(slug, gpkg, fh, SOURCES[slug]["name_field"])
                print(f"[{slug}] jsonl rows: kept={kept} dropped_noise={dropped}")
                total_kept += kept
                total_dropped += dropped
        print(f"\nWrote {total_kept:,} rows ({total_dropped:,} noise) to {out_jsonl}")

    print(f"Wall time: {(time.time()-started)/60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
