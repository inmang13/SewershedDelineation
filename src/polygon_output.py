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

from boundary import build_boundary

SQFT_PER_ACRE = 43560.0

# Default delineation-flag thresholds, overridable from config["parameters"].
DEFAULT_LARGE_CATCHMENT_ACRES = 500.0
DEFAULT_MIN_BUFFER_COVERAGE = 0.5  # untuned — see module docstring

# CSV column order for output/flags.csv. `parcel` is only populated by
# competing_pipe flags (empty for site-level flags).
FLAG_COLUMNS = ["flag_type", "severity", "manhole", "parcel", "description"]


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

    # low_population_match — fraction of the thin QC ribbon that has a served parcel
    # on it. Uses `pop.qc_buffer` (the pipe_buffer_distance_ft ribbon), NOT the
    # selection buffer: the selection radius may be widened for delineation, but the
    # coverage check must stay measured against the thin ribbon or it always reads
    # as fully covered. `sewershed` is the cached dissolved served union.
    qc_ribbon = pop.qc_buffer
    if qc_ribbon is not None and not qc_ribbon.is_empty and qc_ribbon.area > 0:
        covered = sewershed.intersection(qc_ribbon).area
        frac = covered / qc_ribbon.area
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


def compute_competing_flags(served_annotated, manhole: str) -> list[dict]:
    """
    Per-parcel `competing_pipe` flags from a competing_pipe_check-annotated
    served GeoDataFrame (population_join.competing_pipe_check).

    One flag per contested unit; severity maps cp_flag "review" ->
    review_required (a foreign pipe crosses the unit or out-claims the in-trace
    pipe) and "warning" -> warning (foreign pipe within the selection radius but
    farther than the in-trace pipe). Geometry is the parcel polygon so the QC
    GeoPackage shows exactly which parcels are contested.

    Flag-only by design: these rows inform Grace's review; nothing is excluded
    from the sewershed here.
    """
    flags = []
    id_col = next((c for c in ("ALTPARNO", "GEOID20") if c in served_annotated.columns),
                  None)
    hit = served_annotated[served_annotated["cp_flag"] != ""]
    for idx, r in hit.iterrows():
        pid = str(r[id_col]) if id_col else str(idx)
        crossed = bool(r["cp_cross"])
        d_out = r["cp_dout"]
        detail = ("a foreign gravity main crosses this parcel" if crossed else
                  f"nearest foreign main {d_out:.0f} ft vs in-trace "
                  f"{r['cp_din']:.0f} ft")
        flags.append({
            "flag_type": "competing_pipe",
            "severity": ("review_required" if r["cp_flag"] == "review"
                         else "warning"),
            "manhole": manhole,
            "parcel": pid,
            "parcel_id": pid,
            "description": (f"Parcel {pid}: {detail} "
                            f"(foreign pipe {r['cp_fpipe']})."),
            "geometry": r.geometry,
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


def build_boundary_gdf(res, params: dict, served_gdf, served_union=None,
                       blocks_gdf=None):
    """
    One-row GeoDataFrame for output/sewershed_boundary.shp — the final *seamless*
    polygon, or None if there's nothing to build.

    Distinct from build_sewershed_gdf, which returns the raw dissolved served
    union (the gappy intermediate, output/sewershed_parcels.shp). This applies the
    configured boundary_method (morph_close / blocks_dissolve / hybrid / concave)
    to fill the street/ROW gaps into a solid, truth-like polygon.

    Parameters
    ----------
    res           TraversalResult (for the manhole id).
    params        cfg["parameters"] — supplies boundary_method, close_radius_ft,
                  concave_ratio.
    served_gdf    the served units the method operates on: parcels for
                  morph_close/hybrid/concave, selected census blocks for
                  blocks_dissolve.
    served_union  optional precomputed dissolved union of served_gdf (reuse the
                  memoized PopulationResult.dissolve()).
    blocks_gdf    county blocks for gap-filling (hybrid only).
    """
    if served_gdf is None or served_gdf.empty:
        return None
    method = params["boundary_method"]
    geom = build_boundary(
        served_gdf, method,
        close_ft=params.get("close_radius_ft", 100.0),
        concave_ratio=params.get("concave_ratio", 0.3),
        blocks_gdf=blocks_gdf,
        served_union=served_union,
    )
    if geom is None or geom.is_empty:
        return None
    return gpd.GeoDataFrame(
        {
            "manhole":    [str(res.source_value)],
            "area_acres": [round(_acres(geom), 2)],
            "method":     [method],
        },
        geometry=[geom],
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
    df = pd.DataFrame([{c: f.get(c, "") for c in FLAG_COLUMNS} for f in flags],
                      columns=FLAG_COLUMNS)
    df.to_csv(path, index=False, encoding="utf-8")
    return len(df)
