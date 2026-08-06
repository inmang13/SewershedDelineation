"""
Candidate-site screening (Phase A) tests — drafted from the intended behaviour,
not from the code:

  - Block population lands on residential parcels by area share. A parcel
    straddling two blocks draws from both; a block with no residential parcels
    places nobody rather than spreading its people over vacant land.
  - Each parcel's population is charged to exactly ONE node (its nearest main's
    downstream end). Parcels out of reach are reported unplaced, not absorbed.
  - The one-pass accumulation is EXACT on a tree.
  - On a diverge/reconverge diamond it over-counts. That is tolerable only
    because (a) it is always an upper bound, which is what makes screening on it
    free of false negatives, and (b) every inflated node is flagged. Both are
    asserted as contracts.
  - The exact recompute gets the diamond right.
  - A direction-error loop (SCC) must not deadlock the topological order, and
    every node in the loop reports the same value — inside a loop there is no
    defensible upstream/downstream split.
  - Nesting under an existing site is a graph relation: both upstream and
    downstream neighbours overlap it, a sibling basin does not.
  - But overlap is NOT only nesting. Three tests are needed and none subsumes the
    others: the graph sees nesting, the point test sees a candidate standing inside
    an inflated existing polygon, and only the catchment test sees a candidate
    standing well clear whose own upstream pipes run back through one.
  - Relation to an existing site is a LABEL, not a filter: encompassing an
    already-sampled branch disqualifies, sitting inside one does not.

Toy geometry and hand-built graphs only — no data/ or config.yaml, so this runs
on a clean clone.

Run:  python -m pytest tests/test_candidate_screen.py
"""

import sys
from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString, box

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from graph_builder import build_graph, build_node_index, nearest_node   # noqa: E402
from candidate_screen import (                                          # noqa: E402
    parcel_population, local_pop_by_node, accumulate_upstream,
    exact_upstream_pop, overlap_excluded_nodes, distance_to_existing,
    trace_pipes_in_existing, classify_existing_relation,
)

CRS = "EPSG:2264"
SNAP_TOL = 1.0


def _pipes(rows):
    """GeoDataFrame of gravity mains from (fid, [(x,y),...]) rows; pidx == order."""
    return gpd.GeoDataFrame(
        {"FACILITYID": [r[0] for r in rows],
         "SLOPE":      [1.0] * len(rows),
         "UPSTREAMIN": [10.0] * len(rows),
         "DOWNSTREAM": [9.0] * len(rows)},
        geometry=[LineString(r[1]) for r in rows], crs=CRS)


def _graph(rows):
    return build_graph(_pipes(rows), SNAP_TOL)


def _at(G, x, y):
    node, _ = nearest_node(build_node_index(G), x, y)
    return node


# --- dasymetric parcel population ----------------------------------------

def test_block_population_splits_across_residential_parcels_by_area():
    # B1 (pop 100) holds all of P0 and the left half of P1 — 50/50 by area.
    # B2 (pop 200) holds only the right half of P1, so P1 takes all 200.
    blocks = gpd.GeoDataFrame(
        {"P1_001N": [100, 200]},
        geometry=[box(0, 0, 100, 100), box(100, 0, 200, 100)], crs=CRS)
    res = gpd.GeoDataFrame(
        geometry=[box(0, 0, 50, 100), box(50, 0, 150, 100)], crs=CRS)

    pop = parcel_population(res, blocks)

    assert pop.iloc[0] == pytest.approx(50.0)
    assert pop.iloc[1] == pytest.approx(250.0)


def test_block_with_no_residential_land_places_nobody():
    # B2 has 500 people but no residential parcel. Spreading them over vacant
    # land would invent households; the shortfall must be visible instead.
    blocks = gpd.GeoDataFrame(
        {"P1_001N": [100, 500]},
        geometry=[box(0, 0, 100, 100), box(200, 0, 300, 100)], crs=CRS)
    res = gpd.GeoDataFrame(geometry=[box(0, 0, 100, 100)], crs=CRS)

    pop = parcel_population(res, blocks)

    assert pop.sum() == pytest.approx(100.0)
    assert pop.attrs["population_placed"] == pytest.approx(100.0)
    assert pop.attrs["population_total"] == pytest.approx(600.0)


# --- parcel -> node charging ----------------------------------------------

def test_parcel_is_charged_once_to_its_nearest_pipes_downstream_node():
    # One pipe (0,50)->(100,50). The parcel sits 10 ft below it, so it is served
    # and belongs to the pipe's downstream end at (100,50).
    rows = [("P0", [(0, 50), (100, 50)])]
    G = _graph(rows)
    pipes = _pipes(rows)
    res = gpd.GeoDataFrame(geometry=[box(0, 0, 50, 40)], crs=CRS)

    lp = local_pop_by_node(G, pipes, res, pd.Series([120.0]), 50.0)

    assert lp.by_node == {_at(G, 100, 50): 120.0}
    assert lp.pop_unplaced == pytest.approx(0.0)
    assert lp.n_parcels_charged == 1


def test_parcel_out_of_reach_is_reported_unplaced_not_absorbed():
    rows = [("P0", [(0, 50), (100, 50)])]
    G = _graph(rows)
    pipes = _pipes(rows)
    res = gpd.GeoDataFrame(
        geometry=[box(0, 0, 50, 40), box(5000, 5000, 5050, 5040)], crs=CRS)

    lp = local_pop_by_node(G, pipes, res, pd.Series([120.0, 80.0]), 50.0)

    assert lp.pop_charged == pytest.approx(120.0)
    assert lp.pop_unplaced == pytest.approx(80.0)
    assert lp.n_parcels_unplaced == 1


# --- accumulation --------------------------------------------------------

def _tree():
    """A->B, C->B, B->D. D is the outlet; nothing diverges."""
    return _graph([("a", [(0, 0), (100, 0)]),
                   ("b", [(100, 100), (100, 0)]),
                   ("c", [(100, 0), (200, 0)])])


def test_accumulation_is_exact_on_a_tree():
    G = _tree()
    A, B, C, D = _at(G, 0, 0), _at(G, 100, 0), _at(G, 100, 100), _at(G, 200, 0)
    local = {A: 10.0, B: 20.0, C: 30.0, D: 40.0}

    up = accumulate_upstream(G, local)

    assert up.est[D] == pytest.approx(100.0)     # everyone drains to the outlet
    assert up.est[B] == pytest.approx(60.0)      # A + C + B's own
    assert up.est[A] == pytest.approx(10.0)      # headwater: itself only
    assert up.n_divergers == 0
    assert not any(up.overcount_risk.values())


def _diamond():
    """A->B, A->C, B->D, C->D. A diverges, D reconverges."""
    return _graph([("a", [(0, 0), (100, 100)]),
                   ("b", [(0, 0), (100, -100)]),
                   ("c", [(100, 100), (200, 0)]),
                   ("d", [(100, -100), (200, 0)])])


def test_accumulation_is_an_upper_bound_so_screening_has_no_false_negatives():
    # The whole prefilter design rests on est >= exact. If that inverts, a real
    # >500 site could be screened out and never seen again.
    G = _diamond()
    up = accumulate_upstream(G, {n: 10.0 * (i + 1)
                                 for i, n in enumerate(sorted(G.nodes()))})
    exact = exact_upstream_pop(up, list(G.nodes()))

    for n in G.nodes():
        assert up.est[n] >= exact[n] - 1e-9


def test_every_overcounted_node_is_flagged():
    # The flag is allowed to be conservative (over-flag), but must never miss an
    # inflated node — a silent over-count is what would mislead the reviewer.
    G = _diamond()
    local = {n: 10.0 * (i + 1) for i, n in enumerate(sorted(G.nodes()))}
    up = accumulate_upstream(G, local)
    exact = exact_upstream_pop(up, list(G.nodes()))

    inflated = [n for n in G.nodes() if up.est[n] > exact[n] + 1e-9]
    assert inflated, "diamond should inflate at least the reconvergence node"
    assert all(up.overcount_risk[n] for n in inflated)
    assert up.n_divergers == 1


def test_exact_recompute_counts_the_diamond_once():
    G = _diamond()
    A = _at(G, 0, 0)
    D = _at(G, 200, 0)
    B, C = _at(G, 100, 100), _at(G, 100, -100)
    local = {A: 10.0, B: 20.0, C: 30.0, D: 40.0}

    up = accumulate_upstream(G, local)
    exact = exact_upstream_pop(up, [D])

    assert up.est[D] == pytest.approx(110.0)     # A counted twice
    assert exact[D] == pytest.approx(100.0)      # A counted once


def test_direction_error_loop_does_not_deadlock_and_reports_one_value():
    # B<->C is a 2-node SCC (a digitizing direction error). Inside a loop there
    # is no honest upstream/downstream split, so both nodes report the same.
    G = _graph([("a", [(0, 0), (100, 0)]),
                ("b", [(100, 0), (200, 0)]),
                ("c", [(200, 0), (100, 0)]),
                ("d", [(200, 0), (300, 0)])])
    A, B, C, D = (_at(G, 0, 0), _at(G, 100, 0), _at(G, 200, 0), _at(G, 300, 0))
    local = {A: 10.0, B: 20.0, C: 30.0, D: 40.0}

    up = accumulate_upstream(G, local)

    assert up.est[B] == up.est[C] == pytest.approx(60.0)
    assert up.est[D] == pytest.approx(100.0)


# --- overlap with existing sites ------------------------------------------

def test_both_upstream_and_downstream_of_an_existing_site_overlap_it():
    # X->E->Y is one branch with the existing site at E; S1->S2 is a separate
    # basin. Nesting either way means overlapping sewersheds.
    G = _graph([("a", [(0, 0), (100, 0)]),
                ("b", [(100, 0), (200, 0)]),
                ("s", [(0, 900), (100, 900)])])
    X, E, Y = _at(G, 0, 0), _at(G, 100, 0), _at(G, 200, 0)
    S1, S2 = _at(G, 0, 900), _at(G, 100, 900)

    excl = overlap_excluded_nodes(G, [E])

    assert {X, E, Y} <= excl
    assert S1 not in excl and S2 not in excl


def test_graph_independent_candidate_can_still_sit_inside_an_existing_polygon():
    # The gap the graph test cannot see, and the reason distance_to_existing
    # exists. S1->S2 shares no ancestor or descendant with the existing site, so
    # overlap_excluded_nodes keeps it — but a delineated boundary is inflated well
    # past the served parcels, and here it swallows S1 outright.
    G = _graph([("a", [(0, 0), (100, 0)]),
                ("s", [(500, 0), (600, 0)])])
    E = _at(G, 100, 0)
    S1 = _at(G, 500, 0)
    existing = gpd.GeoDataFrame(geometry=[box(-50, -200, 550, 200)], crs=CRS)

    assert S1 not in overlap_excluded_nodes(G, [E])      # graph says independent
    dist = distance_to_existing(G, [S1], existing)
    assert dist[S1] == pytest.approx(0.0)                # geometry says overlapping


def test_catchment_reaching_into_an_existing_site_is_caught_though_point_is_clear():
    # The gap BOTH other tests miss, and the lab's actual criterion. The candidate
    # at (900,0) is graph-independent of the existing site and its point sits well
    # outside the existing polygon — but its own upstream pipe runs back through
    # that polygon, so it shares sewage with an existing sampling area.
    rows = [("up",  [(100, 0), (500, 0)]),     # inside the existing polygon
            ("mid", [(500, 0), (900, 0)])]     # out to the candidate
    G = _graph(rows)
    pipes = _pipes(rows)
    target = _at(G, 900, 0)
    existing = gpd.GeoDataFrame(geometry=[box(0, -100, 600, 100)], crs=CRS)

    # point test says clear
    assert distance_to_existing(G, [target], existing)[target] > 0
    # catchment test says otherwise
    n_in, n_total = trace_pipes_in_existing(G, pipes, [target], existing)[target]
    assert n_total == 2 and n_in >= 1


def test_catchment_clear_of_existing_sites_reports_zero_intrusion():
    rows = [("up", [(2000, 0), (2400, 0)])]
    G = _graph(rows)
    pipes = _pipes(rows)
    target = _at(G, 2400, 0)
    existing = gpd.GeoDataFrame(geometry=[box(0, -100, 600, 100)], crs=CRS)

    assert trace_pipes_in_existing(G, pipes, [target], existing)[target] == (0, 1)


def test_distance_to_existing_measures_real_clearance_outside_the_polygon():
    G = _graph([("s", [(500, 0), (600, 0)])])
    S2 = _at(G, 600, 0)
    existing = gpd.GeoDataFrame(geometry=[box(0, -100, 400, 100)], crs=CRS)

    assert distance_to_existing(G, [S2], existing)[S2] == pytest.approx(200.0)



def _chain(y, n):
    """n+1 nodes in a row at height y: (0,y)->(100,y)->...->(100n, y)."""
    return [(f"c{y}_{i}", [(100 * i, y), (100 * (i + 1), y)]) for i in range(n)]
