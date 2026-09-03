"""
Force-main graph-wiring tests — from the stated intent:

"Once I've confirmed which end of a force main is the pump station, the trace
should actually follow it: standing at a manhole below the discharge, tracing
upstream should pull in the whole basin above the wet well. Only wire the ones
I've settled — anything still in review shouldn't quietly show up in a trace.
And a force main is pressurized, so nobody connects to it; it shouldn't add any
population."

Toy geometry only — no data/ or config.yaml. Runs on a clean clone.

Run:  python -m pytest tests/test_force_main_wiring.py
"""

import sys
from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from force_main_topology import build_topology                   # noqa: E402
from force_main_classify import classify_termini, component_verdicts  # noqa: E402
from force_main_wiring import (                                   # noqa: E402
    build_force_main_edges, add_force_main_edges, load_force_main_edges,
    cycles_through_force_mains, FM_LAYER,
)
from graph_builder import build_node_index                        # noqa: E402
from traversal import traverse_upstream, TraversalResult          # noqa: E402

CRS = "EPSG:2264"


def _fm(lines, fids=None):
    n = len(lines)
    return gpd.GeoDataFrame(
        {"FACILITYID": fids or [f"{i:05d}" for i in range(n)],
         "DIAMETER": [8.0] * n,
         "OWNER": ["DNC"] * n,
         "LIFECYCLES": ["Active"] * n},
        geometry=[LineString(c) for c in lines], crs=CRS)


def _gravity(nodes, edges):
    G = nx.MultiDiGraph()
    for nid, (x, y) in nodes.items():
        G.add_node(nid, x=float(x), y=float(y), role="junction")
    for i, (u, v) in enumerate(edges):
        G.add_edge(u, v, pidx=i, facilityid=f"G{i}", length_ft=10.0)
    for nid in G.nodes:
        has_in, has_out = G.in_degree(nid) > 0, G.out_degree(nid) > 0
        G.nodes[nid]["role"] = ("junction" if has_in and has_out
                                else "start_only" if has_out else "end_only")
    return G


def _one_station():
    """
    The canonical layout this feature exists for.

    Gravity basin A (nodes 1->2->3) dead-ends at a wet well (node 3, nothing
    flows out). A force main runs from there to node 4, which starts a second
    gravity run 4->5. Gravity-only, a trace from node 5 sees basin B and stops
    at node 4; it can never reach basin A.

        1 -> 2 -> 3  ):(wet well)      4 -> 5
                   \\___ force main ___/
    """
    G = _gravity(
        {1: (0, 0), 2: (100, 0), 3: (200, 0), 4: (600, 0), 5: (700, 0)},
        [(1, 2), (2, 3), (4, 5)])
    fm = _fm([[(200, 0), (600, 0)]], fids=["FM01"])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    verdicts = component_verdicts(topo, termini, fm)
    return G, fm, topo, termini, verdicts


# ---------------------------------------------------------------------------
# The point of the feature: a trace crosses the station
# ---------------------------------------------------------------------------

def test_trace_reaches_the_basin_above_the_pump_station():
    """The whole reason this exists — without wiring, basin A is unreachable."""
    G, fm, topo, termini, verdicts = _one_station()
    assert verdicts.verdict.tolist() == ["resolved"], "setup must be settled"

    before, _, _ = traverse_upstream(G, 5)
    assert {e["facilityid"] for e in before} == {"G2"}, \
        "gravity-only, node 5 sees only its own run"

    edges, _ = build_force_main_edges(termini, verdicts, fm, topo)
    add_force_main_edges(G, edges)

    after, nodes, _ = traverse_upstream(G, 5)
    gravity_after = {e["facilityid"] for e in after if e["pidx"] is not None}
    assert gravity_after == {"G0", "G1", "G2"}, \
        "after wiring, the pumped basin upstream of the wet well is included"
    assert {1, 2, 3, 4} <= nodes


def test_force_main_conveys_but_adds_no_pipe_to_the_population_join():
    """Pressurized main, no service laterals — it must not enter the buffer."""
    G, fm, topo, termini, verdicts = _one_station()
    edges, _ = build_force_main_edges(termini, verdicts, fm, topo)
    add_force_main_edges(G, edges)

    raw, nodes, depth = traverse_upstream(G, 5)
    res = TraversalResult(5, (700, 0), 0.0, "coordinate", "x", raw, nodes, depth)

    assert None not in res.pidx_list, "a None pidx would crash pipes.iloc"
    assert len(res.pidx_list) == 3, "three gravity pipes, not four"
    assert len(res.gravity_edges) == len(res.pidx_list), \
        "gravity_edges and pidx_list must stay aligned — depth maps positionally"
    assert any(e["pidx"] is None for e in res.edges), \
        "the force-main edge is still in the graph, just not in the geometry"


# ---------------------------------------------------------------------------
# Only settled systems get wired
# ---------------------------------------------------------------------------

def test_a_system_still_in_review_is_not_wired():
    """Two possible discharges, direction unsettled — must stay out of the trace."""
    # Both force-main ends land on gravity nodes that still flow onward (2->9
    # and 3->4), so neither reads as a wet well: two discharges, no pump station.
    G = _gravity({1: (0, 0), 2: (200, 0), 9: (200, 300),
                  3: (600, 0), 4: (800, 0)},
                 [(1, 2), (2, 9), (3, 4)])
    fm = _fm([[(200, 0), (600, 0)]], fids=["FM01"])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    verdicts = component_verdicts(topo, termini, fm)
    assert verdicts.verdict.iloc[0] != "resolved"

    edges, skipped = build_force_main_edges(termini, verdicts, fm, topo)
    assert edges.empty, "an unresolved system must not be wired"
    assert "still in review" in skipped.reason.iloc[0]


def test_a_system_ending_at_a_plant_is_skipped_and_said_so():
    """A plant is terminal — correct to skip, but it must be reported, not absent."""
    G, fm, topo, termini, verdicts = _one_station()
    termini = termini.copy()
    termini.loc[termini.classification == "discharge", "classification"] = "terminal"
    verdicts = component_verdicts(topo, termini, fm)
    assert verdicts.verdict.iloc[0] == "terminates_at_facility"

    edges, skipped = build_force_main_edges(termini, verdicts, fm, topo)
    assert edges.empty
    assert len(skipped) == 1, "skipping silently is the failure mode"
    assert "treatment plant" in skipped.reason.iloc[0]


def test_a_candidate_contact_is_never_wired():
    """A candidate is a proposal Grace hasn't accepted — wiring it fabricates flow."""
    G, fm, topo, termini, verdicts = _one_station()
    termini = termini.copy()
    termini["contact"] = "candidate"
    verdicts = component_verdicts(topo, termini, fm)

    edges, skipped = build_force_main_edges(termini, verdicts, fm, topo)
    assert edges.empty
    assert not skipped.empty


# ---------------------------------------------------------------------------
# Two stations sharing one discharge
# ---------------------------------------------------------------------------

def test_both_pump_stations_on_a_shared_discharge_are_wired():
    """Two lift stations into one force main is normal — neither basin may be lost."""
    G = _gravity(
        {1: (0, 0), 2: (200, 0),          # basin A, dead-ends at 2
         3: (0, 400), 4: (200, 400),      # basin B, dead-ends at 4
         5: (600, 200), 6: (700, 200)},   # the shared discharge
        [(1, 2), (3, 4), (5, 6)])
    fm = _fm([[(200, 0), (400, 200)],
              [(200, 400), (400, 200)],
              [(400, 200), (600, 200)]], fids=["FMA", "FMB", "FMC"])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    verdicts = component_verdicts(topo, termini, fm)
    assert verdicts.verdict.iloc[0] == "resolved"

    edges, _ = build_force_main_edges(termini, verdicts, fm, topo)
    assert len(edges) == 2, "one edge per wet well, both into the shared discharge"
    assert set(edges.discharge_x) == {600.0}

    add_force_main_edges(G, edges)
    _, nodes, _ = traverse_upstream(G, 6)
    assert {1, 2, 3, 4, 5} <= nodes, "both pumped basins reach the discharge"


# ---------------------------------------------------------------------------
# Failing loud
# ---------------------------------------------------------------------------

def test_a_coordinate_that_no_longer_matches_a_node_raises(tmp_path):
    """A silent skip would drop a whole basin from every trace with no signal."""
    G, fm, topo, termini, verdicts = _one_station()
    edges, _ = build_force_main_edges(termini, verdicts, fm, topo)
    edges.loc[0, "discharge_x"] = 99999.0        # the graph moved under it

    with pytest.raises(ValueError, match="over the .* tolerance"):
        add_force_main_edges(G, edges, tol_ft=10.0)


def test_a_force_main_that_loops_back_to_its_own_wet_well_is_reported():
    """Discharge draining back to its own wet well inflates every trace through it."""
    G, fm, topo, termini, verdicts = _one_station()
    edges, _ = build_force_main_edges(termini, verdicts, fm, topo)
    add_force_main_edges(G, edges)
    assert cycles_through_force_mains(G).empty, "the clean layout has no loop"

    G.add_edge(4, 1, pidx=99, facilityid="G99", length_ft=10.0)  # 4 drains back to 1
    looped = cycles_through_force_mains(G)
    assert len(looped) == 1
    assert looped.facilityid.iloc[0] == "FM:FM01", "names the force main to look at"


def test_a_station_collapsed_onto_one_gravity_node_is_reported_not_wired():
    """Both ends on one node is a self-loop conveying nothing — say so."""
    G, fm, topo, termini, verdicts = _one_station()
    termini = termini.copy()
    termini["gravity_node"] = 3          # both ends resolve to the same node
    edges, skipped = build_force_main_edges(termini, verdicts, fm, topo)
    assert edges.empty
    assert "same gravity node" in skipped.reason.iloc[0]


# ---------------------------------------------------------------------------
# The artifact round-trips
# ---------------------------------------------------------------------------

def test_edge_list_survives_a_write_and_read(tmp_path):
    """The file is the handoff between the two runners — it must round-trip."""
    G, fm, topo, termini, verdicts = _one_station()
    edges, _ = build_force_main_edges(termini, verdicts, fm, topo)
    path = tmp_path / "force_main_edges.csv"
    edges.to_csv(path, index=False, encoding="utf-8-sig")

    back = load_force_main_edges({"inputs": {"force_main_edges": str(path)}})
    add_force_main_edges(G, back)
    _, nodes, _ = traverse_upstream(G, 5)
    assert {1, 2, 3} <= nodes


def test_no_configured_edge_file_means_gravity_only():
    """Absent key must be distinguishable from 'every system failed review'."""
    assert load_force_main_edges({"inputs": {}}) is None


def test_a_configured_but_missing_edge_file_raises():
    """Silently tracing gravity-only when force mains were expected is the bad case."""
    with pytest.raises(FileNotFoundError, match="run_force_mains"):
        load_force_main_edges({"inputs": {"force_main_edges": "nope.csv"}})


def test_wired_edges_are_labelled_so_gravity_qa_can_exclude_them():
    """A force main is not a gravity pipe; QA must be able to tell them apart."""
    G, fm, topo, termini, verdicts = _one_station()
    edges, _ = build_force_main_edges(termini, verdicts, fm, topo)
    add_force_main_edges(G, edges)
    layers = [d.get("layer") for _, _, d in G.edges(data=True)]
    assert layers.count(FM_LAYER) == 1
    assert layers.count(None) == 3, "gravity pipes carry no layer tag"
