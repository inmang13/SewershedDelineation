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
from shapely.geometry import Polygon
from shapely.ops import unary_union


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


def _explode_to_parts(units: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Explode multipart units into single-part rows and stamp a unique ``PARTID``.

    A multipart parcel (one assessor record, several disjoint polygons) is served
    as a whole today, so ONE part touching a foreign main flags the entire record
    and a part beyond the selection radius rides in on its siblings. Exploding
    lets each part be selected and contested on its own — most parts go clean, and
    ride-along parts drop out.

    ``PARTID`` = ``<original id>#<part index>`` (falls back to the row position
    when the layer has no id column). The original id column is left intact for the
    downstream demographic join; a split part carries its parent's attributes and
    is apportioned later.
    """
    from polygon_output import BASE_UNIT_ID_COLUMNS
    exploded = units.explode(index_parts=False).reset_index(drop=True)
    base = next((c for c in BASE_UNIT_ID_COLUMNS if c in exploded.columns), None)
    if base is not None:
        part_n = exploded.groupby(base).cumcount().astype(str)
        exploded["PARTID"] = exploded[base].astype(str) + "#" + part_n
    else:
        exploded["PARTID"] = exploded.index.astype(str)
    return exploded


def load_units(cfg: dict, unit_layer: str = "parcels") -> gpd.GeoDataFrame:
    """
    Load the chosen population-unit layer: 'parcels' (default) or 'blocks'.
    A thin dispatcher so callers can select the unit without knowing which loader
    applies. Multipart units are exploded into single-part rows (config
    ``explode_multipart_units``, default on) so each part selects/contests on its
    own — see ``_explode_to_parts``.
    """
    if unit_layer == "parcels":
        units = load_population_units(cfg)
    elif unit_layer == "blocks":
        units = load_census_blocks(cfg)
    else:
        raise ValueError(
            f"unknown unit_layer '{unit_layer}'; expected 'parcels' or 'blocks'")
    if cfg["parameters"].get("explode_multipart_units", True):
        units = _explode_to_parts(units)
    return units


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


def _classify_border_inner(units_geom, ring_ft, min_expose):
    """Label each served unit "border" or "inner" by a local surround test.

    For each unit, take the ring of width `ring_ft` just outside it and measure
    how much of that ring is NOT covered by other served units. A unit ringed by
    served neighbours on all sides (streets bridged up to `ring_ft`) has a
    near-fully-covered ring → INNER; a unit whose ring pokes into unserved space
    (more than `min_expose` fraction uncovered) is on the sewershed edge → BORDER.

    Independent of the output close radius — unlike judging against the closed
    footprint, which mislabels edge units as inner because the close dilates the
    boundary ~close_radius past the real served edge. Uses an STRtree so each
    unit only unions its actual neighbours (O(n·k), not O(n^2)).
    """
    from shapely import STRtree
    geoms = list(units_geom.values)
    if not geoms:
        return []
    tree = STRtree(geoms)
    labels = []
    for i, g in enumerate(geoms):
        ring = g.buffer(ring_ft).difference(g)
        if ring.is_empty or ring.area <= 0:
            labels.append("inner")
            continue
        neigh = [geoms[j] for j in tree.query(ring) if j != i]
        others = unary_union(neigh) if neigh else None
        uncovered = ring.area if others is None else ring.difference(others).area
        labels.append("border" if uncovered / ring.area > min_expose else "inner")
    return labels


def competing_pipe_check(pipes: gpd.GeoDataFrame,
                         pidx_list,
                         served: gpd.GeoDataFrame,
                         selection_radius_ft: float,
                         border_ring_ft: float = 75.0,
                         border_min_expose: float = 0.10,
                         border_min_cover_frac: float = 0.0,
                         border_min_cover_area_ft2: float = 0.0,
                         ignore_pidx=None) -> gpd.GeoDataFrame:
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
    (decision_log 2026-07-02) — except cp_excl, an auto-exclude the consuming
    pass applies (border unit, foreign pipe crosses, no in-trace pipe touches).

    `border_ring_ft` / `border_min_expose`: the local surround test for the
    border/inner label (see `_classify_border_inner`). A unit is BORDER if more
    than `border_min_expose` of its `border_ring_ft`-wide neighbourhood is not
    covered by other served units (its edge faces unserved space), else INNER.

    `ignore_pidx` (optional): positional indices of pipes to exclude from the
    foreign set — the small dangling networks (see graph_builder.small_dangling_pidx).
    A parcel crossed only by an ignored stub is not contested.

    Added columns (DBF-safe names): cp_din (ft to nearest in-trace pipe),
    cp_dout (ft to nearest foreign pipe, NaN if none within radius), cp_fpipe
    (that pipe's FACILITYID), cp_fpidx (that pipe's positional index, -1 if none
    — a stable key when FACILITYID is null/duplicated), cp_cross (1 if a foreign
    pipe intersects the unit), cp_pos ("border" / "inner" — position in the
    served blob), cp_excl (1 = auto-exclude: a border unit no in-trace pipe
    touches that a foreign pipe crosses), cp_flag ("" / "warning" / "review").
    """
    out = served.copy()
    if out.empty:
        for c, v in (("cp_din", np.nan), ("cp_dout", np.nan), ("cp_fpipe", ""),
                     ("cp_fpidx", -1), ("cp_cross", 0), ("cp_pos", ""),
                     ("cp_excl", 0), ("cp_flag", "")):
            out[c] = v
        return out

    trace = set(pidx_list)
    ignore = set(ignore_pidx or ())
    in_positions = sorted(trace)
    # Foreign = any main not in this trace, EXCEPT pipes flagged to ignore (small
    # dangling networks — a 1-3 pipe stub crossing a parcel is noise, not a rival
    # network). Dropping them here removes them from both cp_dout and cp_cross,
    # since crossed is computed over foreign_positions below.
    foreign_positions = [i for i in range(len(pipes))
                         if i not in trace and i not in ignore]
    geoms = out[["geometry"]]

    def _nearest(positions, max_distance=None):
        """Per-unit nearest distance, FACILITYID, and positional pidx to the
        pipes at `positions` (positional indices into `pipes`).

        `_pidx` carries the true positional index — sjoin returns the sliced
        frame's own index, which need not be positional, so we set it explicitly
        from `positions` (the exact list passed to iloc).
        """
        r = pipes.iloc[positions][["FACILITYID", "geometry"]].copy()
        r["_pidx"] = positions
        j = gpd.sjoin_nearest(geoms, r, how="left", max_distance=max_distance,
                              distance_col="_d")
        j = j[~j.index.duplicated(keep="first")]   # exact-tie: keep first match
        return (j["_d"], j["FACILITYID"].fillna(""),
                j["_pidx"].fillna(-1).astype(int))

    out["cp_din"], _, _ = _nearest(in_positions)
    # A hair over the radius so a unit exactly at the boundary isn't dropped by
    # float noise; anything genuinely beyond stays NaN (no contest possible).
    out["cp_dout"], out["cp_fpipe"], out["cp_fpidx"] = _nearest(
        foreign_positions, max_distance=selection_radius_ft * (1 + 1e-9))

    crossed = gpd.sjoin(geoms, pipes.iloc[foreign_positions][["geometry"]],
                        how="inner", predicate="intersects").index.unique()
    out["cp_cross"] = out.index.isin(crossed).astype(int)

    # Border vs inner position by a LOCAL SURROUND test (see
    # _classify_border_inner): a unit is INNER only if it is ringed by other
    # served units on essentially all sides, BORDER if part of its `border_ring_ft`
    # neighbourhood pokes into unserved space. This must NOT be judged against the
    # morph-closed footprint — the close radius (e.g. 150 ft) dilates the output
    # boundary that far past the real served edge, so a whole ring of genuine edge
    # units falls "inside" it and reads as inner (bug caught 2026-07-06 on
    # 140112#0 / 106791#0). The distinction is load-bearing: a foreign pipe
    # crossing a BORDER unit is a neighbouring-network claim (split/exclude); the
    # same on a truly-surrounded INNER unit is a connectivity gap to fix, not a
    # unit to trim.
    out["cp_pos"] = _classify_border_inner(
        out.geometry, border_ring_ft, border_min_expose)

    # Auto-exclude (Grace's rule, 2026-07-06): a BORDER unit that no in-trace
    # pipe intersects (cp_din > 0, strict — no tolerance) but a foreign pipe
    # crosses (cp_cross == 1) belongs to the neighbouring main, not this trace.
    # Flag-only elsewhere; a consuming pass drops cp_excl == 1 before the
    # boundary is built.
    out["cp_excl"] = ((out["cp_pos"] == "border")
                      & (out["cp_din"] > 0)
                      & (out["cp_cross"] == 1)).astype(int)

    # Low-coverage border gate (Grace's rule, 2026-07-08): a BORDER unit pulled
    # in by a nick — where the in-trace selection buffer barely clips one corner
    # — e.g. a 386-ac park parcel (143319) clipped 0.011 ac (0.003%) rides the
    # whole parcel in. A nick is small in BOTH senses, so both must hold to
    # exclude: fraction < border_min_cover_frac AND absolute overlap <
    # border_min_cover_area_ft2. The absolute floor is what stops the fraction
    # test from wrongly dropping a large RURAL parcel legitimately edged by a
    # pipe (26532: 38-ac parcels at ~1.3% frac but ~0.5 ac real contact) — those
    # clear the area floor and stay. BORDER-only so an interior parcel a pipe
    # merely skirts is never dropped. cp_cover (fraction) + cp_covar (overlap
    # ft²) recorded for review; cp_excl absorbs failures so the existing
    # consuming pass drops them with the rest.
    out["cp_cover"] = 1.0
    out["cp_covar"] = np.nan
    if border_min_cover_frac > 0 and in_positions:
        buf = pipes.iloc[in_positions].geometry.buffer(
            selection_radius_ft).union_all()
        border = out["cp_pos"] == "border"
        areas = out.geometry.area
        overlap = out.geometry.intersection(buf).area
        out.loc[border, "cp_cover"] = (overlap / areas.where(areas > 0, 1.0))[border]
        out.loc[border, "cp_covar"] = overlap[border]
        lowcov = (border
                  & (out["cp_cover"] < border_min_cover_frac)
                  & (out["cp_covar"] < border_min_cover_area_ft2))
        out.loc[lowcov, "cp_excl"] = 1

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


def _near_pipes_by_side(pipes, unit, radius_ft, trace, ignore):
    """Positional indices of pipes within `radius_ft` of `unit`, split into
    (in_trace, foreign) — foreign excludes the trace and the ignored dangling
    stubs. Shared by the split and buffer-assignment passes."""
    d = pipes.geometry.distance(unit)
    near = set(np.where(d.values <= radius_ft)[0])
    in_pos = [i for i in near if i in trace]
    for_pos = [i for i in near if i not in trace and i not in ignore]
    return in_pos, for_pos


def _densify_pipe_points(pipes, positions, step_ft):
    """Points sampled every `step_ft` along each pipe at `positions`."""
    pts = []
    for i in positions:
        g = pipes.iloc[i].geometry
        if g is None or g.is_empty:
            continue
        n = max(int(g.length // step_ft) + 1, 2)
        pts += [g.interpolate(t) for t in np.linspace(0, g.length, n)]
    return pts


def _equidistant_keep(unit, pipes, in_pos, for_pos, step_ft):
    """The sub-geometry of `unit` closer to an in-trace pipe than to any foreign
    pipe, via a Voronoi partition of densified pipe points. Returns the kept
    geometry (possibly empty) — the equidistant line between the two pipe sets is
    the cut."""
    from scipy.spatial import cKDTree
    from shapely import voronoi_polygons
    from shapely.geometry import MultiPoint
    in_pts = _densify_pipe_points(pipes, in_pos, step_ft)
    for_pts = _densify_pipe_points(pipes, for_pos, step_ft)
    if not in_pts or not for_pts:
        # A pipe set sampled to no points (all geometries empty/None) — can't
        # partition, so leave the unit whole rather than silently dropping it.
        return unit
    seeds = in_pts + for_pts
    labels = np.array([0] * len(in_pts) + [1] * len(for_pts))
    tree = cKDTree(np.array([(pt.x, pt.y) for pt in seeds]))
    keep = []
    for cell in voronoi_polygons(MultiPoint(seeds), extend_to=unit.envelope).geoms:
        clip = cell.intersection(unit)
        if clip.is_empty:
            continue
        rp = cell.representative_point()
        _, idx = tree.query([rp.x, rp.y])
        if labels[idx] == 0:
            keep.append(clip)
    return unary_union(keep) if keep else Polygon()


def split_border_contested(served_ann: gpd.GeoDataFrame,
                           pipes: gpd.GeoDataFrame,
                           pidx_list,
                           selection_radius_ft: float,
                           cfg: dict,
                           ignore_pidx=None) -> gpd.GeoDataFrame:
    """
    Equidistant-split the border units that BOTH an in-trace and a foreign pipe
    cross, keeping only the portion closer to the in-trace network.

    Split target = `cp_pos == "border"` AND `cp_cross == 1` AND `cp_din == 0`
    (an in-trace pipe runs through the unit AND a foreign pipe crosses it — Grace's
    "intersected by foreign AND trace pipes"). For each, the unit geometry is
    replaced by the sub-area nearer an in-trace pipe than any foreign pipe (the
    equidistant Voronoi cut); a unit whose kept piece is empty is dropped.

    NOT split (pass through whole): inner units (decisively served / fragment
    cases), and border units with `cp_din > 0` — a foreign pipe crosses but no
    in-trace pipe does, which is the full-exclude case (`cp_excl`), handled before
    this by dropping `cp_excl == 1`. Keeping the two mechanisms mutually exclusive
    preserves both of Grace's validated rulings (196966 fully excluded, 137546
    part 2 split 46/54).

    The foreign set matches the competing check: any pipe not in the trace and not
    in `ignore_pidx` (the small dangling stubs the contest ignores). Config:
    `split_near_radius_ft` (pipes within this of the unit are considered, default
    100) and `split_densify_step_ft` (point spacing along pipes, default 3).

    Adds `cp_keep` (fraction of the original unit area retained; 1.0 for units not
    split). Returns the adjusted served set (fewer rows if any split to empty).
    """
    params = cfg["parameters"]
    if not params.get("split_border_contested", True) or served_ann.empty:
        served_ann = served_ann.copy()
        served_ann["cp_keep"] = 1.0
        return served_ann

    near_ft = params.get("split_near_radius_ft", 100.0)
    step_ft = params.get("split_densify_step_ft", 3.0)
    trace = set(pidx_list)
    ignore = set(ignore_pidx or ())

    target = ((served_ann["cp_pos"] == "border")
              & (served_ann["cp_cross"] == 1)
              & (served_ann["cp_din"] == 0))
    out = served_ann.copy()
    out["cp_keep"] = 1.0
    drop_idx = []
    for idx in out.index[target]:
        unit = out.at[idx, "geometry"]
        in_pos, for_pos = _near_pipes_by_side(pipes, unit, near_ft, trace, ignore)
        if not in_pos or not for_pos:
            continue                      # can't split — leave whole
        kept = _equidistant_keep(unit, pipes, in_pos, for_pos, step_ft)
        if kept.is_empty or kept.area <= 0:
            drop_idx.append(idx)
            continue
        out.at[idx, "geometry"] = kept
        out.at[idx, "cp_keep"] = kept.area / unit.area
    if drop_idx:
        out = out.drop(index=drop_idx)
    return out


def assign_remaining_by_buffer(served_ann: gpd.GeoDataFrame,
                               pipes: gpd.GeoDataFrame,
                               pidx_list,
                               cfg: dict,
                               ignore_pidx=None) -> gpd.GeoDataFrame:
    """
    Resolve the still-open contested units by buffered-pipe area (Grace's rule,
    2026-07-06): buffer each competing pipe and assign the unit to the pipe whose
    buffer covers the most of it — in-trace pipe wins → keep, foreign pipe wins →
    exclude (drop the unit).

    Applies only to units still flagged contested and not already resolved by the
    split/exclude passes (`cp_flag != ""` and `cp_keep == 1.0` — a split unit has
    cp_keep < 1). The winner is decided on AGGREGATE buffer area: area(unit ∩
    union(in-trace pipe buffers)) vs area(unit ∩ union(foreign pipe buffers)),
    ties → keep. Aggregating (not a single best pipe) so a unit fed by several
    in-trace mains isn't lost to one foreign pipe. Foreign pipes in `ignore_pidx`
    (dangling stubs) don't compete.

    Config `competing_assign_buffer_ft` (default = `selection_radius_ft`) is the
    buffer radius; candidate pipes are those within it of the unit. Adds `cp_asgn`
    ("keep"/"exclude"/"" for units not evaluated) — LABEL ONLY, no rows dropped;
    the caller drops `cp_asgn == "exclude"` before building the boundary (keeps
    the counts easy and the CSV complete).
    """
    params = cfg["parameters"]
    out = served_ann.copy()
    out["cp_asgn"] = ""
    if not params.get("competing_assign_by_buffer", True) or out.empty:
        return out

    buf_ft = params.get("competing_assign_buffer_ft",
                        params.get("selection_radius_ft", 50.0))
    trace = set(pidx_list)
    ignore = set(ignore_pidx or ())
    keep_col = out["cp_keep"] if "cp_keep" in out.columns else 1.0
    mask = (out["cp_flag"] != "") & (keep_col == 1.0)

    def _agg_area(unit, positions):
        bufs = [pipes.iloc[i].geometry.buffer(buf_ft) for i in positions
                if pipes.iloc[i].geometry is not None
                and not pipes.iloc[i].geometry.is_empty]
        if not bufs:
            return 0.0
        return unit.intersection(unary_union(bufs)).area

    # Candidate pipes are limited to within buf_ft of the unit — a pipe farther
    # than the buffer radius can't overlap the unit anyway. NOTE: this ties the
    # competing set to buf_ft; if buf_ft is ever set below the selection radius,
    # a pipe that selected the unit but sits beyond buf_ft won't compete.
    for idx in out.index[mask]:
        unit = out.at[idx, "geometry"]
        in_pos, for_pos = _near_pipes_by_side(pipes, unit, buf_ft, trace, ignore)
        a_in = _agg_area(unit, in_pos)
        a_for = _agg_area(unit, for_pos)
        out.at[idx, "cp_asgn"] = "exclude" if a_for > a_in else "keep"
    return out
