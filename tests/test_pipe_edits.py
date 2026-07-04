"""
Pipe-edit tests (flip / delete / extend), drafted from Grace's QC-round-2 intent:

  "Pipe 41145 is pointing in the wrong direction. Please flip."
  "pipe 54796 and 54794 can be ignored/deleted."
  "pipe 12071 should be extended to MH 20198."

Toy geometry, one case per edit type, plus the locator and validation rules:

  flip     reverses the pipe's coordinate order (direction inverts) — pidx kept.
  delete   empties the geometry so it forms no graph edge — pidx kept, other
           pipes unaffected.
  extend   moves the endpoint nearer the target manhole onto it, so the pipe
           connects there; orientation preserved.
  locate   FACILITYID first; x/y disambiguates a duplicate id; a locator beyond
           tolerance raises rather than editing the wrong pipe.
  schema   qa_review accepts/validates the three new decisions and the target
           column; a snap row still requires x/y.

Run:  python -m pytest tests/test_pipe_edits.py -q
"""

import sys
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pipe_edits import apply_edits, _locate_pipe
from graph_builder import build_graph

CRS = "EPSG:2264"


def _pipes(rows):
    return gpd.GeoDataFrame(
        {"FACILITYID": [fid for fid, _ in rows]},
        geometry=[LineString(c) for _, c in rows],
        crs=CRS,
    )


def _manholes(points):
    return gpd.GeoDataFrame(
        {"FACILITYID": [fid for fid, _ in points]},
        geometry=[Point(xy) for _, xy in points],
        crs=CRS,
    )


def test_flip_reverses_geometry_and_direction():
    pipes = _pipes([("41145", [(0, 0), (100, 0)])])
    out, log = apply_edits(pipes, [{"decision": "flip", "pipe_id": "41145",
                                    "x": None, "y": None, "target": ""}])
    assert list(out.geometry.iloc[0].coords) == [(100.0, 0.0), (0.0, 0.0)]
    assert len(out) == 1                     # pidx preserved
    assert log.iloc[0]["decision"] == "flip"

    G = build_graph(out, snap_tol_ft=1.0, snap_gap_search_radius_ft=0.0)
    (u, v, d), = [(u, v, d) for u, v, d in G.edges(data=True)]
    # Edge now runs start->end of the reversed line: from (100,0) to (0,0).
    assert (round(G.nodes[u]["x"]), round(G.nodes[v]["x"])) == (100, 0)


def test_delete_empties_geometry_keeps_pidx():
    pipes = _pipes([
        ("54794", [(0, 0), (50, 0)]),
        ("54796", [(50, 0), (100, 0)]),
        ("KEEP",  [(100, 0), (200, 0)]),
    ])
    edits = [{"decision": "delete", "pipe_id": f, "x": None, "y": None, "target": ""}
             for f in ("54794", "54796")]
    out, log = apply_edits(pipes, edits)
    assert len(out) == 3                                   # rows kept
    assert out.geometry.iloc[0].is_empty and out.geometry.iloc[1].is_empty
    assert not out.geometry.iloc[2].is_empty
    # The two deleted pipes form no edges; only KEEP survives in the graph.
    G = build_graph(out, snap_tol_ft=1.0, snap_gap_search_radius_ft=0.0)
    assert G.number_of_edges() == 1
    assert {d["facilityid"] for _, _, d in G.edges(data=True)} == {"KEEP"}
    # Flag point captured before the geometry was emptied (mid-line).
    assert abs(log.iloc[0]["x"] - 25) < 1e-6


def test_extend_moves_nearer_endpoint_to_manhole():
    # Pipe ends 6 ft short of MH 20198; must reach it and connect to the mains.
    # The MH is a real junction — two mains meet there endpoint-to-endpoint.
    pipes = _pipes([
        ("12071",  [(0, 0), (0, 94)]),          # end at (0,94), MH at (0,100)
        ("MAIN_A", [(-50, 100), (0, 100)]),     # ends at the MH
        ("MAIN_B", [(0, 100), (50, 100)]),      # starts at the MH
    ])
    mh = _manholes([("20198", (0, 100))])
    out, log = apply_edits(
        pipes,
        [{"decision": "extend", "pipe_id": "12071", "x": None, "y": None,
          "target": "20198"}],
        manholes=mh,
    )
    coords = list(out.geometry.iloc[0].coords)
    assert coords[0] == (0.0, 0.0)                         # start untouched
    assert coords[-1] == (0.0, 100.0)                      # end moved to MH
    assert "extended to" in log.iloc[0]["detail"]

    G = build_graph(out, snap_tol_ft=1.0, snap_gap_search_radius_ft=0.0)
    def node_at(x, y):
        return min(G.nodes,
                   key=lambda n: (G.nodes[n]["x"] - x) ** 2 + (G.nodes[n]["y"] - y) ** 2)
    # 12071's far end now shares the MH node with the main -> connected.
    assert node_at(0, 0) in __import__("networkx").node_connected_component(
        G.to_undirected(), node_at(50, 100))


def test_extend_target_xy_literal():
    pipes = _pipes([("P", [(0, 0), (0, 94)])])
    out, _ = apply_edits(
        pipes,
        [{"decision": "extend", "pipe_id": "P", "x": None, "y": None,
          "target": "0,100"}],
    )
    assert list(out.geometry.iloc[0].coords)[-1] == (0.0, 100.0)


def test_locate_disambiguates_duplicate_fid_by_xy():
    pipes = _pipes([
        ("DUP", [(0, 0), (0, 100)]),
        ("DUP", [(500, 0), (500, 100)]),        # same FID, far away
    ])
    # x/y near the second one selects it.
    got = _locate_pipe(pipes, {"decision": "flip", "pipe_id": "DUP",
                               "x": 500, "y": 50}, locate_tol_ft=50.0)
    assert got == 1


def test_locate_out_of_tolerance_raises():
    pipes = _pipes([("A", [(0, 0), (0, 100)])])
    with pytest.raises(ValueError, match="no pipe within"):
        _locate_pipe(pipes, {"decision": "flip", "pipe_id": "NOPE",
                             "x": 9999, "y": 9999}, locate_tol_ft=50.0)


def test_qa_review_parses_edit_rows(tmp_path):
    from qa_review import load_review_decisions, pipe_edits
    csv = tmp_path / "decisions.csv"
    csv.write_text(
        "flag_type,pipe_id,x,y,decision,radius_ft,target,comment\n"
        ",41145,,,flip,,,backwards\n"
        ",54794,,,delete,,,stray\n"
        ",12071,,,extend,,20198,reach the MH\n"
        "snap_gap,00322 / 53297,2000,3000,snap,,,real gap\n",
        encoding="utf-8",
    )
    decisions = load_review_decisions(csv)
    edits = pipe_edits(decisions)
    assert [e["decision"] for e in edits] == ["flip", "delete", "extend"]
    assert edits[2]["target"] == "20198"
    # snap still parsed too (not an edit).
    assert any(d["decision"] == "snap" for d in decisions)


def test_qa_review_snap_requires_xy(tmp_path):
    from qa_review import load_review_decisions
    csv = tmp_path / "decisions.csv"
    csv.write_text(
        "flag_type,pipe_id,x,y,decision,radius_ft,target,comment\n"
        "snap_gap,X,,,snap,,,missing coords\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="snap decision needs x and y"):
        load_review_decisions(csv)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
