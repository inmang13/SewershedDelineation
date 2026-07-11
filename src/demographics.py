"""
Demographic apportionment engine for the sewershed phase.

Attaches socioeconomic characteristics to each sewershed by apportioning census
counts from the geographies they live on (blocks for race/ethnicity, block-groups
for income/poverty/SNAP) into the sewershed polygon. Property value comes
straight off the parcels — it needs no apportionment because parcels are already
inside the polygon.

Apportionment method — DASYMETRIC (residential-parcel mask). A census unit that
straddles the sewershed boundary contributes population in proportion to how much
of its *residential* land (not raw area) falls inside, so counts aren't spread
over parks, ROW, water, or industrial land. Formula, for census unit u and
sewershed S with residential mask R:

    weight_u = area(R ∩ u ∩ S) / area(R ∩ u)
    apportioned_count = count_u * weight_u

Correctness rules honored here (each variable uses its OWN universe — do not
reuse total population as a denominator):
  * race/ethnicity %  = category / decennial total population   (person universe)
  * poverty rate      = below-poverty / B17001 poverty universe (its own total,
                        excludes institutional/group-quarters pop)
  * SNAP rate         = SNAP households / total households       (household universe)

Margins of error: decennial data (population/race/ethnicity) is a full count —
no MOE. ACS counts (poverty, SNAP) propagate MOE via root-sum-of-squares on the
apportioned components, and rate MOEs use the Census proportion formula. The
pooled median income is interpolated from the pooled income histogram; per the
plan its MOE is NOT propagated by the count formula (that would be wrong for a
median) and is footnoted instead.
"""

import math

import numpy as np
import pandas as pd
from shapely.strtree import STRtree


# ---------------------------------------------------------------------------
# Residential mask
# ---------------------------------------------------------------------------

def build_residential_mask(parcels, cfg) -> "gpd.GeoDataFrame":
    """
    Filter the parcel layer to residential land, to serve as the dasymetric
    population surface.

    the city's `PARUSECODE` is a ZONING code, not land use — the actual use is in
    `PARUSEDESC` ("RES/ 1-FAMILY", "COM/ APT-GARDEN", "VACANT LAND", ...). A
    parcel is residential if its description starts with one of
    `residential_desc_prefixes` (default "RES/") OR contains one of
    `residential_desc_contains` (default apartment/converted-residence codes,
    which the assessor files under COM/). Vacant land ("VAC ...") is excluded
    even when residential-zoned — nobody lives there.
    """
    dcfg = cfg.get("demographics", {})
    col = dcfg.get("land_use_desc_column", "PARUSEDESC")
    prefixes = tuple(dcfg.get("residential_desc_prefixes", ["RES/"]))
    contains = list(dcfg.get("residential_desc_contains", ["APT", "CONVERTED RESID"]))

    desc = parcels[col].fillna("")
    is_res = desc.str.startswith(prefixes)
    for token in contains:
        is_res = is_res | desc.str.contains(token, regex=False)
    # Never count vacant land as populated, whatever else matched.
    is_res = is_res & ~desc.str.startswith("VAC")
    return parcels[is_res].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Dasymetric weights
# ---------------------------------------------------------------------------

def _residential_area_within(geom, res_geoms, res_tree: STRtree) -> float:
    """Total residential-parcel area inside `geom` (a census unit, or a unit ∩
    sewershed). Uses an STRtree over residential parcels for candidate lookup."""
    if geom is None or geom.is_empty:
        return 0.0
    idx = res_tree.query(geom)
    total = 0.0
    for i in idx:
        inter = res_geoms[i].intersection(geom)
        if not inter.is_empty:
            total += inter.area
    return total


def dasymetric_weights(units, sewershed_geom, res_geoms, res_tree) -> pd.Series:
    """
    Fraction of each census unit's residential land that falls inside the
    sewershed. Returns a Series aligned to `units.index`, each value in [0, 1].

    A unit with no residential land (denominator 0) gets weight 0 — it has no
    population to give regardless of geometric overlap.
    """
    weights = {}
    for idx, u in units.geometry.items():
        u_in_s = u.intersection(sewershed_geom)
        denom = _residential_area_within(u, res_geoms, res_tree)
        if denom <= 0:
            weights[idx] = 0.0
            continue
        numer = _residential_area_within(u_in_s, res_geoms, res_tree)
        weights[idx] = min(1.0, numer / denom)
    return pd.Series(weights, dtype=float)


# ---------------------------------------------------------------------------
# Count apportionment + MOE
# ---------------------------------------------------------------------------

def apportion(values: pd.Series, weights: pd.Series) -> float:
    """Σ w_u · value_u — the apportioned count for one variable."""
    return float((values.fillna(0) * weights).sum())


def _as_list(codes) -> list:
    return codes if isinstance(codes, (list, tuple)) else [codes]


def apportion_codes(units, weights, codes) -> float:
    """Apportioned count summed over one or more variable codes. A census concept
    can span several codes — e.g. 'below poverty' in C17002 is the ratio<0.50 bin
    plus the 0.50-0.99 bin."""
    return sum(apportion(units[c], weights) for c in _as_list(codes))


def apportion_moe_codes(units, weights, codes) -> float:
    """MOE of a count summed over one or more codes: RSS across both the codes and
    the apportioned units (independent components combine in quadrature)."""
    return math.sqrt(sum(apportion_moe(units[c], weights) ** 2
                         for c in _as_list(codes)))


def apportion_moe(moes: pd.Series, weights: pd.Series) -> float:
    """
    MOE of an apportioned (weighted-sum) count via root-sum-of-squares:
        MOE = sqrt( Σ (w_u · MOE_u)^2 ).
    Scaling a count by a fraction scales its MOE by the same fraction; summing
    independent components combines in quadrature (Census aggregation rule).
    """
    scaled = (weights * moes.fillna(0)) ** 2
    return float(math.sqrt(scaled.sum()))


def proportion_moe(num, den, num_moe, den_moe) -> float | None:
    """
    MOE of a proportion p = num/den by the Census formula:
        MOE(p) = sqrt( num_moe^2 - p^2 · den_moe^2 ) / den
    Falls back to the ratio formula (+) when the radicand is negative, per Census
    guidance. Returns None if the denominator is 0.
    """
    if not den:
        return None
    p = num / den
    radicand = num_moe ** 2 - (p ** 2) * (den_moe ** 2)
    if radicand < 0:
        radicand = num_moe ** 2 + (p ** 2) * (den_moe ** 2)
    return math.sqrt(radicand) / den


# ---------------------------------------------------------------------------
# Pooled median income
# ---------------------------------------------------------------------------

def pooled_median_income(bg_units, weights, income_bins) -> float | None:
    """
    Sewershed median household income, pooled from the ACS B19001 binned income
    distribution across apportioned block-groups (median-of-medians is invalid).

    `income_bins` is an ordered list of (estimate_col, lower_bound, upper_bound)
    for the 16 income brackets (upper_bound None for the open-topped bin). Each
    bin's household count is apportioned (Σ w · HH), the cumulative histogram is
    built, and the median household is located by linear interpolation within its
    bracket. Returns None if no households are apportioned.
    """
    counts = []
    for col, lo, hi in income_bins:
        counts.append((apportion(bg_units[col], weights), lo, hi))
    total = sum(c for c, _, _ in counts)
    if total <= 0:
        return None

    half = total / 2.0
    cum = 0.0
    for count, lo, hi in counts:
        if cum + count >= half:
            if count <= 0:
                return float(lo)
            if hi is None:            # open-topped final bin — return its floor
                return float(lo)
            frac = (half - cum) / count
            return float(lo + frac * (hi - lo))
        cum += count
    return float(counts[-1][1])


# ---------------------------------------------------------------------------
# Property value (parcels — no census apportionment)
# ---------------------------------------------------------------------------

def property_stats(served_parcels, value_col="PARVAL", area_weight=None) -> dict:
    """
    Property-value summary over the served parcels. Values come from the assessor
    layer, which already sits inside the polygon, so no census apportionment is
    needed. `area_weight` (optional Series in [0,1]) scales each parcel's value by
    the fraction of the parcel inside the sewershed, so a boundary-straddling
    parcel contributes proportionally (analogous to the split `cp_keep` fraction).
    """
    if served_parcels is None or len(served_parcels) == 0:
        return {"n_parcels": 0, "prop_val_total": None,
                "prop_val_mean": None, "prop_val_median": None}
    vals = pd.to_numeric(served_parcels[value_col], errors="coerce")
    if area_weight is not None:
        wtot = float((vals.fillna(0) * area_weight).sum())
        wsum = float(area_weight[vals.notna()].sum())
        mean = wtot / wsum if wsum else None
    else:
        wtot = float(vals.sum())
        mean = float(vals.mean())
    return {
        "n_parcels": int(len(served_parcels)),
        "prop_val_total": wtot,
        "prop_val_mean": mean,
        "prop_val_median": float(vals.median()) if vals.notna().any() else None,
    }


# ---------------------------------------------------------------------------
# Per-site assembly
# ---------------------------------------------------------------------------

def demographics_for_site(sewershed_geom, blocks, bgs, res_geoms, res_tree,
                          served_parcels, spec, served_area_weight=None) -> dict:
    """
    Compute the full demographic record for one sewershed.

    Parameters
    ----------
    sewershed_geom : shapely polygon (working CRS, feet)
    blocks         : GeoDataFrame of decennial blocks intersecting the site,
                     with the decennial count columns joined
    bgs            : GeoDataFrame of ACS block-groups intersecting the site, with
                     the ACS estimate/MOE columns joined
    res_geoms, res_tree : residential-parcel geometries + STRtree (dasymetric mask)
    served_parcels : parcels selected for this site (for property value)
    spec           : dict of variable roles (see run_demographics for construction):
                       total_pop, race{label:code}, ethnicity{label:code},
                       poverty(num,den, num_moe,den_moe),
                       snap(num,den, num_moe,den_moe),
                       income_bins[(col,lo,hi)]
    served_area_weight : optional per-parcel clip fraction for property value

    Returns a flat dict of stats + a `n_block_groups` confidence column.
    """
    rec = {}

    # --- Blocks: population, race, ethnicity (person universe, no MOE) ---
    block_w = dasymetric_weights(blocks, sewershed_geom, res_geoms, res_tree)
    total_pop = apportion(blocks[spec["total_pop"]], block_w)
    rec["population"] = round(total_pop)

    for label, code in spec["race"].items():
        cnt = apportion(blocks[code], block_w)
        rec[f"race_{label}"] = round(cnt)
        rec[f"pct_{label}"] = (100.0 * cnt / total_pop) if total_pop else None

    for label, code in spec.get("ethnicity", {}).items():
        cnt = apportion(blocks[code], block_w)
        rec[f"eth_{label}"] = round(cnt)
        rec[f"pct_{label}"] = (100.0 * cnt / total_pop) if total_pop else None

    # --- Block-groups: poverty, SNAP, income (ACS, MOE-bearing) ---
    bg_w = dasymetric_weights(bgs, sewershed_geom, res_geoms, res_tree)
    rec["n_block_groups"] = int((bg_w > 0).sum())

    pv = spec["poverty"]
    pov_num = apportion_codes(bgs, bg_w, pv["num"])
    pov_den = apportion_codes(bgs, bg_w, pv["den"])
    rec["poverty_universe"] = round(pov_den)
    rec["poverty_below"] = round(pov_num)
    rec["poverty_rate"] = (100.0 * pov_num / pov_den) if pov_den else None
    rec["poverty_rate_moe"] = _rate_moe_pct(bgs, bg_w, pv, pov_num, pov_den)

    sn = spec["snap"]
    snap_num = apportion_codes(bgs, bg_w, sn["num"])
    snap_den = apportion_codes(bgs, bg_w, sn["den"])
    rec["snap_households"] = round(snap_num)
    rec["total_households"] = round(snap_den)
    rec["snap_rate"] = (100.0 * snap_num / snap_den) if snap_den else None
    rec["snap_rate_moe"] = _rate_moe_pct(bgs, bg_w, sn, snap_num, snap_den)

    rec["median_income"] = pooled_median_income(bgs, bg_w, spec["income_bins"])
    # Median MOE intentionally not propagated (count RSS is invalid for a median).
    rec["median_income_moe"] = None

    # --- Property value (parcels, no apportionment) ---
    rec.update(property_stats(served_parcels,
                              value_col=spec.get("property_value_col", "PARVAL"),
                              area_weight=served_area_weight))
    return rec


def _rate_moe_pct(bgs, weights, roles, num, den):
    """Percentage-point MOE for an ACS rate, if MOE columns are present."""
    if "num_moe" not in roles or "den_moe" not in roles:
        return None
    num_moe = apportion_moe_codes(bgs, weights, roles["num_moe"])
    den_moe = apportion_moe_codes(bgs, weights, roles["den_moe"])
    m = proportion_moe(num, den, num_moe, den_moe)
    return 100.0 * m if m is not None else None
