"""
Force-main topology and direction-inference tests — from the stated intent:

"A force main should let a trace cross from a discharge manhole back to the
pump station that feeds it. Work out which end of a force main is the wet well
and which is the discharge, using the fact that a pump station is where the
gravity network ends and a discharge is a gravity node that still flows
downstream. Where that rule can't decide, say so instead of guessing. And don't
change any topology — just report."

Toy geometry only — no data/ or config.yaml. Runs on a clean clone.

Run:  python -m pytest tests/test_force_mains.py
"""

import sys
from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from force_mains import (                                        # noqa: E402
    build_topology, classify_termini, component_verdicts, review_rows,
    load_force_mains, to_geopackage, write_qc_gpkg,
    flag_station_adjacent_discharges,
    FLAG_JUNCTION, FLAG_AMBIGUOUS, FLAG_NO_CONTACT, FLAG_STATION_ADJACENT,
    MAX_PREFILLED_RADIUS_GAP_FT,
)
from terminal_facilities import STATION                           # noqa: E402
from graph_builder import build_node_index                       # noqa: E402
from qa_review import load_review_decisions, manual_snaps        # noqa: E402

CRS = "EPSG:2264"


def _fm(lines, fids=None, dia=8.0):
    """Force-main frame with the attribute columns the module reads."""
    n = len(lines)
    return gpd.GeoDataFrame(
        {"FACILITYID": fids or [f"{i:05d}" for i in range(n)],
         "DIAMETER": [dia] * n,
         "OWNER": ["DNC"] * n,
         "LIFECYCLES": ["Active"] * n},
        geometry=[LineString(c) for c in lines], crs=CRS)


def _gravity(nodes, edges):
    """
    Minimal stand-in for the Phase 3 MultiDiGraph.

    `nodes` maps node id -> (x, y); `edges` are directed (u, v) pairs. Roles are
    derived the same way build_graph derives them, so out_degree means here
    exactly what it means in the real graph.
    """
    G = nx.MultiDiGraph()
    for nid, (x, y) in nodes.items():
        G.add_node(nid, x=float(x), y=float(y), role="junction")
    for u, v in edges:
        G.add_edge(u, v)
    for nid in G.nodes:
        has_in, has_out = G.in_degree(nid) > 0, G.out_degree(nid) > 0
        G.nodes[nid]["role"] = ("junction" if has_in and has_out
                                else "start_only" if has_out else "end_only")
    return G


# ---------------------------------------------------------------------------
# Load and inclusion filter — the data transform
# ---------------------------------------------------------------------------

def _write_layer(tmp_path, rows) -> Path:
    """
    A force-main GeoPackage on disk, one row per
    (fid, lifecycle, owner, diameter[, comment]).
    """
    gdf = gpd.GeoDataFrame(
        {"FACILITYID": [r[0] for r in rows],
         "LIFECYCLES": [r[1] for r in rows],
         "OWNER":      [r[2] for r in rows],
         "DIAMETER":   [r[3] for r in rows],
         "COMMENT":    [r[4] if len(r) > 4 else None for r in rows]},
        geometry=[LineString([(0, 100 * i), (100, 100 * i)])
                  for i in range(len(rows))], crs=CRS)
    path = tmp_path / "force_mains.gpkg"
    gdf.to_file(path, driver="GPKG")
    return path


def _cfg(path, include=None):
    return {"inputs": {"force_main_shapefile": str(path)},
            "parameters": {"crs": CRS, "force_main_include": include}}


def test_inclusion_filter_keeps_only_active_public_mains(tmp_path):
    """Active + public + >=4 in is the documented rule; everything else goes."""
    path = _write_layer(tmp_path, [
        ("00001", "Active",   "DNC", 12.0),   # keep
        ("00002", "Inactive", "DNC", 12.0),   # lifecycle
        ("00003", "Active",   "PVT", 12.0),   # private
        ("00004", "Active",   "DNC",  2.0),   # too small
        ("00005", "Active",   "DNC", 42.0),   # keep
    ])
    kept, report = load_force_mains(_cfg(path))
    assert sorted(kept.FACILITYID) == ["00001", "00005"]
    assert report.attrs["n_total"] == 5
    assert report.attrs["n_kept"] == 2
    assert report.n.sum() == 3


def test_zero_diameter_is_reported_as_missing_data_not_as_too_small(tmp_path):
    """
    A DIAMETER of 0 is a data-quality problem. Folding it into the size cut
    would hide it behind a modelling decision.
    """
    path = _write_layer(tmp_path, [("00001", "Active", "DNC", 0.0),
                                   ("00002", "Active", "DNC", 2.0)])
    _, report = load_force_mains(_cfg(path))
    reasons = dict(zip(report.reason, report.n))
    assert reasons["DIAMETER = 0 (missing data, not a small pipe)"] == 1
    assert any("private grinder lateral" in r for r in reasons)
    assert sum(reasons.values()) == 2


def test_bypass_port_lines_are_excluded_by_comment(tmp_path):
    """
    A bypass port line is a portable-pump connection used during maintenance. It
    carries no routine flow, so keeping it would fabricate connectivity that
    exists only when a crew is on site. Match is case-insensitive and matches a
    substring, so a dated/prefixed comment is caught too.
    """
    path = _write_layer(tmp_path, [
        ("00001", "Active", "DNC", 12.0, "bypass port line"),
        ("00002", "Active", "DNC", 12.0, "20181121-cwest: BYPASS PORT LINE"),
        ("00003", "Active", "DNC", 12.0, "Eno Force Main"),
        ("00004", "Active", "DNC", 12.0, None),
    ])
    kept, report = load_force_mains(_cfg(path, include={
        "lifecycle": ["Active"], "exclude_owner": ["PVT"],
        "min_diameter_in": 4, "exclude_comment": ["bypass port line"]}))
    assert sorted(kept.FACILITYID) == ["00003", "00004"]
    assert dict(zip(report.reason, report.n)) == {
        "COMMENT contains 'bypass port line'": 2}


def test_meter_bypasses_survive_the_bypass_exclusions(tmp_path):
    """
    Load-bearing distinction (Grace, 2026-08-01): a portable-pump connection
    carries no routine flow and comes out; a METER bypass carries real flow when
    the meter is offline, so it is live conveyance and stays in. The shipped
    pattern list must not catch the second kind.
    """
    from force_mains import DEFAULT_INCLUDE
    path = _write_layer(tmp_path, [
        ("00001", "Active", "DNC", 12.0, "20181119-cw: bypass pumping line"),
        ("00002", "Active", "DNC", 12.0, "By-Pass Port line"),
        ("00003", "Active", "DNC", 12.0, "By-Pass Pumping Port"),
        ("00004", "Active", "DNC", 12.0, "Emergency Bypass Connection"),
        ("00005", "Active", "DNC", 12.0, "20181119-cw:  for bypass pump pipe"),
        ("00006", "Active", "DNC", 12.0, "flow meter bypass"),          # keep
        ("00007", "Active", "DNC", 36.0, '36" Sewer Meter Bypass Line'),  # keep
        ("00008", "Active", "DNC", 12.0, "Eno Force Main"),             # keep
    ])
    kept, _ = load_force_mains(_cfg(path, include=DEFAULT_INCLUDE))
    assert sorted(kept.FACILITYID) == ["00006", "00007", "00008"]


def test_each_comment_pattern_is_reported_separately(tmp_path):
    """A pattern matching nothing must be visible, not silently inert."""
    path = _write_layer(tmp_path, [
        ("00001", "Active", "DNC", 12.0, "bypass port line"),
        ("00002", "Active", "DNC", 12.0, "Eno Force Main"),
    ])
    _, report = load_force_mains(_cfg(path, include={
        "lifecycle": ["Active"], "exclude_owner": ["PVT"], "min_diameter_in": 4,
        "exclude_comment": ["bypass port line", "never matches anything"]}))
    reasons = dict(zip(report.reason, report.n))
    assert reasons == {"COMMENT contains 'bypass port line'": 1}


def test_no_comment_rule_keeps_every_comment(tmp_path):
    """The rule is opt-in; an empty list must not filter anything."""
    path = _write_layer(tmp_path, [
        ("00001", "Active", "DNC", 12.0, "bypass port line")])
    kept, _ = load_force_mains(_cfg(path, include={
        "lifecycle": ["Active"], "exclude_owner": ["PVT"],
        "min_diameter_in": 4, "exclude_comment": []}))
    assert len(kept) == 1


def test_drop_report_names_the_features_it_dropped(tmp_path):
    """A run has to be able to state what it threw away, not just how much."""
    path = _write_layer(tmp_path, [("09999", "Inactive", "DNC", 12.0),
                                   ("00001", "Active", "DNC", 12.0)])
    _, report = load_force_mains(_cfg(path))
    assert "09999" in report.facilityids.iloc[0]


def test_include_rules_are_overridable_from_config(tmp_path):
    """The filter is a config decision, not a hardcoded one."""
    path = _write_layer(tmp_path, [("00001", "Active", "PVT", 2.0)])
    kept, _ = load_force_mains(_cfg(path, include={
        "lifecycle": ["Active"], "exclude_owner": [], "min_diameter_in": 1}))
    assert len(kept) == 1


def test_missing_layer_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_force_mains(_cfg(tmp_path / "nope.gpkg"))


def test_conversion_strips_z_and_reprojects(tmp_path):
    """Z is 0.0 on 6297 of 6303 vertices in the real layer — carrying it breaks KD-trees."""
    src = tmp_path / "src.gpkg"
    gpd.GeoDataFrame(
        {"FACILITYID": ["00001"]},
        geometry=[LineString([(0, 0, 0), (100, 0, 0)])], crs=CRS
    ).to_file(src, driver="GPKG")
    out = tmp_path / "out.gpkg"
    assert to_geopackage(str(src), str(out), CRS) == 1
    assert not gpd.read_file(out).geometry.has_z.any()


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------

def test_touching_force_mains_form_one_component():
    """Two mains sharing an endpoint are one pumped system, not two."""
    fm = _fm([[(0, 0), (100, 0)], [(100, 0), (200, 0)]])
    topo = build_topology(fm, tol_ft=1.0)
    assert len(topo.components) == 1
    assert len(topo.termini(topo.components[0])) == 2


def test_gap_wider_than_tolerance_splits_the_component():
    """A 5 ft gap at a 1 ft tolerance is two systems — the snap must not reach."""
    fm = _fm([[(0, 0), (100, 0)], [(105, 0), (200, 0)]])
    topo = build_topology(fm, tol_ft=1.0)
    assert len(topo.components) == 2


def test_parallel_mains_are_both_kept():
    """
    Dual force mains out of one lift station are standard practice. A simple
    graph would drop the second one's pidx, undercounting pipes and length and
    hiding the cycle that makes the component's direction ambiguous.
    """
    fm = _fm([[(0, 0), (100, 0)], [(0, 0), (50, 40), (100, 0)]])
    topo = build_topology(fm, tol_ft=1.0)
    comp = topo.components[0]
    assert topo.graph.number_of_edges() == 2
    pidx = {d["pidx"] for _, _, d in topo.graph.subgraph(comp).edges(data=True)}
    assert pidx == {0, 1}
    assert topo.n_cycles(comp) == 1


def test_manual_join_repairs_a_break_inside_the_pressurized_network():
    """
    A break inside the force-main network splits one pumped system into two
    components, and each half then reports a spurious no_discharge. A confirmed
    join must merge them back before any gravity junction is considered.
    """
    fm = _fm([[(0, 0), (100, 0)], [(106, 0), (200, 0)]])   # 6 ft gap
    assert len(build_topology(fm, tol_ft=1.0).components) == 2
    joined = build_topology(fm, tol_ft=1.0,
                            manual_joins=[{"x": 103.0, "y": 0.0, "radius_ft": 5.0}])
    assert len(joined.components) == 1


def test_a_join_that_would_merge_nothing_raises():
    """
    A repair that quietly no-ops is worse than a loud one — the reviewer would
    believe a break was fixed when it wasn't. Same contract as snap_endpoints.
    """
    fm = _fm([[(0, 0), (100, 0)], [(106, 0), (200, 0)]])
    with pytest.raises(ValueError, match="fewer than two distinct"):
        build_topology(fm, tol_ft=1.0,
                       manual_joins=[{"x": 50_000.0, "y": 0.0, "radius_ft": 5.0}])


def test_join_file_survives_an_excel_round_trip(tmp_path):
    """
    Excel writes cp1252 on a Windows machine, and a curly apostrophe in a
    comment kills a utf-8 read. A review loop that cannot survive Excel is not a
    review loop.
    """
    from force_mains import load_manual_joins
    path = tmp_path / "joins.csv"
    # U+2019 encodes to byte 0x92 in cp1252 — the exact byte that killed the
    # utf-8 read of Grace's returned decisions file on 2026-08-01.
    path.write_bytes(
        "x,y,radius_ft,comment\n100,0,5.0,Grace’s confirmed join\n".encode("cp1252"))
    joins = load_manual_joins({"inputs": {"force_main_joins": str(path)}})
    assert joins == [{"x": 100.0, "y": 0.0, "radius_ft": 5.0}]


def test_no_join_file_is_not_an_error():
    from force_mains import load_manual_joins
    assert load_manual_joins({"inputs": {}}) == []
    assert load_manual_joins({"inputs": {"force_main_joins": "nope.csv"}}) == []


def _stub_case(fm_lines, gravity_at=(10_000.0, 0.0)):
    """Topology + node index for the stub tests; gravity sits where you put it."""
    from force_mains import prune_small_stubs
    fm = _fm(fm_lines, dia=3.0)
    G = _gravity({1: gravity_at, 2: (gravity_at[0] + 100, gravity_at[1])}, [(1, 2)])
    topo = build_topology(fm, 1.0)
    return prune_small_stubs(fm, topo, build_node_index(G),
                             small_diameter_in=4.0, max_component_ft=25.0,
                             contact_tol_ft=10.0)


def test_a_short_small_main_going_nowhere_is_dropped():
    """4 ft of 3 in pipe, nowhere near the network — litter, not topology."""
    kept, report = _stub_case([[(0, 0), (4, 0)]])
    assert kept.empty and len(report) == 1


def test_a_short_small_main_that_touches_gravity_is_kept():
    """
    Short AND disconnected is a stub. Short but attached is a real connector —
    both conditions have to hold or a legitimate short link gets thrown away.
    """
    kept, report = _stub_case([[(0, 0), (4, 0)]], gravity_at=(6.0, 0.0))
    assert len(kept) == 1 and report.empty


def test_a_long_small_main_going_nowhere_is_kept():
    """
    A long main touching nothing is a snap gap to investigate, not litter.
    Dropping it would hide the very problem the review file exists to surface.
    """
    kept, report = _stub_case([[(0, 0), (900, 0)]])
    assert len(kept) == 1 and report.empty


def test_stub_test_is_per_component_not_per_pipe():
    """
    Pipe 2467 on the real network is 24.5 ft and would fail any per-pipe length
    rule, but it is one segment of a healthy 909 ft system. The test has to look
    at the whole connected run.
    """
    kept, report = _stub_case([[(0, 0), (20, 0)], [(20, 0), (900, 0)]])
    assert len(kept) == 2 and report.empty


def test_normal_diameter_mains_are_never_pruned():
    """The rule targets small mains only; behaviour for normal ones is unchanged."""
    from force_mains import prune_small_stubs
    fm = _fm([[(0, 0), (4, 0)]], dia=12.0)
    G = _gravity({1: (10_000.0, 0.0), 2: (10_100.0, 0.0)}, [(1, 2)])
    kept, report = prune_small_stubs(fm, build_topology(fm, 1.0),
                                     build_node_index(G), 4.0, 25.0, 10.0)
    assert len(kept) == 1 and report.empty


def test_loop_is_reported_as_a_cycle():
    """A ring has no unambiguous interior direction — n_cycles must catch it."""
    fm = _fm([[(0, 0), (100, 0)], [(100, 0), (100, 100)],
              [(100, 100), (0, 100)], [(0, 100), (0, 0)]])
    topo = build_topology(fm, tol_ft=1.0)
    assert topo.n_cycles(topo.components[0]) == 1


# ---------------------------------------------------------------------------
# Direction inference — the headline behaviour
# ---------------------------------------------------------------------------

def _wetwell_to_discharge_case(fm_geom):
    """
    One force main from a gravity dead end (wet well) to a gravity headwater
    that flows onward (discharge). Node 1 at (0,0) is the sink; node 3 at
    (500,0) starts a run to node 4.
    """
    G = _gravity({1: (0, 0), 2: (-100, 0), 3: (500, 0), 4: (600, 0)},
                 [(2, 1), (3, 4)])
    fm = _fm([fm_geom])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G),
                              snap_tol_ft=10.0, review_radius_ft=500.0)
    return topo, termini, component_verdicts(topo, termini, fm), fm


def test_wetwell_and_discharge_are_identified():
    topo, termini, verdicts, _ = _wetwell_to_discharge_case([(0, 0), (500, 0)])
    assert set(termini.classification) == {"wetwell", "discharge"}
    assert verdicts.verdict.iloc[0] == "resolved"
    wet = termini[termini.classification == "wetwell"].iloc[0]
    assert (wet.x, wet.y) == (0.0, 0.0)          # the gravity dead end
    assert wet.out_degree == 0


def test_result_does_not_depend_on_digitized_direction():
    """
    Geometry direction is not a trustworthy convention for pressurized mains,
    so drawing the same main backwards must give the same answer.
    """
    _, fwd, _, _ = _wetwell_to_discharge_case([(0, 0), (500, 0)])
    _, rev, _, _ = _wetwell_to_discharge_case([(500, 0), (0, 0)])
    assert (dict(zip(zip(fwd.x, fwd.y), fwd.classification))
            == dict(zip(zip(rev.x, rev.y), rev.classification)))


def test_two_downstream_ends_is_ambiguous_not_a_guess():
    """Both ends flow onward — the rule cannot pick, so it must not."""
    G = _gravity({1: (0, 0), 2: (100, 0), 3: (500, 0), 4: (600, 0)},
                 [(1, 2), (3, 4)])
    fm = _fm([[(0, 0), (500, 0)]])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    verdicts = component_verdicts(topo, termini, fm)
    assert verdicts.verdict.iloc[0] == "multi_discharge"


def test_far_from_gravity_is_reported_not_snapped():
    """A main nowhere near gravity is a data gap to report, not a junction."""
    G = _gravity({1: (0, 0), 2: (100, 0)}, [(1, 2)])
    fm = _fm([[(50_000, 50_000), (50_500, 50_000)]])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    assert set(termini.contact) == {"none"}
    assert component_verdicts(topo, termini, fm).verdict.iloc[0] == "isolated"


def test_gap_between_tolerances_is_a_candidate_not_a_contact():
    """40 ft with a 10 ft tolerance: offered for review, never auto-accepted."""
    G = _gravity({1: (0, 0), 2: (-100, 0), 3: (540, 0), 4: (640, 0)},
                 [(2, 1), (3, 4)])
    fm = _fm([[(0, 0), (500, 0)]])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    far = termini[termini.dist_ft > 10].iloc[0]
    assert far.contact == "candidate"
    assert far.classification.endswith("?")


# ---------------------------------------------------------------------------
# Review file — the actual deliverable of this step
# ---------------------------------------------------------------------------

def test_review_file_is_readable_by_the_existing_review_loader(tmp_path):
    """
    The whole point of matching qa_review's schema: a reviewer sets
    decision=snap and the existing pass-3 machinery consumes it unchanged.
    """
    topo, termini, verdicts, fm = _wetwell_to_discharge_case([(0, 0), (500, 0)])
    rows = review_rows(termini, verdicts, fm, topo)
    path = tmp_path / "force_main_review.csv"
    rows.to_csv(path, index=False)

    assert load_review_decisions(path) == []          # nothing pre-accepted

    rows.loc[0, "decision"] = "snap"
    rows.to_csv(path, index=False)
    snaps = manual_snaps(load_review_decisions(path))
    assert len(snaps) == 1
    assert set(snaps[0]) == {"x", "y", "radius_ft"}


def test_facilityid_is_namespaced_against_the_gravity_layer():
    """218 force-main ids also exist in the gravity mains — never emit a bare id."""
    topo, termini, verdicts, fm = _wetwell_to_discharge_case([(0, 0), (500, 0)])
    rows = review_rows(termini, verdicts, fm, topo)
    assert all(p.startswith("FM:") for p in rows.pipe_id)


def test_wide_gaps_ship_without_a_prefilled_radius():
    """
    Pass 3 merges everything inside radius_ft, so a pre-filled radius on a wide
    candidate would swallow unrelated nodes if accepted unread.
    """
    G = _gravity({1: (0, 0), 2: (-100, 0), 3: (900, 0), 4: (1000, 0)},
                 [(2, 1), (3, 4)])
    fm = _fm([[(0, 0), (500, 0)]])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    rows = review_rows(termini, verdicts=component_verdicts(topo, termini, fm),
                       fm=fm, topo=topo)
    wide = rows[rows.dist_ft > MAX_PREFILLED_RADIUS_GAP_FT]
    assert not wide.empty
    assert all(r == "" for r in wide.radius_ft)


def test_prefilled_radius_reaches_both_sides_of_a_narrow_gap():
    """A snap radius that doesn't reach both clusters merges nothing."""
    topo, termini, verdicts, fm = _wetwell_to_discharge_case([(4, 0), (500, 0)])
    rows = review_rows(termini, verdicts, fm, topo)
    narrow = rows[rows.dist_ft <= MAX_PREFILLED_RADIUS_GAP_FT]
    assert not narrow.empty
    for r in narrow.itertuples(index=False):
        assert float(r.radius_ft) >= r.dist_ft / 2.0


def test_a_closed_ring_still_reaches_the_review_file():
    """
    A pure loop has no free end, so it produces no terminus rows. Without an
    explicit row it would vanish from the review file while still appearing in
    the verdicts and in fm_pipes — unreviewable network.
    """
    G = _gravity({1: (0, 0), 2: (100, 0)}, [(1, 2)])
    fm = _fm([[(50_000, 50_000), (50_100, 50_000)],
              [(50_100, 50_000), (50_100, 50_100)],
              [(50_100, 50_100), (50_000, 50_100)],
              [(50_000, 50_100), (50_000, 50_000)]])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    verdicts = component_verdicts(topo, termini, fm)
    assert termini.empty and verdicts.n_termini.iloc[0] == 0
    rows = review_rows(termini, verdicts, fm, topo)
    assert len(rows) == 1
    assert rows.classification.iloc[0] == "ring"
    assert rows.pipe_id.iloc[0].startswith("FM:")


def test_empty_layer_is_an_empty_topology_not_a_crash():
    """Matches node_layer.snap_endpoints' own early return on no endpoints."""
    topo = build_topology(_fm([]), 1.0)
    assert topo.components == []


def test_review_row_records_which_end_of_the_main():
    """A reviewer opening the pipe in GIS needs to know which end is meant."""
    topo, termini, verdicts, fm = _wetwell_to_discharge_case([(0, 0), (500, 0)])
    rows = review_rows(termini, verdicts, fm, topo)
    assert set(rows.fm_end) == {"start", "end"}


def test_qc_gpkg_assigns_every_pipe_to_a_component(tmp_path):
    """A pipe with no comp_id means the topology dropped it — the MultiGraph bug."""
    topo, termini, verdicts, fm = _wetwell_to_discharge_case([(0, 0), (500, 0)])
    layers = write_qc_gpkg(tmp_path / "qc.gpkg", CRS, fm, topo, termini, verdicts)
    assert "fm_pipes" in layers
    pipes = gpd.read_file(tmp_path / "qc.gpkg", layer="fm_pipes")
    assert (pipes.comp_id >= 0).all()


def test_every_flag_type_names_a_real_condition():
    """No row should carry a flag type that isn't one of the three defined."""
    topo, termini, verdicts, fm = _wetwell_to_discharge_case([(0, 0), (500, 0)])
    rows = review_rows(termini, verdicts, fm, topo)
    assert set(rows.flag_type) <= {FLAG_JUNCTION, FLAG_AMBIGUOUS, FLAG_NO_CONTACT}


# ---------------------------------------------------------------------------
# Station-adjacent discharge misread (East End / Geer St, 2026-08-02/03)
# ---------------------------------------------------------------------------
#
# Some stations are a wet well manhole joined by a few feet of gravity pipe to
# a second manhole that continues downstream. The out-degree rule sees flow
# continuing past that second manhole and calls it a discharge, when the whole
# thing is still inside the station and there is no real second connection.
# flag_station_adjacent_discharges catches this: short incident pipe + a
# confirmed station nearby -> downgrade out of CONFIRMED_CONTACTS so the
# component doesn't silently resolve on a fake discharge.

def _facility(name, role, x, y):
    return gpd.GeoDataFrame({"name": [name], "role": [role]},
                            geometry=[Point(x, y)], crs=CRS)


def test_short_stub_next_to_a_station_is_caught():
    """The East End shape: a real wetwell plus a short stub reading discharge."""
    # Wetwell end (0,0) on a gravity dead end; the "discharge" end (18,0) is a
    # short 18 ft pipe sitting on a junction that only continues because of the
    # station's own connector — the exact misread signature.
    G = _gravity({1: (0, 0), 2: (-50, 0), 3: (18, 0), 4: (118, 0)},
                 [(2, 1), (3, 4)])
    fm = _fm([[(0, 0), (18, 0)]])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    station = _facility("East End Lift Station", STATION, 18, 0)

    out, report = flag_station_adjacent_discharges(termini, topo, fm, station,
                                                    max_pipe_ft=25.0,
                                                    station_tol_ft=200.0)
    flagged = out[out.classification == "discharge"].iloc[0]
    assert flagged.station_adjacent
    assert flagged.contact == "candidate"          # out of CONFIRMED_CONTACTS
    assert len(report) == 1


def test_long_discharge_pipe_is_not_flagged_even_near_a_station():
    """The rule targets short stubs specifically, not every station-adjacent pipe."""
    G = _gravity({1: (0, 0), 2: (-50, 0), 3: (900, 0), 4: (1000, 0)},
                 [(2, 1), (3, 4)])
    fm = _fm([[(0, 0), (900, 0)]])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    station = _facility("Nearby Station", STATION, 900, 0)

    out, report = flag_station_adjacent_discharges(termini, topo, fm, station,
                                                    max_pipe_ft=25.0,
                                                    station_tol_ft=200.0)
    discharge_row = out[out.classification == "discharge"].iloc[0]
    assert not discharge_row.station_adjacent
    assert discharge_row.contact == "connected"
    assert report.empty


def test_short_discharge_pipe_without_a_nearby_station_is_not_flagged():
    """Short alone isn't the signal — it takes a confirmed station too."""
    G = _gravity({1: (0, 0), 2: (-50, 0), 3: (18, 0), 4: (118, 0)},
                 [(2, 1), (3, 4)])
    fm = _fm([[(0, 0), (18, 0)]])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    no_facilities = gpd.GeoDataFrame({"name": [], "role": []}, geometry=[], crs=CRS)

    out, report = flag_station_adjacent_discharges(
        termini, topo, fm, no_facilities, max_pipe_ft=25.0, station_tol_ft=200.0)
    discharge_row = out[out.classification == "discharge"].iloc[0]
    assert not discharge_row.station_adjacent
    assert discharge_row.contact == "connected"
    assert report.empty


def test_flagged_component_no_longer_silently_resolves():
    """
    The whole point: East End's component (only a wetwell + the misread) must
    stop reading `resolved` once the misread is caught, or it never surfaces
    for review at all — which is exactly what happened before this rule existed.
    """
    # A single 18 ft pipe: (0,0) reads wetwell (real dead end), (18,0) reads
    # discharge (misread — the junction only continues because of the
    # station's own outlet). Both readings connected -> `resolved`, before fix.
    G = _gravity({1: (0, 0), 2: (-50, 0), 3: (18, 0), 4: (118, 0)},
                 [(2, 1), (3, 4)])
    fm = _fm([[(0, 0), (18, 0)]])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    station = _facility("East End Lift Station", STATION, 18, 0)

    before = component_verdicts(topo, termini, fm)
    assert before.verdict.iloc[0] == "resolved"        # the bug, before the fix

    fixed_termini, _ = flag_station_adjacent_discharges(
        termini, topo, fm, station, max_pipe_ft=25.0, station_tol_ft=200.0)
    after = component_verdicts(topo, fixed_termini, fm)
    assert after.verdict.iloc[0] != "resolved"


def test_review_comment_names_the_pattern_not_a_generic_ambiguity():
    """A reviewer should be told WHY this one is different, not just 'ambiguous'."""
    G = _gravity({1: (0, 0), 2: (-50, 0), 3: (18, 0), 4: (118, 0)},
                 [(2, 1), (3, 4)])
    fm = _fm([[(0, 0), (18, 0)]])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    station = _facility("East End Lift Station", STATION, 18, 0)
    flagged, _ = flag_station_adjacent_discharges(
        termini, topo, fm, station, max_pipe_ft=25.0, station_tol_ft=200.0)
    verdicts = component_verdicts(topo, flagged, fm)

    rows = review_rows(flagged, verdicts, fm, topo)
    hit = rows[rows.flag_type == FLAG_STATION_ADJACENT]
    assert len(hit) == 1
    assert "station" in hit.comment.iloc[0].lower()


def test_facility_confirmed_terminus_is_not_asked_which_end_is_the_pump_station():
    """
    Regression (Grace, 2026-08-03): the Snow Hill LS row already had a
    facility match confirming it as the wet well, but because the SYSTEM was
    still unresolved (its other end was a data gap), the row still asked
    "which end of this system is the pump station?" — the wrong question for
    a point that was already answered. The comment must say this end is
    settled and point at the real open problem instead.
    """
    from terminal_facilities import match_facilities, apply_to_termini
    G = _gravity({1: (0, 0), 2: (-50, 0)}, [(2, 1)])   # dead end -> wetwell
    fm = _fm([[(0, 0), (500, 0)]])                     # other end far from gravity
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    station = _facility("Snow Hill Lift Station", STATION, 0, 0)
    matches = match_facilities(station, termini, 1000.0, 200.0)
    termini = apply_to_termini(termini, matches)
    verdicts = component_verdicts(topo, termini, fm)
    assert verdicts.verdict.iloc[0] in {"no_discharge"}   # confirms the repro shape

    rows = review_rows(termini, verdicts, fm, topo)
    hit = rows[rows.pipe_id.str.contains("FM:")]
    confirmed = hit[hit.classification == "wetwell"].iloc[0]
    assert "already confirmed" in confirmed.comment.lower()
    assert "which end" not in confirmed.comment.lower()
    assert "Snow Hill Lift Station" in confirmed.comment


def test_no_facilities_layer_leaves_everything_unflagged():
    """flag_station_adjacent_discharges must be a no-op when there's no facility data."""
    topo, termini, verdicts, fm = _wetwell_to_discharge_case([(0, 0), (500, 0)])
    out, report = flag_station_adjacent_discharges(
        termini, topo, fm, None, max_pipe_ft=25.0, station_tol_ft=200.0)
    assert report.empty
    assert not out.station_adjacent.any()
    pd.testing.assert_series_equal(out.contact, termini.contact)


# ---------------------------------------------------------------------------
# Reviewer direction rulings — "this end is the pump station, whatever the rule says"
# ---------------------------------------------------------------------------

def _one_ambiguous_station():
    """
    The Geer St shape: the wet-well manhole also passes a gravity main through
    it, so gravity still flows out of the node the force main lands on and the
    out-degree rule calls that end a discharge. Both ends then read as
    discharges and the system can't be settled.
    """
    G = _gravity({1: (0, 0), 2: (200, 0), 3: (200, 300),
                  4: (600, 0), 5: (800, 0)},
                 [(1, 2), (2, 3), (4, 5)])
    fm = _fm([[(200, 0), (600, 0)]], fids=["FM01"])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    return G, fm, topo, termini


def test_a_reviewer_can_name_the_pump_station_the_rule_got_wrong():
    from force_mains import apply_direction_overrides
    G, fm, topo, termini = _one_ambiguous_station()
    assert component_verdicts(topo, termini, fm).verdict.iloc[0] == "multi_discharge"

    termini2, log = apply_direction_overrides(
        termini, [{"x": 200.0, "y": 0.0, "classification": "wetwell",
                   "comment": "this is the station"}], 25.0)

    assert log.was.iloc[0] == "discharge" and log.now.iloc[0] == "wetwell"
    assert component_verdicts(topo, termini2, fm).verdict.iloc[0] == "resolved", \
        "naming the wet well settles the system"


def test_a_ruling_that_matches_nothing_raises_instead_of_being_ignored():
    """Geometry moved out from under the ruling — silently dropping it is worse."""
    from force_mains import apply_direction_overrides
    G, fm, topo, termini = _one_ambiguous_station()
    with pytest.raises(ValueError, match="matched no force-main terminus"):
        apply_direction_overrides(
            termini, [{"x": 99999.0, "y": 0.0, "classification": "wetwell",
                       "comment": ""}], 25.0)


def test_a_ruling_changes_only_the_nearest_terminus():
    """Two ends of one station can both sit inside tolerance — don't flip both."""
    from force_mains import apply_direction_overrides
    G, fm, topo, termini = _one_ambiguous_station()
    termini2, log = apply_direction_overrides(
        termini, [{"x": 200.0, "y": 0.0, "classification": "wetwell",
                   "comment": ""}], 10_000.0)   # absurdly wide on purpose
    assert len(log) == 1
    assert (termini2.classification == "wetwell").sum() == 1, \
        "a wide radius must not invent a second wet well"


def test_no_rulings_leaves_the_classification_untouched():
    from force_mains import apply_direction_overrides
    G, fm, topo, termini = _one_ambiguous_station()
    termini2, log = apply_direction_overrides(termini, [], 25.0)
    assert log.empty
    assert termini2.classification.tolist() == termini.classification.tolist()


# ---------------------------------------------------------------------------
# Station outlets where two force mains leave one point
# ---------------------------------------------------------------------------

def _dual_main_station():
    """
    The Lick Creek shape: two mains leave the station and rejoin downstream, so
    the outlet has two neighbours and is NOT a free end. A free end exists far
    away on the other side of the system.
    """
    G = _gravity({1: (0, 0), 2: (100, 0), 3: (900, 0), 4: (1000, 0)},
                 [(1, 2), (3, 4)])
    fm = _fm([[(100, 0), (400, 60)],     # main A out of the station
              [(100, 0), (400, -60)],    # main B out of the station
              [(400, 60), (500, 0)],
              [(400, -60), (500, 0)],
              [(500, 0), (900, 0)]],     # single pipe onward to the discharge
             fids=["A", "B", "A2", "B2", "TRUNK"])
    topo = build_topology(fm, 1.0)
    termini = classify_termini(topo, G, build_node_index(G), 10.0, 500.0)
    return G, fm, topo, termini


def test_a_station_outlet_with_two_mains_leaving_is_not_a_free_end():
    """The premise: this is why the matcher missed the real outlet."""
    G, fm, topo, termini = _dual_main_station()
    assert not ((termini.x == 100.0) & (termini.y == 0.0)).any(), \
        "two mains leaving one point means it is not a terminus"


def test_a_confirmed_station_pins_the_wet_well_at_the_outlet_junction():
    from force_mains import add_station_junction_termini
    G, fm, topo, termini = _dual_main_station()
    fac = _facility("Dual Main LS", STATION, 106.0, 0.0)

    out, added = add_station_junction_termini(
        termini, topo, G, build_node_index(G), fac, 200.0, 10.0, 500.0)

    assert len(added) == 1
    assert added.n_mains.iloc[0] == 2
    assert added.classification.iloc[0] == "wetwell", \
        "gravity ends at the station, so the outlet is the wet well"
    assert ((out.x == 100.0) & (out.y == 0.0)).any(), \
        "the outlet is now a terminus the facility matcher can see"


def test_nothing_changes_when_a_free_end_is_already_closer():
    """Where the matcher already has a good answer, leave it alone."""
    from force_mains import add_station_junction_termini
    G, fm, topo, termini = _dual_main_station()
    # station parked next to the far free end instead of the outlet
    fac = _facility("Far LS", STATION, 895.0, 0.0)
    out, added = add_station_junction_termini(
        termini, topo, G, build_node_index(G), fac, 200.0, 10.0, 500.0)
    assert added.empty
    assert len(out) == len(termini)


def test_no_facilities_means_no_synthetic_termini():
    from force_mains import add_station_junction_termini
    G, fm, topo, termini = _dual_main_station()
    out, added = add_station_junction_termini(
        termini, topo, G, build_node_index(G), None, 200.0, 10.0, 500.0)
    assert added.empty and len(out) == len(termini)
