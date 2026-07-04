"""
Midspan-junction split tests (2026-07-03), drafted from Grace's stated intent:
"If there is a MH in the middle of the pipe, then split the pipe. For entire
the city network" — plus the connectivity requirement that motivated it: a
lateral whose endpoint lands on a main's interior (the 62112 / 34720 / MH 58048
case) must trace as part of the network after the split.

Toy geometry, one case per rule:
  1. T-junction (lateral endpoint + MH on the main's interior) -> main is
     split, the lateral is upstream of the main's downstream end in the graph.
  2. MH mid-pipe with nothing tying in -> split happens, network continuity is
     preserved through the new node.
  3. Junction point within the endpoint-exclusion radius of the main's own
     endpoint -> NOT split (belongs to the snap/snap_gap machinery).
  4. Two junctions on one pipe -> three segments, cut in order.
  5. No junctions -> pipes returned unchanged, empty log.
  6. pidx stability: the parent row keeps the first segment; appended rows copy
     all attributes; every segment preserves the parent's orientation
     (geometry-first direction rule).

Run:  python -m pytest tests/test_pipe_splits.py    (from the project root)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import geopandas as gpd
import networkx as nx
from shapely.geometry import LineString, Point

from pipe_splits import find_midspan_junctions, split_pipes_at_junctions
from graph_builder import build_graph

CRS = "EPSG:2264"
INT_TOL = 2.0
END_EXC = 10.0


def _pipes(rows):
    """rows: list of (fid, coords). Returns a pipes GeoDataFrame."""
    return gpd.GeoDataFrame(
        {"FACILITYID": [fid for fid, _ in rows]},
        geometry=[LineString(c) for _, c in rows],
        crs=CRS,
    )


def _manholes(points):
    """points: list of (fid, (x, y)). Returns a manholes GeoDataFrame."""
    return gpd.GeoDataFrame(
        {"FACILITYID": [fid for fid, _ in points]},
        geometry=[Point(xy) for _, xy in points],
        crs=CRS,
    )


def _split(pipes, manholes):
    junctions = find_midspan_junctions(pipes, manholes, INT_TOL, END_EXC)
    return split_pipes_at_junctions(pipes, junctions)


def test_tee_splits_and_traces():
    """Lateral endpoint + MH on the main's interior: split, then the lateral
    is upstream of the main's downstream end (the 62112/34720 case)."""
    pipes = _pipes([
        ("MAIN", [(0, 0), (100, 0)]),          # main, flows +x
        ("LAT",  [(50, 30), (50, 0.5)]),       # lateral, ends 0.5 ft off the main's interior
    ])
    mh = _manholes([("MH-T", (50, 0))])
    out, log = _split(pipes, mh)

    assert len(out) == 3                       # main became two segments
    assert len(log) == 1
    assert log.iloc[0]["parent_pidx"] == 0
    assert log.iloc[0]["trigger"] == "endpoint+manhole"
    assert log.iloc[0]["new_pidx"] == 2

    # Parent row keeps the first segment (0,0)->(50,0); appendee gets the rest.
    seg1, seg2 = out.geometry.iloc[0], out.geometry.iloc[2]
    assert seg1.coords[0] == (0.0, 0.0) and abs(seg1.coords[-1][0] - 50) < 0.01
    assert abs(seg2.coords[0][0] - 50) < 0.01 and seg2.coords[-1] == (100.0, 0.0)
    assert out.iloc[2]["FACILITYID"] == "MAIN"  # attributes copied

    # Connectivity: from the lateral's start you must reach the main's end.
    G = build_graph(out, snap_tol_ft=1.0, snap_gap_search_radius_ft=0.0)
    def node_at(x, y):
        return min(G.nodes,
                   key=lambda n: (G.nodes[n]["x"] - x) ** 2 + (G.nodes[n]["y"] - y) ** 2)
    assert nx.has_path(G, node_at(50, 30), node_at(100, 0))
    # And the reverse trace from the main's end sees the lateral (the actual
    # delineation direction).
    assert node_at(50, 30) in nx.ancestors(G, node_at(100, 0))


def test_mh_only_split_preserves_continuity():
    """MH mid-pipe, nothing tying in: split adds a node without breaking flow."""
    pipes = _pipes([("MAIN", [(0, 0), (100, 0)])])
    mh = _manholes([("MH-MID", (30, 0.5))])
    out, log = _split(pipes, mh)

    assert len(out) == 2
    assert log.iloc[0]["trigger"] == "manhole"
    G = build_graph(out, snap_tol_ft=1.0, snap_gap_search_radius_ft=0.0)
    assert G.number_of_nodes() == 3 and G.number_of_edges() == 2
    def node_at(x, y):
        return min(G.nodes,
                   key=lambda n: (G.nodes[n]["x"] - x) ** 2 + (G.nodes[n]["y"] - y) ** 2)
    assert nx.has_path(G, node_at(0, 0), node_at(100, 0))


def test_near_endpoint_not_split():
    """A junction within endpoint_exclusion of the main's own endpoint is left
    to the snap/snap_gap machinery — no split, no phantom sliver."""
    pipes = _pipes([
        ("MAIN", [(0, 0), (100, 0)]),
        ("LAT",  [(95, 30), (95, 0.5)]),       # 5 ft from MAIN's end -> excluded
    ])
    mh = _manholes([("MH-E", (95, 0))])
    out, log = _split(pipes, mh)
    assert len(out) == 2 and log.empty


def test_two_junctions_one_pipe():
    """Two independent tees on one main -> three segments, cut in order."""
    pipes = _pipes([
        ("MAIN", [(0, 0), (100, 0)]),
        ("LAT1", [(30, 30), (30, 0.5)]),
        ("LAT2", [(70, 30), (70, 0.5)]),
    ])
    out, log = _split(pipes, None)
    assert len(out) == 5                       # 3 originals + 2 appended segments
    assert len(log) == 2
    assert list(log["dist_along"]) == sorted(log["dist_along"])
    lengths = sorted(round(out.geometry.iloc[i].length) for i in (0, 3, 4))
    assert lengths == [30, 30, 40]             # 0-30, 30-70, 70-100


def test_no_junctions_identity():
    """Clean layer: unchanged frame, empty log."""
    pipes = _pipes([
        ("A", [(0, 0), (100, 0)]),
        ("B", [(100, 0), (200, 0)]),           # endpoint-to-endpoint: normal snap
    ])
    out, log = _split(pipes, _manholes([("MH1", (100, 0))]))
    assert len(out) == 2 and log.empty
    assert out.geometry.iloc[0].equals(pipes.geometry.iloc[0])


def test_weld_band_endpoint_connects():
    """Code-review regression: a lateral endpoint 1-2 ft off the main is beyond
    the 1 ft pass-1 snap, so without the weld pass the pipe would be split but
    the lateral left disconnected. The weld must move the endpoint onto the cut
    so the trace crosses the tee."""
    pipes = _pipes([
        ("MAIN", [(0, 0), (100, 0)]),
        ("LAT",  [(50, 30), (50, 1.5)]),       # 1.5 ft off: in the weld band
    ])
    out, log = _split(pipes, None)
    assert len(log) == 1
    # Welded: the lateral's downstream endpoint now sits exactly on the cut.
    lat_end = out.geometry.iloc[1].coords[-1]
    assert abs(lat_end[0] - log.iloc[0]["x"]) < 1e-9
    assert abs(lat_end[1] - log.iloc[0]["y"]) < 1e-9

    G = build_graph(out, snap_tol_ft=1.0, snap_gap_search_radius_ft=0.0)
    def node_at(x, y):
        return min(G.nodes,
                   key=lambda n: (G.nodes[n]["x"] - x) ** 2 + (G.nodes[n]["y"] - y) ** 2)
    assert node_at(50, 30) in nx.ancestors(G, node_at(100, 0))


def test_orientation_preserved():
    """Every segment keeps the parent's flow orientation (start = upstream)."""
    pipes = _pipes([
        ("MAIN", [(100, 0), (0, 0)]),          # flows -x (reversed on purpose)
        ("LAT",  [(50, 30), (50, 0.5)]),
    ])
    out, log = _split(pipes, None)
    assert len(out) == 3
    seg1, seg2 = out.geometry.iloc[0], out.geometry.iloc[2]
    # Parent flowed 100 -> 0, so segment order along the flow is (100..50), (50..0).
    assert seg1.coords[0][0] > seg1.coords[-1][0]
    assert seg2.coords[0][0] > seg2.coords[-1][0]
    assert abs(seg1.coords[-1][0] - 50) < 0.01 and abs(seg2.coords[0][0] - 50) < 0.01


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
