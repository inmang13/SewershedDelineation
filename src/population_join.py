"""
Phase 5 — population unit assignment.

Given the upstream pipe set from Phase 4 (addressed by `pidx`), buffer those
pipes by `pipe_buffer_distance_ft` and select the population units (parcels)
that fall within the served area. Those parcels are the contributing area for
the sampling point; their dissolved union is the sewershed polygon (built in
Phase 6).

Inclusion rule — intersect-any: a parcel is served if it touches the buffer at
all. This was chosen empirically: validated against 24 hand-delineated sampling
polygons (SiteID == manhole FACILITYID), intersect-any scored a median IoU of
0.64, while stricter rules (centroid-in, >=50% area-in) scored ~0.04. With a
thin 50 ft buffer ribbon the median served parcel is only ~16% inside the
buffer, so excluding edge-straddling parcels discards essentially the whole
catchment. See docs/decision_log.md (2026-06-28, Phase 5).

Boundary parcels are NOT separately excluded or flagged in this phase per that
finding; Phase 6 owns any delineation-level flags.
"""

import geopandas as gpd
import numpy as np


class PopulationResult:
    """
    Served population units for one sewershed.

    Attributes
    ----------
    served      GeoDataFrame of units (parcels or blocks) intersecting the
                selection buffer
    buffer      the dissolved selection buffer (radius = selection_radius_ft;
                None if no upstream pipes) — this is what selected the units
    qc_buffer   the thin QC ribbon (radius = pipe_buffer_distance_ft) used by
                Phase 6's low_population_match. Decoupled from `buffer` so that
                widening the selection radius doesn't fatten the QC ribbon and
                make the coverage flag misfire. Defaults to `buffer` when a
                separate QC radius isn't supplied.
    n_served    unit count
    """

    def __init__(self, served, buffer, qc_buffer=None):
        self.served = served
        self.buffer = buffer
        self.qc_buffer = qc_buffer if qc_buffer is not None else buffer
        self._dissolved = None
        self._dissolved_cached = False

    @property
    def n_served(self):
        return len(self.served)

    @property
    def is_empty(self):
        return self.served.empty

    def dissolve(self):
        """
        Return the dissolved sewershed polygon (None if no served units).

        Cached: union_all over all served parcels is the most expensive op in the
        pipeline and Phase 6 asks for the dissolved polygon several times (flags,
        shapefile, map). Compute it once.
        """
        if not self._dissolved_cached:
            self._dissolved = (None if self.served.empty
                               else self.served.geometry.union_all())
            self._dissolved_cached = True
        return self._dissolved


def load_population_units(cfg: dict) -> gpd.GeoDataFrame:
    """
    Load the population-unit (parcel) layer, reprojected to the working CRS.

    The parcel shapefile is missing CRS metadata, so it is assigned from
    inputs.population_units_crs before reprojecting. Empty/null geometries are
    dropped so the spatial index and area math stay clean.
    """
    inp    = cfg["inputs"]
    params = cfg["parameters"]
    par = gpd.read_file(inp["population_units_shapefile"])
    if par.crs is None:
        # The parcel layer has no CRS, so set_crs here is load-bearing — a wrong
        # value silently mislocates every parcel. Require the config key rather
        # than defaulting to the working CRS (which would mask a typo/omission).
        declared = inp.get("population_units_crs")
        if not declared:
            raise ValueError(
                "parcel layer has no CRS metadata and inputs.population_units_crs "
                "is not set — cannot place parcels"
            )
        par = par.set_crs(declared)
    par = par.to_crs(params["crs"])
    par = par[par.geometry.notna() & ~par.geometry.is_empty].reset_index(drop=True)
    return par


def load_census_blocks(cfg: dict) -> gpd.GeoDataFrame:
    """
    Load census blocks as an alternate population unit, reprojected to the working
    CRS and filtered to the target county.

    The TIGER block file ships a .prj (EPSG:4269 geographic), so — unlike the
    parcels — no CRS assignment is needed; it is reprojected to parameters.crs.
    The file is statewide (~230k blocks for NC), so it is filtered to
    inputs.census_blocks_county_fips (COUNTYFP20) up front for speed. Empty/null
    geometries are dropped.
    """
    inp    = cfg["inputs"]
    params = cfg["parameters"]
    path = inp.get("census_blocks_shapefile")
    if not path:
        raise ValueError("inputs.census_blocks_shapefile is not set — cannot use "
                         "census blocks as a population unit")
    blk = gpd.read_file(path)
    if blk.crs is None:
        declared = inp.get("census_blocks_crs")
        if not declared:
            raise ValueError("census block layer has no CRS and "
                             "inputs.census_blocks_crs is not set")
        blk = blk.set_crs(declared)
    fips = inp.get("census_blocks_county_fips")
    if fips is not None and "COUNTYFP20" in blk.columns:
        blk = blk[blk["COUNTYFP20"] == str(fips)]
    blk = blk.to_crs(params["crs"])
    blk = blk[blk.geometry.notna() & ~blk.geometry.is_empty].reset_index(drop=True)
    return blk


def load_units(cfg: dict, unit_layer: str = "parcels") -> gpd.GeoDataFrame:
    """
    Load the chosen population-unit layer: 'parcels' (default) or 'blocks'.
    A thin dispatcher so callers can select the unit without knowing which loader
    applies.
    """
    if unit_layer == "parcels":
        return load_population_units(cfg)
    if unit_layer == "blocks":
        return load_census_blocks(cfg)
    raise ValueError(f"unknown unit_layer '{unit_layer}'; expected 'parcels' or 'blocks'")


def buffer_upstream_pipes(pipes: gpd.GeoDataFrame, pidx_list, buffer_ft: float):
    """Dissolved buffer around the upstream pipes. Returns None if none given."""
    if not pidx_list:
        return None
    return pipes.iloc[list(pidx_list)].buffer(buffer_ft).union_all()


def assign_population_units(pipes: gpd.GeoDataFrame,
                            pidx_list,
                            units: gpd.GeoDataFrame,
                            selection_radius_ft: float,
                            qc_buffer_ft: float = None) -> PopulationResult:
    """
    Select population units served by the upstream pipe set (intersect-any rule).

    Parameters
    ----------
    pipes                full gravity-main GeoDataFrame (pidx indexes into it)
    pidx_list            positional pipe indices from TraversalResult.pidx_list
    units                population-unit layer (parcels or blocks; from load_units)
    selection_radius_ft  buffer distance around pipes for unit selection
                         (selection_radius_ft). A unit is served if it touches
                         this buffer at all.
    qc_buffer_ft         optional thin QC-ribbon radius (pipe_buffer_distance_ft)
                         carried on the result for Phase 6's low_population_match.
                         When None, the QC ribbon defaults to the selection buffer.

    The selection radius and the QC ribbon are separate on purpose: the sweep
    widens `selection_radius_ft` to capture the true served area, while the QC
    coverage check must stay measured against the original thin pipe buffer or it
    would always look "well covered".
    """
    buf = buffer_upstream_pipes(pipes, pidx_list, selection_radius_ft)
    if buf is None or buf.is_empty:
        return PopulationResult(units.iloc[0:0].copy(), buf, buf)

    # Spatial-index prefilter then exact intersects test (intersect-any rule).
    cand = units.sindex.query(buf, predicate="intersects")
    served = units.iloc[sorted(cand)].copy()

    # Thin QC ribbon, only if a distinct radius is requested (avoids a second
    # buffer op when it would equal the selection buffer anyway).
    if qc_buffer_ft is not None and qc_buffer_ft != selection_radius_ft:
        qc_buf = buffer_upstream_pipes(pipes, pidx_list, qc_buffer_ft)
    else:
        qc_buf = buf
    return PopulationResult(served, buf, qc_buf)


def competing_pipe_check(pipes: gpd.GeoDataFrame,
                         pidx_list,
                         served: gpd.GeoDataFrame,
                         selection_radius_ft: float) -> gpd.GeoDataFrame:
    """
    Annotate served units with competing-pipe metrics (QC round 1 item 2).

    Intersect-any selection can't tell WHOSE pipe a boundary parcel is near: a
    parcel within the selection radius of an in-trace pipe may actually be served
    by a different network's main (a foreign pipe — any gravity main not in this
    trace, whether another basin's or downstream of the target). This compares
    each served unit's distance to the nearest in-trace pipe against the nearest
    foreign pipe and grades the contest:

      cp_flag = "review"   a foreign pipe intersects the unit, or is closer than
                           the nearest in-trace pipe — the foreign main has the
                           stronger claim (review_required).
      cp_flag = "warning"  the in-trace pipe does NOT intersect the unit (it was
                           pulled in by the selection buffer) and a foreign pipe
                           lies within the selection radius but farther — a
                           marginal selection the other network could also claim.
      cp_flag = ""         no foreign pipe within the selection radius, OR an
                           in-trace pipe physically intersects the unit — a
                           decisive claim that a merely-nearby foreign pipe does
                           not contest (intersect is prioritized over proximity).

    Foreign pipes beyond the selection radius are irrelevant by construction
    (they could never have selected the unit), so distances are only resolved
    within that radius; cp_dout is NaN when no foreign pipe is that close.

    Flag-only: the served set is returned annotated, never filtered. Whether
    flagged units should be excluded is a human call made after reviewing a batch
    (decision_log 2026-07-02).

    Added columns (DBF-safe names): cp_din (ft to nearest in-trace pipe),
    cp_dout (ft to nearest foreign pipe, NaN if none within radius), cp_fpipe
    (that pipe's FACILITYID), cp_cross (1 if a foreign pipe intersects the unit),
    cp_flag ("" / "warning" / "review").
    """
    out = served.copy()
    if out.empty:
        for c, v in (("cp_din", np.nan), ("cp_dout", np.nan), ("cp_fpipe", ""),
                     ("cp_cross", 0), ("cp_flag", "")):
            out[c] = v
        return out

    trace = set(pidx_list)
    in_pipes = pipes.iloc[sorted(trace)]
    foreign = pipes.iloc[[i for i in range(len(pipes)) if i not in trace]]
    geoms = out[["geometry"]]

    def _nearest_dist(right, max_distance=None):
        """Per-unit nearest distance (and FACILITYID) to `right` pipes."""
        j = gpd.sjoin_nearest(geoms, right[["FACILITYID", "geometry"]],
                              how="left", max_distance=max_distance,
                              distance_col="_d")
        # Exact-tie duplicates: keep the first match per unit.
        j = j[~j.index.duplicated(keep="first")]
        return j["_d"], j["FACILITYID"].fillna("")

    out["cp_din"], _ = _nearest_dist(in_pipes)
    # A hair over the radius so a unit exactly at the boundary isn't dropped by
    # float noise; anything genuinely beyond stays NaN (no contest possible).
    out["cp_dout"], out["cp_fpipe"] = _nearest_dist(
        foreign, max_distance=selection_radius_ft * (1 + 1e-9))

    crossed = gpd.sjoin(geoms, foreign[["geometry"]],
                        how="inner", predicate="intersects").index.unique()
    out["cp_cross"] = out.index.isin(crossed).astype(int)

    review = (out["cp_cross"] == 1) | (out["cp_dout"] < out["cp_din"])
    # Intersect is prioritized over proximity: an in-trace pipe running through
    # the unit (cp_din == 0) is a decisive claim, so a foreign pipe that is
    # merely nearby (within radius, not crossing, farther) does not contest it.
    # Warn only when the unit is a marginal selection — near but not intersected
    # by any in-trace pipe (cp_din > 0). A foreign pipe that crosses or is closer
    # still escalates to review above regardless of cp_din.
    contested = out["cp_dout"].notna() & ~review & (out["cp_din"] > 0)
    out["cp_flag"] = ""
    out.loc[contested, "cp_flag"] = "warning"
    out.loc[review, "cp_flag"] = "review"
    return out
