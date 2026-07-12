"""
Phase 4 (upstream traversal) regression tests — drafted from the intended
behaviour, not from the code:

  - "Upstream of a manhole" means *every* pipe whose flow reaches it — both
    branches of a fork, all the way to the headwaters.
  - A headwater (nothing upstream) is a valid empty result, not an error.
  - Traversal must TERMINATE and count each contributing pipe exactly once even
    on degenerate topology (a self-loop, a 2-node cycle) — a corrupt cycle has
    no meaningful "upstream set", so we assert termination + each-edge-once, not
    a set.
  - The false-headwater guard: when the nearest node to a target has nothing
    upstream but a real junction sits at the same manhole (endpoints that didn't
    merge), resolution must pick the junction — otherwise the trace is silently
    empty.
  - A target off the network must fail loud (TargetResolutionError), not snap to
    whatever node happens to be nearest.

Each test is built so it goes RED against the specific bug it guards (noted per
test). Toy geometry only — no data/ or config.yaml, so it runs on a clean clone.

Run:  python -m pytest tests/test_traversal.py
"""

import sys
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graph_builder import build_graph, build_node_index, nearest_node   # noqa: E402
from traversal import (                                                  # noqa: E402
    traverse_upstream, resolve_target_node, TargetResolutionError,
)

CRS = "EPSG:2264"
SNAP_TOL = 1.0          # endpoints within 1 ft merge to one node


def _graph(rows):
    """Build a directed graph from (fid, [(x,y),...]) rows. pidx == row order."""
    pipes = gpd.GeoDataFrame(
        {"FACILITYID": [r[0] for r in rows],
         "FROMMH":     [None] * len(rows),
         "TOMH":       [None] * len(rows),
         "SLOPE":      [1.0] * len(rows),
         "UPSTREAMIN": [10.0] * len(rows),
         "DOWNSTREAM": [9.0] * len(rows)},
        geometry=[LineString(r[1]) for r in rows], crs=CRS)
    return build_graph(pipes, SNAP_TOL)


def _node_at(G, index, x, y):
    node, _dist = nearest_node(index, x, y)
    return node


def _cfg(tol):
    return {"inputs": {}, "parameters": {"manhole_snap_distance_ft": tol}}


# --- upstream set: both branches, to the headwaters -----------------------

def test_upstream_of_outlet_is_every_contributing_pipe():
    # Fork: A->B and C->B, then B->D. D is the outlet; every pipe drains to it.
    #   A(0,0) --0--> B(100,0);  C(100,100) --1--> B;  B --2--> D(200,0)
    G = _graph([
        ("P0", [(0, 0), (100, 0)]),        # A -> B
        ("P1", [(100, 100), (100, 0)]),    # C -> B  (second branch)
        ("P2", [(100, 0), (200, 0)]),      # B -> D  (outlet)
    ])
    idx = build_node_index(G)
    outlet = _node_at(G, idx, 200, 0)
    edges, _nodes, _depth = traverse_upstream(G, outlet)
    got = {e["pidx"] for e in edges}
    # Intent: the whole contributing network, not just the last pipe or one
    # branch. RED against a traversal that stops at the junction or misses P1.
    assert got == {0, 1, 2}


def test_headwater_has_empty_upstream():
    # A(0,0) --0--> B(100,0). A is a headwater: nothing flows into it.
    G = _graph([("P0", [(0, 0), (100, 0)])])
    idx = build_node_index(G)
    headwater = _node_at(G, idx, 0, 0)
    edges, _nodes, _depth = traverse_upstream(G, headwater)
    # Intent: empty is valid, not a crash. RED against code that errors/returns
    # the outgoing pipe for a zero-in-degree node.
    assert edges == []


# --- degenerate topology must terminate, count each edge once -------------

def test_self_loop_terminates_and_counts_once():
    # Self-loop at S plus S->E. Reverse-walking from E hits the self-loop.
    G = _graph([
        ("SELF", [(300, 300), (300, 300)]),   # S -> S (self-loop)
        ("OUT",  [(300, 300), (400, 300)]),    # S -> E
    ])
    idx = build_node_index(G)
    outlet = _node_at(G, idx, 400, 300)
    edges, _nodes, _depth = traverse_upstream(G, outlet)   # must return (no hang)
    pidx = [e["pidx"] for e in edges]
    # Intent: the self-loop is collected exactly once, and traversal ends.
    # RED against a walk with no visited-set (infinite loop -> CI timeout).
    assert pidx.count(0) == 1
    assert sorted(set(pidx)) == [0, 1]


def test_two_node_cycle_terminates_and_counts_each_once():
    # A<->B mutual cycle, plus B->O outlet.
    G = _graph([
        ("AB",  [(0, 0), (100, 0)]),      # A -> B
        ("BA",  [(100, 0), (0, 0)]),      # B -> A  (closes the 2-node cycle)
        ("OUT", [(100, 0), (200, 0)]),    # B -> O
    ])
    idx = build_node_index(G)
    outlet = _node_at(G, idx, 200, 0)
    edges, _nodes, _depth = traverse_upstream(G, outlet)
    pidx = [e["pidx"] for e in edges]
    # Intent: every edge in the cycle counted once; traversal terminates.
    # RED against no visited-set (infinite loop).
    assert sorted(pidx) == [0, 1, 2]
    assert all(pidx.count(p) == 1 for p in pidx)


# --- target resolution ----------------------------------------------------

def test_false_headwater_guard_prefers_the_upstream_junction():
    # Two endpoints 2 ft apart (do NOT merge at snap_tol=1): a start_only node
    # at (100,0) with nothing upstream, and the downstream end of an incoming
    # pipe at (98,0) with in-degree 1. A target at (100.4,0) is nearest the
    # start_only node.
    G = _graph([
        ("UP", [(100, 0), (200, 0)]),   # start (100,0) is start_only, in_deg 0
        ("IN", [(0, 0), (98, 0)]),       # end (98,0) has in_deg 1
    ])
    idx = build_node_index(G)
    start_only = _node_at(G, idx, 100, 0)
    junction = _node_at(G, idx, 98, 0)
    assert G.in_degree(start_only) == 0 and G.in_degree(junction) == 1  # setup sanity

    node, xy, dist, source, val = resolve_target_node(
        G, _cfg(5.0), index=idx, target=("coordinate", [100.4, 0.0]))
    # Intent: prefer the node that actually has upstream, so the trace isn't
    # silently empty. RED if the guard is removed (nearest = start_only, in_deg 0).
    assert node == junction
    edges, _n, _d = traverse_upstream(G, node)
    assert len(edges) >= 1


def test_offnetwork_target_raises():
    G = _graph([("P0", [(0, 0), (100, 0)])])
    idx = build_node_index(G)
    # Target 9999 ft away, tolerance 5 ft.
    with pytest.raises(TargetResolutionError):
        resolve_target_node(G, _cfg(5.0), index=idx,
                            target=("coordinate", [9999.0, 9999.0]))
