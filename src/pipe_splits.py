"""
Midspan-junction pipe splitting (QC round 1 follow-up, 2026-07-03).

The network's newest construction contains T-junctions digitized without splitting
the receiving main: a lateral's endpoint (usually marked by a manhole) lands on
another pipe's *interior*, so endpoint-to-endpoint snapping can never connect
them and the lateral's subnetwork traces as a separate component. The 62112 /
34720 / MH 58048 case (Tract 17.09) is the type specimen; fragments
component_140 / component_141 (18.06, Tract 14) are the same defect.

This module detects those junctions and splits the receiving pipe at each one,
in memory at load time — the city's source layer is never modified (same
reproducibility contract as the manual-snap pass; the source-data fix stays on
the GIS-maintainer list via the `midspan_junction` QA flag).

Split trigger — union of two rules:
  endpoint  another pipe's endpoint sits on this pipe's interior
            (within `interior_tol_ft` of the line, farther than
            `endpoint_exclusion_ft` from both of this pipe's own endpoints)
  manhole   a manhole point sits on this pipe's interior under the same
            distance tests (Grace's stated rule, 2026-07-03)

Both rules mark real junctions: an endpoint *terminating* on a main is a tee
regardless of whether the manhole point exists (a crossing pipe would pass
through, not terminate), and a mid-pipe manhole marks a structure the network
model should have a node at even when nothing ties in yet. The
`endpoint_exclusion_ft` guard keeps near-endpoint cases out — those belong to
the existing snap/snap_gap machinery, and cutting a few feet from a pipe end
would create sliver segments.

Splitting: the receiving pipe's geometry is cut at each junction (point
projected onto the line). The first segment replaces the original row —
existing `pidx` references stay valid — and the remaining segments are
appended as new rows with all attributes copied. Each segment keeps the parent
line's orientation, so the geometry-first direction rule (start = upstream)
is preserved.

Welding: the cut point is exactly on the line, but a tying endpoint can sit up
to `interior_tol_ft` away — beyond the 1 ft pass-1 snap, which would leave the
lateral disconnected despite the split. So after cutting, every pipe endpoint
within `interior_tol_ft` of a cut point is moved onto it exactly, guaranteeing
the pass-1 merge. (Attributes on appended segments are the parent's verbatim —
inverts/slope describe the full parent run, so per-segment attribute
cross-checks can repeat the parent's flag; acceptable, the fields are
cross-checks only under the geometry-first rule.)
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, LineString
from shapely.ops import substring

SPLIT_LOG_COLUMNS = ["parent_pidx", "facilityid", "x", "y", "dist_along",
                     "trigger", "sources", "new_pidx"]


def _fid_str(val) -> str:
    """FACILITYID as a display string ('?' if null)."""
    return str(val) if pd.notna(val) else "?"


@dataclass
class SplitRecord:
    """One junction where a pipe gets cut."""
    pidx: int              # positional index of the pipe being split
    facilityid: str        # its FACILITYID ("?" if null)
    dist_along: float      # cut position, ft along the line from its start
    x: float               # cut-point coordinates (on the line)
    y: float
    triggers: set = field(default_factory=set)   # {"endpoint", "manhole"}
    sources: list = field(default_factory=list)  # what tied in (pipe FID / MH FID)


def _endpoints(geom):
    coords = list(geom.coords)
    return Point(coords[0]), Point(coords[-1])


def find_midspan_junctions(pipes: gpd.GeoDataFrame,
                           manholes: gpd.GeoDataFrame | None,
                           interior_tol_ft: float,
                           endpoint_exclusion_ft: float) -> list[SplitRecord]:
    """
    Locate every midspan junction in the pipe layer.

    A candidate point (another pipe's endpoint, or a manhole) marks a junction
    on pipe j when it is within `interior_tol_ft` of j's line but farther than
    `endpoint_exclusion_ft` from both of j's endpoints. Junction points landing
    within `interior_tol_ft` of each other along the same pipe are merged into
    one SplitRecord (a tee usually fires both the endpoint and manhole rules).

    Returns records grouped per (pipe, location), unsorted.
    """
    sidx = pipes.sindex
    # (pidx, dist_along) -> SplitRecord, merged by proximity along the line
    found: dict[int, list[SplitRecord]] = {}

    def _register(j: int, pt: Point, trigger: str, source: str):
        geom = pipes.geometry.iloc[j]
        if not isinstance(geom, LineString):
            return  # multipart/degenerate geometry — never observed; skip safely
        if pt.distance(geom) >= interior_tol_ft:
            return
        s, e = _endpoints(geom)
        if pt.distance(s) <= endpoint_exclusion_ft or pt.distance(e) <= endpoint_exclusion_ft:
            return  # near-endpoint: existing snap / snap_gap machinery owns it
        d = float(geom.project(pt))
        for rec in found.setdefault(j, []):
            if abs(rec.dist_along - d) < interior_tol_ft:
                rec.triggers.add(trigger)
                rec.sources.append(source)
                return
        cut = geom.interpolate(d)
        found[j].append(SplitRecord(
            pidx=j,
            facilityid=_fid_str(pipes.iloc[j].get("FACILITYID")),
            dist_along=d, x=float(cut.x), y=float(cut.y),
            triggers={trigger}, sources=[source],
        ))

    # Rule 1 — endpoint on interior.
    for i, geom in enumerate(pipes.geometry):
        if geom is None or geom.is_empty or not isinstance(geom, LineString):
            continue
        ifid = _fid_str(pipes.iloc[i].get("FACILITYID"))
        for pt in _endpoints(geom):
            for j in sidx.query(pt.buffer(interior_tol_ft), predicate="intersects"):
                if j != i:
                    _register(int(j), pt, "endpoint", f"pipe {ifid}")

    # Rule 2 — manhole on interior.
    if manholes is not None:
        for _, row in manholes.iterrows():
            pt = row.geometry
            if pt is None or pt.is_empty:
                continue
            mfid = _fid_str(row.get("FACILITYID"))
            for j in sidx.query(pt.buffer(interior_tol_ft), predicate="intersects"):
                _register(int(j), pt, "manhole", f"MH {mfid}")

    return [rec for recs in found.values() for rec in recs]


def split_pipes_at_junctions(pipes: gpd.GeoDataFrame,
                             junctions: list[SplitRecord],
                             weld_tol_ft: float = 2.0
                             ) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """
    Cut each recorded pipe at its junction point(s).

    Returns (pipes_out, split_log). pipes_out preserves positional indexing:
    row `pidx` keeps the first (most-upstream) segment of a split pipe, and the
    remaining segments are appended at the end in deterministic order, with all
    attribute columns copied from the parent. The label index is reset so
    `iloc` == `loc` == pidx everywhere downstream.

    After cutting, tying endpoints are welded: any pipe endpoint within
    `weld_tol_ft` of a cut point is moved exactly onto it, so the pass-1 snap
    (1 ft) always merges the lateral with the new junction node even when the
    endpoint sat 1-2 ft off the line. Pass the same tolerance used to find the
    junctions (`interior_tol_ft`).

    split_log columns: SPLIT_LOG_COLUMNS — one row per cut, with trigger
    ("endpoint" / "manhole" / "endpoint+manhole"), sources, and new_pidx
    (the appended segment's positional index).
    """
    if not junctions:
        return pipes.reset_index(drop=True), pd.DataFrame(columns=SPLIT_LOG_COLUMNS)

    out = pipes.reset_index(drop=True).copy()
    by_pipe: dict[int, list[SplitRecord]] = {}
    for rec in junctions:
        by_pipe.setdefault(rec.pidx, []).append(rec)

    appended = []
    log_rows = []
    next_pidx = len(out)
    for pidx in sorted(by_pipe):
        geom = out.geometry.iloc[pidx]
        cuts = sorted(by_pipe[pidx], key=lambda r: r.dist_along)
        breaks = [0.0] + [r.dist_along for r in cuts] + [geom.length]
        segments = [substring(geom, breaks[k], breaks[k + 1])
                    for k in range(len(breaks) - 1)]
        # First segment replaces the parent row; the rest are appended.
        out.at[pidx, out.geometry.name] = segments[0]
        for seg, rec in zip(segments[1:], cuts):
            row = out.iloc[pidx].copy()
            row[out.geometry.name] = seg
            appended.append(row)
            log_rows.append({
                "parent_pidx": pidx,
                "facilityid": rec.facilityid,
                "x": rec.x, "y": rec.y,
                "dist_along": round(rec.dist_along, 2),
                "trigger": "+".join(sorted(rec.triggers)),
                "sources": "; ".join(rec.sources),
                "new_pidx": next_pidx,
            })
            next_pidx += 1

    out = pd.concat([out, gpd.GeoDataFrame(appended, crs=pipes.crs)],
                    ignore_index=True)
    out = gpd.GeoDataFrame(out, geometry=pipes.geometry.name, crs=pipes.crs)

    # Weld pass: move every tying endpoint within weld_tol_ft of a cut point
    # exactly onto it, so the pass-1 snap merges lateral and junction even when
    # the endpoint sat beyond the 1 ft snap tolerance. Coordinate-based on the
    # final frame, so it is immune to the tying pipe itself having been split
    # (its endpoints may now live on appended rows).
    sidx = out.sindex
    for rec in log_rows:
        cut = Point(rec["x"], rec["y"])
        for j in sidx.query(cut.buffer(weld_tol_ft), predicate="intersects"):
            geom = out.geometry.iloc[j]
            if not isinstance(geom, LineString):
                continue
            coords = list(geom.coords)
            moved = False
            if 0 < Point(coords[0]).distance(cut) <= weld_tol_ft:
                coords[0] = (rec["x"], rec["y"])
                moved = True
            if 0 < Point(coords[-1]).distance(cut) <= weld_tol_ft:
                coords[-1] = (rec["x"], rec["y"])
                moved = True
            if moved:
                out.at[j, out.geometry.name] = LineString(coords)

    return out, pd.DataFrame(log_rows, columns=SPLIT_LOG_COLUMNS)


def apply_midspan_splits(pipes: gpd.GeoDataFrame,
                         cfg: dict,
                         manholes: gpd.GeoDataFrame | None = None
                         ) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """
    Config-gated entry point: detect and apply midspan-junction splits.

    Reads `parameters.split_midspan_junctions` (default True); when disabled,
    returns the pipes unchanged with an empty log. Loads the manhole layer from
    config when the caller doesn't supply one (network_qa already has it;
    load_graph_from_config doesn't).
    """
    params = cfg["parameters"]
    if not params.get("split_midspan_junctions", True):
        return (pipes.reset_index(drop=True),
                pd.DataFrame(columns=SPLIT_LOG_COLUMNS))

    if manholes is None:
        mh_path = cfg["inputs"].get("manholes_shapefile")
        if mh_path:
            manholes = gpd.read_file(mh_path).to_crs(params["crs"])

    interior_tol = params.get("midspan_interior_tol_ft", 2.0)
    junctions = find_midspan_junctions(
        pipes,
        manholes,
        interior_tol_ft=interior_tol,
        endpoint_exclusion_ft=params.get("midspan_endpoint_exclusion_ft", 10.0),
    )
    return split_pipes_at_junctions(pipes, junctions, weld_tol_ft=interior_tol)
