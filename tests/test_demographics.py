"""
Demographic-join math tests (Phase 9 Track G) — drafted from the intended
behaviour, with every expected value hand-computed and the arithmetic shown
inline. Nothing here mirrors the implementation.

Intent being locked in:

  - Dasymetric weighting means population follows RESIDENTIAL LAND, not raw
    area: a census unit whose homes all sit inside the sewershed contributes
    100% of its people even if half its territory is outside — and a unit with
    no residential land contributes nobody, no matter the geometric overlap.
  - Apportioned counts are Σ (weight × value); their margins of error (MOE)
    combine by root-sum-of-squares (the Census aggregation rule).
  - A proportion's MOE follows the Census formula
    sqrt(num_moe² − p²·den_moe²)/den, falling back to "+" when the radicand
    goes negative, and is undefined (None) for a zero denominator.
  - Median income is pooled from the binned B19001 distribution by linear
    interpolation (median-of-medians is invalid); the open-topped $200k+ bin
    can only ever return its floor; zero households → None.
  - Property stats summarize assessor values directly (no census involved),
    with an optional area weight for boundary-straddling parcels.

Pure math + toy geometry only — no data/, no config.yaml, no Census API.
Runs on a clean clone.

Run:  python -m pytest tests/test_demographics.py
"""

import math
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from demographics import (                     # noqa: E402
    dasymetric_weights, apportion, apportion_moe,
    apportion_codes, apportion_moe_codes,
    proportion_moe, pooled_median_income, property_stats,
)

CRS = "EPSG:2264"


# --- dasymetric weights -----------------------------------------------------

def _res_lookup(parcel_boxes):
    """Residential parcels as (geom list, STRtree) — the inputs the weight fn takes."""
    geoms = list(parcel_boxes)
    return geoms, STRtree(geoms)


def test_population_follows_residential_land_not_area():
    # Block: 100x100 square. Its ONLY residential parcel (20x20) sits fully in
    # the west half. Sewershed = west half of the block (x < 50).
    # Raw areal overlap would say weight 0.5; dasymetric intent says ALL the
    # block's homes are inside -> weight 1.0. RED against plain area weighting.
    block = gpd.GeoDataFrame(geometry=[box(0, 0, 100, 100)], crs=CRS)
    res_geoms, res_tree = _res_lookup([box(0, 0, 20, 20)])
    shed = box(0, 0, 50, 100)

    w = dasymetric_weights(block, shed, res_geoms, res_tree)
    assert w.iloc[0] == pytest.approx(1.0)


def test_no_residential_land_means_weight_zero():
    # Same geometry, but the block has NO residential parcel anywhere. Even
    # with 50% geometric overlap, it has no population to give -> weight 0.
    # RED against falling back to areal weighting when the mask is empty.
    block = gpd.GeoDataFrame(geometry=[box(0, 0, 100, 100)], crs=CRS)
    res_geoms, res_tree = _res_lookup([box(500, 500, 520, 520)])  # elsewhere
    shed = box(0, 0, 50, 100)

    w = dasymetric_weights(block, shed, res_geoms, res_tree)
    assert w.iloc[0] == 0.0


def test_straddling_parcel_splits_and_weights_sum_to_one():
    # One residential parcel (40..60 x 0..20, area 400) straddles the seam
    # between sewershed A (x<50) and sewershed B (x>=50). Half its area is in
    # each: w_A = 200/400 = 0.5, w_B = 0.5, and together they account for the
    # whole block's population (sum = 1). RED against double-counting or
    # dropping the boundary parcel.
    block = gpd.GeoDataFrame(geometry=[box(0, 0, 100, 100)], crs=CRS)
    res_geoms, res_tree = _res_lookup([box(40, 0, 60, 20)])
    shed_a = box(0, 0, 50, 100)
    shed_b = box(50, 0, 100, 100)

    w_a = dasymetric_weights(block, shed_a, res_geoms, res_tree).iloc[0]
    w_b = dasymetric_weights(block, shed_b, res_geoms, res_tree).iloc[0]
    assert w_a == pytest.approx(0.5)
    assert w_b == pytest.approx(0.5)
    assert w_a + w_b == pytest.approx(1.0)


# --- count apportionment + MOE ----------------------------------------------

def test_apportion_is_weighted_sum():
    # 0.5·100 + 0.2·50 = 50 + 10 = 60.
    values = pd.Series([100.0, 50.0])
    weights = pd.Series([0.5, 0.2])
    assert apportion(values, weights) == pytest.approx(60.0)


def test_apportion_treats_missing_values_as_zero():
    # A block with no reported value contributes nothing, not NaN poisoning the
    # whole sum: 0.5·100 + 0.5·NaN -> 50. RED against propagating NaN.
    values = pd.Series([100.0, float("nan")])
    weights = pd.Series([0.5, 0.5])
    assert apportion(values, weights) == pytest.approx(50.0)


def test_apportion_moe_is_root_sum_of_squares():
    # Census rule: MOE = sqrt(Σ (w·moe)²) = sqrt((0.5·10)² + (0.5·20)²)
    #            = sqrt(25 + 100) = sqrt(125) ≈ 11.1803.
    # RED against linear summing (would give 15).
    moes = pd.Series([10.0, 20.0])
    weights = pd.Series([0.5, 0.5])
    assert apportion_moe(moes, weights) == pytest.approx(math.sqrt(125.0))


def test_multi_code_concept_sums_counts_and_rss_moes():
    # 'Below poverty' spans two C17002 codes. Counts add linearly:
    # 100 + 50 = 150. Their MOEs combine in quadrature: sqrt(3² + 4²) = 5.
    units = pd.DataFrame({"C1": [100.0], "C2": [50.0],
                          "C1_M": [3.0], "C2_M": [4.0]})
    w = pd.Series([1.0])
    assert apportion_codes(units, w, ["C1", "C2"]) == pytest.approx(150.0)
    assert apportion_moe_codes(units, w, ["C1_M", "C2_M"]) == pytest.approx(5.0)


# --- proportion MOE -----------------------------------------------------------

def test_proportion_moe_census_formula():
    # p = 50/100 = 0.5. radicand = 10² − 0.5²·4² = 100 − 4 = 96.
    # MOE(p) = sqrt(96)/100 ≈ 0.097980.
    assert proportion_moe(50.0, 100.0, 10.0, 4.0) == pytest.approx(
        math.sqrt(96.0) / 100.0)


def test_proportion_moe_negative_radicand_falls_back_to_plus():
    # radicand = 1² − 0.5²·10² = 1 − 25 = −24 < 0 → per Census guidance switch
    # to the ratio form: sqrt(1 + 25)/100 = sqrt(26)/100 ≈ 0.050990.
    # RED against sqrt of a negative (ValueError) or returning None here.
    assert proportion_moe(50.0, 100.0, 1.0, 10.0) == pytest.approx(
        math.sqrt(26.0) / 100.0)


def test_proportion_moe_zero_denominator_is_none():
    assert proportion_moe(0.0, 0.0, 5.0, 5.0) is None


# --- pooled median income -----------------------------------------------------

BINS = [("b1", 0, 10_000), ("b2", 10_000, 20_000), ("b3", 20_000, None)]


def test_pooled_median_interpolates_within_bracket():
    # Apportioned household counts per bin: 5, 10, 0 (weight 1).
    # Total 15, median household = 7.5th. Bin 1 holds 5 (cum 5 < 7.5); the
    # median lands in bin 2 at fraction (7.5−5)/10 = 0.25 of the way through
    # $10k–$20k -> 10000 + 0.25·10000 = 12,500.
    # RED against median-of-medians or returning a bin midpoint.
    bg = pd.DataFrame({"b1": [5.0], "b2": [10.0], "b3": [0.0]})
    w = pd.Series([1.0])
    assert pooled_median_income(bg, w, BINS) == pytest.approx(12_500.0)


def test_pooled_median_respects_weights():
    # Two block-groups with DIFFERENT distributions and unequal weights:
    #   BG1 (all low-income,  b1=10) at weight 1.0 -> apportioned b1 = 10
    #   BG2 (all mid-income,  b2=10) at weight 0.2 -> apportioned b2 = 2
    # Pooled: total 12, median household = 6th, inside bin 1 at 6/10 = 0.6
    # -> 0 + 0.6·10000 = 6,000.
    # Ignoring the weights would pool 10 + 10 = 20, median = 10th = exactly
    # the top of bin 1 -> 10,000. RED against unapportioned pooling.
    bg = pd.DataFrame({"b1": [10.0, 0.0], "b2": [0.0, 10.0], "b3": [0.0, 0.0]})
    w = pd.Series([1.0, 0.2])
    assert pooled_median_income(bg, w, BINS) == pytest.approx(6_000.0)


def test_pooled_median_open_top_bin_returns_its_floor():
    # Counts 2, 2, 20: total 24, median household = 12th, which falls in the
    # open-topped $20k+ bin. No upper bound exists to interpolate against, so
    # the honest answer is the bin floor ($20,000) — a known-understated value.
    bg = pd.DataFrame({"b1": [2.0], "b2": [2.0], "b3": [20.0]})
    w = pd.Series([1.0])
    assert pooled_median_income(bg, w, BINS) == pytest.approx(20_000.0)


def test_pooled_median_no_households_is_none():
    # An industrial/vacant catchment apportions zero households -> None,
    # not a zero-income artifact. (The real 18.01 site is this case.)
    bg = pd.DataFrame({"b1": [0.0], "b2": [0.0], "b3": [0.0]})
    w = pd.Series([1.0])
    assert pooled_median_income(bg, w, BINS) is None


# --- property stats -------------------------------------------------------------

def test_property_stats_plain_summary():
    parcels = pd.DataFrame({"PARVAL": [100_000.0, 200_000.0, 300_000.0]})
    s = property_stats(parcels)
    assert s["n_parcels"] == 3
    assert s["prop_val_total"] == pytest.approx(600_000.0)
    assert s["prop_val_mean"] == pytest.approx(200_000.0)
    assert s["prop_val_median"] == pytest.approx(200_000.0)


def test_property_stats_area_weight_scales_contribution():
    # Boundary-straddling parcel counts proportionally: values 100k (w=1.0)
    # and 200k (w=0.5) -> weighted total 100k + 100k = 200k; weighted mean
    # 200k / (1.0+0.5) = 133,333.33. RED against ignoring the weight.
    parcels = pd.DataFrame({"PARVAL": [100_000.0, 200_000.0]})
    w = pd.Series([1.0, 0.5])
    s = property_stats(parcels, area_weight=w)
    assert s["prop_val_total"] == pytest.approx(200_000.0)
    assert s["prop_val_mean"] == pytest.approx(200_000.0 / 1.5)


def test_property_stats_empty_is_all_none():
    s = property_stats(pd.DataFrame({"PARVAL": []}))
    assert s["n_parcels"] == 0
    assert s["prop_val_total"] is None
    assert s["prop_val_mean"] is None
    assert s["prop_val_median"] is None
