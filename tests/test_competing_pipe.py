"""
Competing-pipe check (QC round 1 item 2) — tests drafted from Grace's stated
intent, not from the code:

  "For each candidate parcel, compare distance to nearest in-trace pipe vs
   nearest out-of-trace gravity main; if a foreign pipe is closer or crosses
   the parcel, flag it."
  "Flag review_required first, auto-exclude only after I've reviewed a batch."

Toy geometry: one in-trace main at x=0, one foreign main at x=60, one far-away
foreign main at x=5000; selection radius 50 ft. Parcels are placed so each rule
fires exactly once:

  P_CLEAN   near the in-trace main only            -> no flag
  P_CROSS   foreign main passes through the parcel -> review_required
  P_CLOSER  foreign main closer than in-trace      -> review_required
  P_WARN    foreign main within 50 ft but farther  -> warning
  P_ONPIPE  in-trace main runs THROUGH the parcel, -> no flag
            foreign main within 50 ft but not         (intersect is decisive;
            crossing                                    proximity doesn't contest it)

Run:  python -m pytest tests/test_competing_pipe.py -q
"""

import sys
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, box

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from population_join import assign_population_units, competing_pipe_check  # noqa: E402
from polygon_output import compute_competing_flags, write_flags_csv        # noqa: E402

CRS = "EPSG:2264"
SEL_R = 50.0
TRACE = [0]  # only the x=0 main is in the upstream trace


@pytest.fixture()
def pipes():
    return gpd.GeoDataFrame(
        {"FACILITYID": ["IN0", "F1", "F2"]},
        geometry=[
            LineString([(0, 0), (0, 400)]),        # pidx 0 — in-trace
            LineString([(60, 0), (60, 400)]),      # pidx 1 — foreign, nearby
            LineString([(5000, 0), (5000, 400)]),  # pidx 2 — foreign, far away
        ],
        crs=CRS,
    )


@pytest.fixture()
def parcels():
    return gpd.GeoDataFrame(
        {"ALTPARNO": ["P_CLEAN", "P_CROSS", "P_CLOSER", "P_WARN", "P_ONPIPE"]},
        geometry=[
            box(-40, 10, -30, 90),   # d_in 30, foreign 90 ft away (out of radius)
            box(30, 110, 70, 190),   # d_in 30, foreign crosses the parcel
            box(35, 210, 45, 290),   # d_in 35, foreign 15 ft — foreign closer
            box(5, 310, 15, 390),    # d_in 5,  foreign 45 ft — contested only
            box(-5, 92, 15, 108),    # d_in 0 (in-trace crosses), foreign 45 ft — decisive
        ],
        crs=CRS,
    )


def annotated(pipes, parcels):
    pop = assign_population_units(pipes, TRACE, parcels, SEL_R)
    return pop, competing_pipe_check(pipes, TRACE, pop.served, SEL_R)


def flag_of(ann, pid):
    return ann.loc[ann["ALTPARNO"] == pid, "cp_flag"].iloc[0]


def test_all_parcels_are_served(pipes, parcels):
    pop, _ = annotated(pipes, parcels)
    assert set(pop.served["ALTPARNO"]) == {
        "P_CLEAN", "P_CROSS", "P_CLOSER", "P_WARN", "P_ONPIPE"}


def test_parcel_near_own_pipe_only_is_not_flagged(pipes, parcels):
    _, ann = annotated(pipes, parcels)
    assert flag_of(ann, "P_CLEAN") == ""


def test_foreign_pipe_crossing_parcel_is_review(pipes, parcels):
    _, ann = annotated(pipes, parcels)
    assert flag_of(ann, "P_CROSS") == "review"
    assert ann.loc[ann["ALTPARNO"] == "P_CROSS", "cp_cross"].iloc[0] == 1


def test_foreign_pipe_closer_than_in_trace_is_review(pipes, parcels):
    _, ann = annotated(pipes, parcels)
    row = ann[ann["ALTPARNO"] == "P_CLOSER"].iloc[0]
    assert row["cp_flag"] == "review"
    assert row["cp_dout"] < row["cp_din"]
    assert row["cp_fpipe"] == "F1"


def test_cp_fpidx_is_the_foreign_pipes_position(pipes, parcels):
    """cp_fpidx carries the foreign pipe's positional index (a stable key when
    FACILITYID is null/duplicated). F1 is pidx 1; -1 when no foreign in range."""
    _, ann = annotated(pipes, parcels)
    assert ann.loc[ann["ALTPARNO"] == "P_CLOSER", "cp_fpidx"].iloc[0] == 1  # F1
    assert ann.loc[ann["ALTPARNO"] == "P_CROSS", "cp_fpidx"].iloc[0] == 1   # F1
    assert ann.loc[ann["ALTPARNO"] == "P_CLEAN", "cp_fpidx"].iloc[0] == -1  # none in range


def test_foreign_pipe_in_radius_but_farther_is_warning(pipes, parcels):
    _, ann = annotated(pipes, parcels)
    row = ann[ann["ALTPARNO"] == "P_WARN"].iloc[0]
    assert row["cp_flag"] == "warning"
    assert row["cp_din"] < row["cp_dout"] <= SEL_R


def test_in_trace_intersecting_parcel_not_contested(pipes, parcels):
    """Intersect is prioritized over proximity: an in-trace pipe running through
    the parcel is decisive, so a foreign pipe merely within radius (not crossing,
    farther) does not flag it. Grace's 183779 case."""
    _, ann = annotated(pipes, parcels)
    row = ann[ann["ALTPARNO"] == "P_ONPIPE"].iloc[0]
    assert row["cp_din"] == 0                       # in-trace pipe intersects
    assert 0 < row["cp_dout"] <= SEL_R              # foreign nearby but not crossing
    assert row["cp_cross"] == 0
    assert row["cp_flag"] == ""                     # decisive — not contested


def test_check_is_flag_only_selection_unchanged(pipes, parcels):
    """Grace's rule: flag first, never silently exclude."""
    pop, ann = annotated(pipes, parcels)
    assert list(ann["ALTPARNO"]) == list(pop.served["ALTPARNO"])
    assert "cp_flag" not in pop.served.columns  # input not mutated


def test_far_foreign_pipe_never_contests(pipes, parcels):
    """F2 at x=5000 must not appear as anyone's competing pipe."""
    _, ann = annotated(pipes, parcels)
    assert "F2" not in set(ann["cp_fpipe"])


def test_flags_carry_parcel_id_severity_and_geometry(pipes, parcels):
    _, ann = annotated(pipes, parcels)
    flags = compute_competing_flags(ann, manhole="30976")
    by_parcel = {f["parcel"]: f for f in flags}
    assert set(by_parcel) == {"P_CROSS", "P_CLOSER", "P_WARN"}
    assert by_parcel["P_CROSS"]["severity"] == "review_required"
    assert by_parcel["P_CLOSER"]["severity"] == "review_required"
    assert by_parcel["P_WARN"]["severity"] == "warning"
    for f in flags:
        assert f["flag_type"] == "competing_pipe"
        assert f["manhole"] == "30976"
        assert f["geometry"] is not None and not f["geometry"].is_empty


def test_flags_csv_mixes_site_and_parcel_rows(pipes, parcels, tmp_path):
    """flags.csv must accept site-level flags (no parcel key) alongside these."""
    _, ann = annotated(pipes, parcels)
    flags = [{"flag_type": "large_catchment", "severity": "warning",
              "manhole": "30976", "description": "site-level"}]
    flags += compute_competing_flags(ann, manhole="30976")
    out = tmp_path / "flags.csv"
    n = write_flags_csv(flags, out)
    assert n == 4
    text = out.read_text(encoding="utf-8")
    assert "parcel" in text.splitlines()[0]
    assert "P_CLOSER" in text


def test_empty_served_set_annotates_without_error(pipes, parcels):
    empty = parcels.iloc[0:0]
    ann = competing_pipe_check(pipes, TRACE, empty, SEL_R)
    assert len(ann) == 0
    assert "cp_flag" in ann.columns


# ---------------------------------------------------------------------------
# Border/inner auto-exclude (Grace's rule, 2026-07-06):
#
#   "A BORDER parcel that does NOT touch the trace pipe but IS crossed by a
#    foreign pipe should not be in the sewershed — auto-exclude it. The same
#    signal on an INNER parcel is a fragment to connect, not a parcel to drop."
#
# Border/inner comes from a LOCAL SURROUND test (_classify_border_inner): a unit
# ringed by other served units on all sides is INNER, one whose ring pokes into
# unserved space is BORDER. NOT judged against the closed footprint — its close
# radius dilates the boundary past the real edge and mislabels edge units inner
# (bug caught 2026-07-06 on 140112#0 / 106791#0). cp_excl requires border AND
# cp_din > 0 AND cp_cross == 1.
#
# In-trace main at x=0; foreign main at x=120 (crosses border test parcels) and
# x=50 (crosses the surrounded inner parcel). Served sets are built directly so
# the surround is controlled.
# ---------------------------------------------------------------------------

from shapely.geometry import box as _box  # noqa: E402
from population_join import _classify_border_inner  # noqa: E402

_EXCL_PIPES = gpd.GeoDataFrame(
    {"FACILITYID": ["IN0", "FB", "FIN"]},
    geometry=[LineString([(0, 0), (0, 500)]),      # pidx 0 — in-trace
              LineString([(120, 0), (120, 500)]),  # pidx 1 — foreign (border parcels)
              LineString([(50, 30), (50, 70)])],   # pidx 2 — foreign (inner parcel)
    crs=CRS)


def _cpc(served, ring=15.0):
    return competing_pipe_check(_EXCL_PIPES, [0], served, SEL_R,
                                border_ring_ft=ring, border_min_expose=0.10)


def test_border_parcel_foreign_cross_no_trace_touch_is_auto_excluded():
    # isolated parcel, foreign FB (x=120) crosses, in-trace 35 ft away -> border+excl
    served = gpd.GeoDataFrame({"ALTPARNO": ["B_EDGE"]},
                              geometry=[_box(35, 100, 130, 180)], crs=CRS)
    r = _cpc(served).iloc[0]
    assert r["cp_pos"] == "border"
    assert r["cp_din"] > 0 and r["cp_cross"] == 1
    assert r["cp_excl"] == 1


def test_in_trace_crossing_border_parcel_is_not_excluded():
    """Strict cp_din > 0: an in-trace pipe running through the parcel (cp_din==0)
    is decisive — not excluded even on the border with a foreign crossing."""
    served = gpd.GeoDataFrame({"ALTPARNO": ["B_ONPIPE"]},
                              geometry=[_box(-10, 400, 130, 420)], crs=CRS)
    r = _cpc(served).iloc[0]
    assert r["cp_din"] == 0 and r["cp_cross"] == 1
    assert r["cp_excl"] == 0


def test_surrounded_parcel_with_foreign_cross_is_inner_and_not_excluded():
    """A parcel ringed by other served units is INNER — a foreign crossing is a
    fragment to connect, not grounds to drop (cp_excl stays 0)."""
    center = _box(40, 40, 60, 60)                 # foreign FIN (x=50) crosses it
    # top/bottom span the full width so the ring corners are covered too.
    ring = [_box(10, 60, 90, 90), _box(10, 10, 90, 40),      # top, bottom
            _box(10, 40, 40, 60), _box(60, 40, 90, 60)]       # left, right
    served = gpd.GeoDataFrame(
        {"ALTPARNO": ["CENTER", "T", "B", "L", "R"]},
        geometry=[center] + ring, crs=CRS)
    r = _cpc(served).loc[served["ALTPARNO"] == "CENTER"].iloc[0]
    assert r["cp_pos"] == "inner"
    assert r["cp_cross"] == 1 and r["cp_excl"] == 0


def test_classify_border_inner_surround_vs_exposed():
    """The classifier itself: an isolated unit is border; one ringed by served
    neighbours on all sides is inner."""
    lone = gpd.GeoSeries([_box(0, 0, 10, 10)], crs=CRS)
    assert _classify_border_inner(lone, 20.0, 0.10) == ["border"]
    center = _box(40, 40, 60, 60)
    ring = [_box(10, 60, 90, 90), _box(10, 10, 90, 40),   # top, bottom (full width)
            _box(10, 40, 40, 60), _box(60, 40, 90, 60)]    # left, right
    geoms = gpd.GeoSeries([center] + ring, crs=CRS)
    assert _classify_border_inner(geoms, 20.0, 0.10)[0] == "inner"


# ---------------------------------------------------------------------------
# Explode multipart parcels + equidistant split (Grace's rule, 2026-07-06):
#
#   "Split multipart parcels into individual parts — most parts go clean once
#    split." + "The split should be only the border, contested parcels" (a part
#    where BOTH an in-trace and a foreign pipe cross → keep the in-trace side).
#
# Option A (advisor): split only border + cp_cross==1 + cp_din==0 (both pipes
# cross). border + cp_din>0 (foreign only) stays a FULL exclude; inner never
# splits.
# ---------------------------------------------------------------------------

from shapely.geometry import MultiPolygon, box as _b  # noqa: E402
from population_join import _explode_to_parts, split_border_contested  # noqa: E402

_SPLIT_CFG = {"parameters": {"split_border_contested": True,
                             "split_near_radius_ft": 100.0,
                             "split_densify_step_ft": 3.0}}


def test_explode_multipart_gives_unique_partid_and_keeps_original_id():
    g = gpd.GeoDataFrame(
        {"ALTPARNO": ["A", "B"]},
        geometry=[MultiPolygon([_b(0, 0, 10, 10), _b(20, 0, 30, 10)]),  # 2 parts
                  _b(40, 0, 50, 10)],                                    # 1 part
        crs=CRS)
    out = _explode_to_parts(g)
    assert len(out) == 3                       # 2 + 1 parts
    assert out["PARTID"].is_unique
    assert set(out["PARTID"]) == {"A#0", "A#1", "B#0"}
    assert list(out["ALTPARNO"]) == ["A", "A", "B"]   # original id preserved


def _split_pipes():
    # in-trace main at x=0, foreign main at x=40; equidistant line is x=20.
    return gpd.GeoDataFrame(
        {"FACILITYID": ["IN", "FOR"]},
        geometry=[LineString([(0, 0), (0, 80)]), LineString([(40, 0), (40, 80)])],
        crs=CRS)


def _served(cp_pos, cp_din, cp_cross, geom):
    return gpd.GeoDataFrame(
        {"ALTPARNO": ["P"], "cp_pos": [cp_pos], "cp_din": [float(cp_din)],
         "cp_cross": [int(cp_cross)]}, geometry=[geom], crs=CRS)


def test_border_both_cross_is_split_to_in_trace_side():
    parcel = _b(-20, 0, 60, 80)   # in-trace crosses (x=0), foreign crosses (x=40)
    served = _served("border", 0.0, 1, parcel)
    out = split_border_contested(served, _split_pipes(), [0], 50.0, _SPLIT_CFG,
                                 ignore_pidx=set())
    assert len(out) == 1
    kept = out.geometry.iloc[0]
    assert abs(out["cp_keep"].iloc[0] - 0.5) < 0.05     # equidistant at x=20 → ~half
    assert kept.bounds[2] <= 21                          # kept side is x <= ~20


def test_inner_parcel_is_never_split():
    served = _served("inner", 0.0, 1, _b(-20, 0, 60, 80))
    out = split_border_contested(served, _split_pipes(), [0], 50.0, _SPLIT_CFG,
                                 ignore_pidx=set())
    assert out["cp_keep"].iloc[0] == 1.0
    assert out.geometry.iloc[0].equals(_b(-20, 0, 60, 80))   # geometry untouched


def test_border_foreign_only_din_gt_0_is_not_split():
    """cp_din>0 (foreign crosses, no in-trace pipe does) is the full-exclude case,
    handled before the split by dropping cp_excl — the splitter leaves it whole."""
    served = _served("border", 8.0, 1, _b(-20, 0, 60, 80))
    out = split_border_contested(served, _split_pipes(), [0], 50.0, _SPLIT_CFG,
                                 ignore_pidx=set())
    assert out["cp_keep"].iloc[0] == 1.0


# ---------------------------------------------------------------------------
# Buffered-pipe area assignment for remaining contested units (Grace, 2026-07-06):
#
#   "Buffer the competing pipes. Assign the parcel to the pipe with the most
#    area intersecting the buffer." + "aggregate in-trace buffers."
#
# AGGREGATE: area(unit ∩ union(in-trace pipe buffers)) vs area(unit ∩ union(
# foreign pipe buffers)); in-trace wins (>=) -> keep, foreign wins -> exclude.
# Label only (no rows dropped); only still-open contested units (cp_flag != ""
# and cp_keep == 1) are evaluated.
# ---------------------------------------------------------------------------

from population_join import assign_remaining_by_buffer  # noqa: E402

_ASGN_PIPES = gpd.GeoDataFrame(
    {"FACILITYID": ["INA", "INB", "FOR"]},
    geometry=[LineString([(0, 0), (0, 200)]),      # pidx 0 in-trace
              LineString([(40, 0), (40, 200)]),    # pidx 1 in-trace
              LineString([(100, 0), (100, 200)])], # pidx 2 foreign
    crs=CRS)
_ASGN_TRACE = [0, 1]


def _asgn_cfg(buf=50.0):
    return {"parameters": {"competing_assign_by_buffer": True,
                           "competing_assign_buffer_ft": buf}}


def _served_asgn(rows):   # rows: (id, cp_flag, cp_keep, geom)
    return gpd.GeoDataFrame(
        {"ALTPARNO": [r[0] for r in rows], "cp_flag": [r[1] for r in rows],
         "cp_keep": [r[2] for r in rows]},
        geometry=[r[3] for r in rows], crs=CRS)


def _asgn_of(out, pid):
    return out.loc[out["ALTPARNO"] == pid, "cp_asgn"].iloc[0]


def test_assign_keep_when_in_trace_buffers_cover_more():
    served = _served_asgn([("P", "review", 1.0, _box(10, 0, 30, 20))])  # near x=0
    out = assign_remaining_by_buffer(served, _ASGN_PIPES, _ASGN_TRACE,
                                     _asgn_cfg(), ignore_pidx=set())
    assert _asgn_of(out, "P") == "keep"
    assert len(out) == 1                       # label only, no rows dropped


def test_assign_exclude_when_foreign_buffer_covers_more():
    served = _served_asgn([("P", "review", 1.0, _box(95, 0, 115, 20))])  # on x=100
    out = assign_remaining_by_buffer(served, _ASGN_PIPES, _ASGN_TRACE,
                                     _asgn_cfg(), ignore_pidx=set())
    assert _asgn_of(out, "P") == "exclude"


def test_unflagged_and_split_units_are_not_evaluated():
    served = _served_asgn([("CLEAN", "", 1.0, _box(10, 0, 30, 20)),
                           ("SPLIT", "review", 0.5, _box(95, 0, 115, 20))])
    out = assign_remaining_by_buffer(served, _ASGN_PIPES, _ASGN_TRACE,
                                     _asgn_cfg(), ignore_pidx=set())
    assert _asgn_of(out, "CLEAN") == ""        # not contested
    assert _asgn_of(out, "SPLIT") == ""        # already split (cp_keep < 1)


def test_aggregate_in_trace_beats_a_single_foreign_pipe():
    """The point of aggregating: neither in-trace pipe alone covers more of the
    unit than the foreign pipe, but the UNION of the two in-trace buffers does,
    so the unit is kept (a single-best-pipe rule would have excluded it)."""
    pipes = gpd.GeoDataFrame(
        {"FACILITYID": ["INA", "INB", "FOR"]},
        geometry=[LineString([(10, 0), (10, 20)]),    # in-trace, covers left
                  LineString([(90, 0), (90, 20)]),    # in-trace, covers right
                  LineString([(50, 0), (50, 20)])],   # foreign, covers middle
        crs=CRS)
    served = _served_asgn([("P", "review", 1.0, _box(0, 0, 100, 20))])
    out = assign_remaining_by_buffer(served, pipes, [0, 1], _asgn_cfg(buf=15.0),
                                     ignore_pidx=set())
    assert _asgn_of(out, "P") == "keep"        # union(2 in-trace) 50 > foreign 30
