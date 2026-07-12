"""
Phase 8 (boundary construction) tests — from the documented contract:
`build_boundary` returns "a Polygon/MultiPolygon (holes filled, all parts kept),
or None if there are no served units."

The whole reason this phase exists (roadmap Phase 8): parcels don't tile the
landscape, so their dissolved union is full of street/ROW holes and doesn't look
like a real catchment. The boundary method must fill those interior gaps into a
solid polygon. So the intent-level assertions are: (1) an interior hole in the
served union is gone in the boundary, and (2) empty input yields None, not a
crash.

Toy geometry only — no data/ or config.yaml. Runs on a clean clone.

Run:  python -m pytest tests/test_boundary.py
"""

import sys
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from boundary import build_boundary   # noqa: E402

CRS = "EPSG:2264"


def _donut_parcels():
    """8 cells of a 3x3 grid (100 ft cells) with the centre cell missing —
    a square 'donut' whose dissolved union has one interior hole."""
    cells = []
    for i in range(3):
        for j in range(3):
            if (i, j) == (1, 1):
                continue                      # leave the centre empty -> a hole
            cells.append(box(i * 100, j * 100, i * 100 + 100, j * 100 + 100))
    return gpd.GeoDataFrame(geometry=cells, crs=CRS)


def _interior_ring_count(geom):
    """Number of holes across all parts of a (Multi)Polygon."""
    parts = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    return sum(len(p.interiors) for p in parts)


@pytest.mark.parametrize("method", ["delaunay", "morph_close"])
def test_boundary_fills_interior_hole(method):
    parcels = _donut_parcels()
    union = parcels.geometry.union_all()
    assert _interior_ring_count(union) == 1        # the raw union really has a hole

    geom = build_boundary(parcels, method, close_ft=100.0, delaunay_max_edge_ft=500.0)

    # Intent: the street/ROW hole is filled. RED against returning the raw
    # dissolved union (which keeps the hole).
    assert geom is not None and not geom.is_empty
    assert _interior_ring_count(geom) == 0
    # Filling the hole strictly grows the covered area.
    assert geom.area > union.area


def test_boundary_empty_input_returns_none():
    empty = gpd.GeoDataFrame(geometry=[], crs=CRS)
    # Intent: no served units -> no polygon, not a crash. RED against a version
    # that calls union_all() on empty geometry and raises.
    assert build_boundary(empty, "delaunay") is None
