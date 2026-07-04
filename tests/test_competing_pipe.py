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
