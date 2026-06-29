"""
Phase 6 — polygon construction + delineation flags.

Consumes the Phase 4 trace (`TraversalResult`) and the Phase 5 population join
(`PopulationResult`) and produces the shipped artifacts for one sampling point:

  - the dissolved sewershed polygon            -> output/sewershed.shp
  - the contributing pipes (debug review layer) -> output/debug_upstream_pipes.shp
  - delineation-level flags                     -> output/flags.csv
  - a one-page overview map (in pdf_maps.py)    -> output/flag_maps.pdf

Delineation flags answer "is this sewershed result trustworthy?" — distinct from
the Phase 2 network flags ("is this *pipe* trustworthy?"), which live in
output/network_qa_flags.csv. Three flags:

  no_upstream_found    review_required  empty trace (headwater / not connected upstream)
  large_catchment      warning          area exceeds large_catchment_threshold_acres
  low_population_match  warning          little of the buffer ribbon overlaps any parcel

The original spec also listed `boundary_parcel`, but Phase 5 established that the
median served parcel is only ~16% inside the buffer — edge-straddling parcels are
the norm, not an anomaly (intersect-any rule, decision_log 2026-06-28). A flag on
"parcel straddles the buffer edge" would fire on nearly every parcel, so it was
dropped (user sign-off, 2026-06-29).

`low_population_match` is scale-invariant on purpose: it measures the fraction of
the buffer ribbon that overlaps a served parcel, NOT an absolute parcel count. A
legitimately small catchment still has near-full coverage; a low value means pipes
run through unparcelled ground (coverage gap, undeveloped/industrial land, or a CRS
mismatch). Its threshold is untuned — there is no validation set for it the way
intersect-any was validated.
"""

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

SQFT_PER_ACRE = 43560.0

# Default delineation-flag thresholds, overridable from config["parameters"].
DEFAULT_LARGE_CATCHMENT_ACRES = 500.0
DEFAULT_MIN_BUFFER_COVERAGE = 0.5  # untuned — see module docstring

# CSV column order for output/flags.csv.
FLAG_COLUMNS = ["flag_type", "severity", "manhole", "description"]


def _acres(geom) -> float:
    """Area in acres (0.0 for None/empty)."""
    if geom is None or geom.is_empty:
        return 0.0
    return geom.area / SQFT_PER_ACRE


def compute_flags(res, pop, params: dict) -> list[dict]:
    """
    Delineation-level flags for one sewershed.

    Parameters
    ----------
    res     TraversalResult (Phase 4)
    pop     PopulationResult (Phase 5)
    params  cfg["parameters"]

    Returns a list of flag dicts: flag_type, severity, manhole, description, and a
    `geometry` (Point) for the overview map. Empty list = clean result.
    """
    flags: list[dict] = []
    manhole = str(res.source_value)
    target_pt = Point(res.target_xy)

    # no_upstream_found — empty trace. Nothing else is computable, so return early.
    # (Do NOT inherit run_population_join's sys.exit on is_empty — that would skip
    # the flag entirely.)
    if res.is_empty:
        flags.append({
            "flag_type": "no_upstream_found",
            "severity": "review_required",
            "manhole": manhole,
            "description": ("Traversal returned zero upstream pipes — target is a "
                            "headwater or not connected upstream on the modeled "
                            "gravity-main network."),
            "geometry": target_pt,
        })
        return flags

    # Pipes exist but the buffer caught no parcels at all — a coverage/CRS smell.
    if pop.is_empty:
        flags.append({
            "flag_type": "low_population_match",
            "severity": "warning",
            "manhole": manhole,
            "description": ("Upstream pipes exist but no parcels intersect the "
                            "buffer — check CRS alignment and parcel coverage near "
                            "this site."),
            "geometry": target_pt,
        })
        return flags

    sewershed = pop.dissolve()
    area_ac = _acres(sewershed)

    # large_catchment — trace may have run past a real outlet.
    thresh_ac = params.get("large_catchment_threshold_acres",
                           DEFAULT_LARGE_CATCHMENT_ACRES)
    if area_ac > thresh_ac:
        flags.append({
            "flag_type": "large_catchment",
            "severity": "warning",
            "manhole": manhole,
            "description": (f"Sewershed area {area_ac:,.0f} acres exceeds the "
                            f"{thresh_ac:,.0f}-acre threshold — verify the trace "
                            f"didn't run past a real outlet."),
            "geometry": sewershed.centroid,
        })

    # low_population_match — fraction of the buffer ribbon that has a served parcel
    # on it. `sewershed` is already the dissolved served union (cached), so reuse it
    # rather than dissolving the parcels again.
    if pop.buffer is not None and not pop.buffer.is_empty and pop.buffer.area > 0:
        covered = sewershed.intersection(pop.buffer).area
        frac = covered / pop.buffer.area
        min_frac = params.get("low_population_match_min_buffer_coverage",
                              DEFAULT_MIN_BUFFER_COVERAGE)
        if frac < min_frac:
            flags.append({
                "flag_type": "low_population_match",
                "severity": "warning",
                "manhole": manhole,
                "description": (f"Only {frac:.0%} of the pipe buffer overlaps served "
                                f"parcels (threshold {min_frac:.0%}) — possible "
                                f"parcel-coverage gap. Threshold is untuned."),
                "geometry": sewershed.centroid,
            })

    return flags


def build_sewershed_gdf(res, pop, params: dict):
    """
    One-row GeoDataFrame for output/sewershed.shp, or None if there's no polygon.

    Field names are kept <=10 chars for the shapefile/DBF format. Schema is
    provisional pending the GIS maintainer's preferred field names (open question
    in roadmap.md).
    """
    sewershed = pop.dissolve()
    if sewershed is None or sewershed.is_empty:
        return None
    return gpd.GeoDataFrame(
        {
            "manhole":    [str(res.source_value)],
            "area_acres": [round(_acres(sewershed), 2)],
            "n_parcels":  [pop.n_served],
            "n_pipes":    [res.n_edges],
            "max_depth":  [res.max_depth],
        },
        geometry=[sewershed],
        crs=params["crs"],
    )


def build_debug_pipes_gdf(pipes: gpd.GeoDataFrame, res):
    """
    The contributing pipes as a review layer, with traversal depth, or None if the
    trace was empty. `res.pidx_list` and `res.edges` are aligned (both built from
    the same edge list), so depth maps positionally.
    """
    if res.is_empty:
        return None
    sub = pipes.iloc[res.pidx_list].copy()
    sub["depth"] = [e["depth"] for e in res.edges]
    sub["manhole"] = str(res.source_value)
    return sub


def write_flags_csv(flags: list[dict], path) -> int:
    """
    Write output/flags.csv (UTF-8). Always writes a header even when there are no
    flags, so a clean run is distinguishable from a run that never produced the
    file. Returns the row count.
    """
    df = pd.DataFrame([{c: f[c] for c in FLAG_COLUMNS} for f in flags],
                      columns=FLAG_COLUMNS)
    df.to_csv(path, index=False, encoding="utf-8")
    return len(df)
