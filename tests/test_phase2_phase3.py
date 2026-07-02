"""
Phase 2 & 3 sign-off tests, per the roadmap's own criteria.

Phase 2 (network QA): "introduce known direction error and snap gap in a copy
of the data; confirm both flags fire, PDF pages generate at correct locations,
repaired shapefile has correct QA_STATUS values." Implemented on a synthetic
network with one planted defect per flag type; asserts each flag fires at the
right location with the right severity, QA_STATUS is set, and — the Phase 2
contract — geometry and attributes are never modified, only annotated.

Phase 3 (graph builder): "load network, confirm node count, visualize edge
directions" — asserted programmatically: every edge's direction matches its
line geometry (start node at the first vertex, end node at the last), node
and edge counts reconcile, and the planted flipped pipe surfaces as the one
non-trivial SCC. (Direction inference from inverts was superseded by the
2026-06-24 geometry-first redesign; inverts are cross-checks only.)

Run:  python tests/test_phase2_phase3.py     (from the project root)
"""

import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString, Point

from graph_builder import build_graph, summarize_graph
from network_qa import run_qa

CRS = "EPSG:2264"

# Synthetic network — one planted defect per QA check. Clusters are spatially
# separate so each check is isolated; all become small disconnected fragments,
# which the disconnected_component check must count.
PIPES = [
    # fid, geometry, FROMMH, TOMH, slope, up_inv, dn_inv
    # main chain (clean) + 2-pipe directed cycle at its end
    ("P01", [(0, 0), (100, 0)],      "MH1", "MH2", 1.0,  10.0, 9.0),
    ("P02", [(100, 0), (200, 0)],    "MH2", "MH3", 1.0,  9.0,  8.0),
    ("P03", [(200, 0), (300, 0)],    "MH3", "MH4", 1.0,  8.0,  7.0),
    ("P04", [(300, 0), (200, 0)],    "MH4", "MH3", 1.0,  7.0,  8.0),   # flipped -> cycle
    # tier-1 snap gap: end-to-end 5 ft apart (auto-repaired, warning)
    ("P05", [(0, 200), (100, 200)],  "MH5", "MH6", 1.0,  10.0, 9.0),
    ("P06", [(105, 200), (200, 200)],"MH6", "MH7", 1.0,  9.0,  8.0),
    # tier-2 snap gap: dangling end 3 ft from a junction (never auto-repaired)
    ("P07", [(400, -100), (500, 0)], "MH8", "MH9", 1.0,  10.0, 9.0),
    ("P08", [(500, 0), (600, 0)],    "MH9", "MH10", 1.0, 9.0,  8.0),
    ("P09", [(500, 100), (500, 3)],  "MH11", "MH9", 1.0, 10.0, 9.5),
    # invert conflict: water would climb 5 -> 6
    ("P10", [(0, 300), (100, 300)],  "MH12", "MH13", 1.0, 5.0, 6.0),
    # missing direction: null FROMMH/TOMH
    ("P11", [(0, 400), (100, 400)],  None,  None,  1.0,  10.0, 9.0),
    # negative slope attribute
    ("P12", [(0, 500), (100, 500)],  "MH14", "MH15", -0.5, 10.0, 9.0),
]

MANHOLES = [
    ("MH1", (0, 0)), ("MH2", (100, 0)), ("MH3", (200, 0)),
    ("MH_ISO", (9999, 9999)),            # isolated: no pipe refs, far from nodes
]


def _write_inputs(tmp: Path):
    pipes = gpd.GeoDataFrame(
        {"FACILITYID": [p[0] for p in PIPES],
         "FROMMH":     [p[2] for p in PIPES],
         "TOMH":       [p[3] for p in PIPES],
         "SLOPE":      [p[4] for p in PIPES],
         "UPSTREAMIN": [p[5] for p in PIPES],
         "DOWNSTREAM": [p[6] for p in PIPES]},
        geometry=[LineString(p[1]) for p in PIPES], crs=CRS)
    manholes = gpd.GeoDataFrame(
        {"FACILITYID": [m[0] for m in MANHOLES]},
        geometry=[Point(m[1]) for m in MANHOLES], crs=CRS)
    pipes.to_file(tmp / "pipes.shp")
    manholes.to_file(tmp / "manholes.shp")
    return pipes


def _cfg(tmp: Path) -> dict:
    return {
        "inputs": {
            "gravity_main_shapefile": str(tmp / "pipes.shp"),
            "manholes_shapefile": str(tmp / "manholes.shp"),
        },
        "parameters": {
            "crs": CRS,
            "node_snap_tolerance_ft": 1.0,
            "snap_gap_search_radius_ft": 10.0,
            "manhole_node_snap_ft": 5.0,
            "max_mappable_cycle_nodes": 10,
            "disconnected_component_max_nodes": 50,
        },
    }


def _by_type(flags):
    out = {}
    for f in flags:
        out.setdefault(f["flag_type"], []).append(f)
    return out


def test_phase2_every_check_fires_at_the_planted_defect():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pipes_in = _write_inputs(tmp)
        pipes_out, manholes, flags = run_qa(_cfg(tmp))
        ft = _by_type(flags)

        # direction error -> one small directed cycle over P03/P04
        # (membership is documented as pipes touching an SCC node, so the
        # adjacent P02 rides along; the flipped pair must be present)
        assert len(ft["directed_cycle"]) == 1
        cyc = ft["directed_cycle"][0]
        assert cyc["severity"] == "review_required"
        members = set(cyc["pipe_id"].split(" / "))
        assert {"P03", "P04"} <= members <= {"P02", "P03", "P04"}, members

        # snap gaps: tier 1 (repaired, warning) at (102.5, 200);
        # tier 2 (unconnected, review_required) near (500, 1.5)
        gaps = ft["snap_gap"]
        t1 = [g for g in gaps if g["severity"] == "warning"]
        t2 = [g for g in gaps if g["severity"] == "review_required"]
        assert len(t1) == 1 and len(t2) == 1
        assert math.hypot(t1[0]["geometry"].x - 102.5,
                          t1[0]["geometry"].y - 200) < 1.0
        assert math.hypot(t2[0]["geometry"].x - 500,
                          t2[0]["geometry"].y - 1.5) < 2.0

        # invert conflicts: the planted P10, plus P04 — the flipped pipe's
        # inverts disagree with its geometry, corroborating the cycle flag
        # exactly as designed. All warnings (geometry stays authoritative).
        ic_ids = {f["pipe_id"] for f in ft["invert_conflict"]}
        assert ic_ids == {"P04", "P10"}, ic_ids
        assert all(f["severity"] == "warning" for f in ft["invert_conflict"])

        # missing direction on P11, and its QA_STATUS is direction_inferrable
        (md,) = ft["missing_direction"]
        assert md["pipe_id"] == "P11"
        assert pipes_out.loc[pipes_out.FACILITYID == "P11",
                             "QA_STATUS"].iloc[0] == "direction_inferrable"

        # negative slope on P12
        (ns,) = ft["attribute_slope_error"]
        assert ns["pipe_id"] == "P12"

        # isolated manhole: MH_ISO only (MH1-3 are referenced by FROMMH/TOMH)
        iso_ids = {f["pipe_id"] for f in ft["isolated_manhole"]}
        assert iso_ids == {"MH_ISO"}, iso_ids

        # every spatially separate cluster is a small fragment. 7, not 6:
        # the tier-2 gap keeps P09 out of the P07/P08 component — which is
        # precisely what that flag is warning about.
        assert len(ft["disconnected_component"]) == 7, \
            len(ft["disconnected_component"])

        # per-pipe annotation: every cluster here is a small fragment, so the
        # "clean" pipe P01 carries only the fragment tag — no defect flags
        p1 = pipes_out.loc[pipes_out.FACILITYID == "P01"].iloc[0]
        assert p1["QA_FLAGS"] == "disconnected_component", p1["QA_FLAGS"]
        p3 = pipes_out.loc[pipes_out.FACILITYID == "P03"].iloc[0]
        assert p3["QA_STATUS"] == "flagged"
        assert "directed_cycle" in p3["QA_FLAGS"]

        # Phase 2 contract: flag-only — geometry and attributes unmodified
        assert all(a.equals(b) for a, b
                   in zip(pipes_in.geometry, pipes_out.geometry))
        for col in ["FROMMH", "TOMH", "SLOPE", "UPSTREAMIN", "DOWNSTREAM"]:
            assert list(pipes_in[col].fillna("~")) == \
                   list(pipes_out[col].fillna("~")), col


def test_phase3_direction_follows_geometry_and_counts_reconcile():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_inputs(tmp)
        pipes = gpd.read_file(tmp / "pipes.shp")
        G = build_graph(pipes, snap_tol_ft=1.0, snap_gap_search_radius_ft=10.0)

        # one edge per pipe; every edge oriented start -> end of its geometry
        assert G.number_of_edges() == len(pipes)
        for u, v, d in G.edges(data=True):
            geom = pipes.geometry.iloc[d["pidx"]]
            sx, sy = geom.coords[0]
            ex, ey = geom.coords[-1]
            du = math.hypot(G.nodes[u]["x"] - sx, G.nodes[u]["y"] - sy)
            dv = math.hypot(G.nodes[v]["x"] - ex, G.nodes[v]["y"] - ey)
            # node centroids can shift by up to the repair radius after merges
            assert du <= 10.0 and dv <= 10.0, d["facilityid"]

        # the planted flip is the one and only non-trivial SCC
        s = summarize_graph(G)
        assert s["n_nontrivial_sccs"] == 1
        assert s["largest_scc"] == 2

        # tier-1 gap merged (P05 end and P06 start share a node);
        # tier-2 gap NOT merged (P09 end stays apart from the P07/P08 junction)
        def node_of(fid, which):
            for u, v, d in G.edges(data=True):
                if d["facilityid"] == fid:
                    return u if which == "start" else v
        assert node_of("P05", "end") == node_of("P06", "start")
        assert node_of("P09", "end") != node_of("P08", "start")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS  {t.__name__}")
    print(f"\n{len(tests)} tests passed.")
