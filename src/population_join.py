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


class PopulationResult:
    """
    Served population units for one sewershed.

    Attributes
    ----------
    served      GeoDataFrame of parcels intersecting the upstream buffer
    buffer      the dissolved buffer geometry (None if no upstream pipes)
    n_served    parcel count
    """

    def __init__(self, served, buffer):
        self.served = served
        self.buffer = buffer

    @property
    def n_served(self):
        return len(self.served)

    @property
    def is_empty(self):
        return self.served.empty

    def dissolve(self):
        """Return the dissolved sewershed polygon (None if no served units)."""
        if self.served.empty:
            return None
        return self.served.geometry.union_all()


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


def buffer_upstream_pipes(pipes: gpd.GeoDataFrame, pidx_list, buffer_ft: float):
    """Dissolved buffer around the upstream pipes. Returns None if none given."""
    if not pidx_list:
        return None
    return pipes.iloc[list(pidx_list)].buffer(buffer_ft).union_all()


def assign_population_units(pipes: gpd.GeoDataFrame,
                            pidx_list,
                            parcels: gpd.GeoDataFrame,
                            buffer_ft: float) -> PopulationResult:
    """
    Select parcels served by the upstream pipe set (intersect-any rule).

    Parameters
    ----------
    pipes      full gravity-main GeoDataFrame (pidx indexes into it)
    pidx_list  positional pipe indices from TraversalResult.pidx_list
    parcels    population-unit layer (from load_population_units)
    buffer_ft  buffer distance around pipes (pipe_buffer_distance_ft)
    """
    buf = buffer_upstream_pipes(pipes, pidx_list, buffer_ft)
    if buf is None or buf.is_empty:
        return PopulationResult(parcels.iloc[0:0].copy(), buf)

    # Spatial-index prefilter then exact intersects test (intersect-any rule).
    cand = parcels.sindex.query(buf, predicate="intersects")
    served = parcels.iloc[sorted(cand)].copy()
    return PopulationResult(served, buf)
