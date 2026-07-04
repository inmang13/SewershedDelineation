"""
Human-directed pipe edits from the QA review file (flip / delete / extend).

The QA review feedback loop (qa_review.py) already carries `snap` decisions that
merge nodes. Round-2 review surfaced three more kinds of source-geometry fix a
reviewer can only describe, not express as a node merge:

  flip     the pipe is digitized backwards — reverse its geometry so the
           geometry-first direction rule (start = upstream) points the right way.
  delete   the pipe should not be in the network (a stray/duplicate main, an
           abandoned stub) — drop it from the traced topology.
  extend   the pipe stops short of a manhole it should reach — move its nearer
           endpoint onto that manhole so the network connects there.

Like snaps and midspan splits, these are applied in memory at load time in every
shared load path, so QA, traversal, and delineation share one topology. The
city's source layer is never modified; each edit emits a `manual_edit` QA flag
so the source-data fix stays on the GIS-maintainer list.

pidx stability: edits mutate geometry in place at the pipe's existing row (a
`delete` empties the geometry rather than dropping the row), so positional pipe
indices — the graph edge key — stay valid. `snap_endpoints` skips empty
geometry, so an emptied pipe contributes no edge while every other pipe keeps
its pidx.

Locating the target pipe: FACILITYID first, then nearest-to-(x,y) among the
matches (or among all pipes when the id is null/unmatched). x/y disambiguates
duplicate/again-null ids and guards against editing the wrong pipe; a match
beyond `locate_tol_ft` raises rather than silently editing a stranger.

Order vs midspan splits: edits run AFTER splits on the combined frame and match
by FACILITYID + x/y, so an edit locates the single split segment nearest its
locator. (Editing a pipe that was itself split at a midspan junction is
untested; none of the round-2 edits touch a split pipe.)
"""

import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, LineString

EDIT_DECISIONS = {"flip", "delete", "extend"}
EDIT_LOG_COLUMNS = ["decision", "facilityid", "pidx", "x", "y", "target", "detail"]


def _no_edits(pipes: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """The (unchanged pipes, empty log) result when there is nothing to apply."""
    return pipes.reset_index(drop=True), pd.DataFrame(columns=EDIT_LOG_COLUMNS)


def _locate_pipe(pipes: gpd.GeoDataFrame, edit: dict, locate_tol_ft: float) -> int:
    """Positional index of the pipe an edit targets, or raise if unresolvable."""
    fid = str(edit.get("pipe_id", "")).strip()
    x, y = edit.get("x"), edit.get("y")

    matches = []
    if fid:
        fids = pipes["FACILITYID"].astype(str)
        matches = [i for i in range(len(pipes)) if fids.iloc[i] == fid]

    if len(matches) == 1:
        return matches[0]

    # Zero or multiple FACILITYID matches: fall back to nearest-to-(x, y).
    if x is None or y is None:
        raise ValueError(
            f"pipe edit ({edit['decision']}): FACILITYID {fid!r} matched "
            f"{len(matches)} pipes and no x/y locator was given — cannot "
            "resolve which pipe to edit"
        )
    pt = Point(x, y)
    pool = matches if matches else range(len(pipes))
    best, best_dist = None, locate_tol_ft
    for i in pool:
        g = pipes.geometry.iloc[i]
        if g is None or g.is_empty:
            continue
        d = g.distance(pt)
        if d <= best_dist:
            best, best_dist = i, d
    if best is None:
        raise ValueError(
            f"pipe edit ({edit['decision']}): no pipe within {locate_tol_ft} ft "
            f"of ({x}, {y}) for FACILITYID {fid!r}"
        )
    return best


def _resolve_target(edit: dict, manholes: gpd.GeoDataFrame | None) -> Point:
    """
    Resolve an `extend` target to a point. `target` is either "x,y" (two
    numbers) or a manhole FACILITYID looked up in the manholes layer.
    """
    raw = str(edit.get("target", "")).strip()
    if not raw:
        raise ValueError("extend edit: `target` is required (manhole FACILITYID "
                         "or 'x,y')")
    if "," in raw:
        try:
            xs, ys = raw.split(",", 1)
            return Point(float(xs), float(ys))
        except ValueError:
            raise ValueError(f"extend edit: target {raw!r} is not a valid 'x,y'") from None
    if manholes is None:
        raise ValueError(f"extend edit: target {raw!r} is a manhole id but no "
                         "manholes layer was provided")
    mfids = manholes["FACILITYID"].astype(str)
    hit = manholes[mfids == raw]
    if hit.empty:
        raise ValueError(f"extend edit: manhole FACILITYID {raw!r} not found")
    return hit.iloc[0].geometry


def apply_edits(pipes: gpd.GeoDataFrame,
                edits: list[dict],
                manholes: gpd.GeoDataFrame | None = None,
                locate_tol_ft: float = 50.0
                ) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """
    Apply flip / delete / extend edits to a copy of `pipes`.

    Returns (pipes_out, edit_log). pipes_out preserves positional indexing —
    edits mutate the pipe's own row (delete empties its geometry). edit_log has
    one row per applied edit (EDIT_LOG_COLUMNS).
    """
    if not edits:
        return _no_edits(pipes)

    out = pipes.reset_index(drop=True).copy()
    gcol = out.geometry.name
    log = []
    for e in edits:
        pidx = _locate_pipe(out, e, locate_tol_ft)
        dec = e["decision"]
        geom = out.geometry.iloc[pidx]
        fid = str(out.iloc[pidx].get("FACILITYID"))
        # A stable point for the manual_edit QA flag, captured before any
        # geometry change (delete empties the line, so it must be taken now).
        rep = (geom.interpolate(0.5, normalized=True)
               if geom is not None and not geom.is_empty else None)

        if dec == "flip":
            if geom is None or geom.is_empty:
                raise ValueError(f"flip edit: pipe {fid} (pidx {pidx}) has no geometry")
            out.at[pidx, gcol] = LineString(list(geom.coords)[::-1])
            # Geometry only — attributes (UPSTREAMIN/DOWNSTREAM/SLOPE/FROMMH/TOMH)
            # are left as source data. Geometry-first: geometry drives direction;
            # inverts are cross-checks the maintainer reconciles at source (the
            # manual_edit flag records the pipe for that). We deliberately do NOT
            # swap the invert fields: the check reads them by column name, so a
            # geometry-only flip never fabricates an invert_conflict, whereas
            # swapping them can invent a physically-implausible one.
            detail = "geometry reversed (was digitized backwards)"

        elif dec == "delete":
            out.at[pidx, gcol] = LineString()  # empty -> no edge; pidx preserved
            detail = "removed from traced network (geometry emptied)"

        elif dec == "extend":
            if geom is None or geom.is_empty:
                raise ValueError(f"extend edit: pipe {fid} (pidx {pidx}) has no geometry")
            target = _resolve_target(e, manholes)
            coords = list(geom.coords)
            # Move whichever endpoint is nearer the target onto it; the other
            # endpoint and the pipe's orientation are left unchanged.
            if Point(coords[0]).distance(target) <= Point(coords[-1]).distance(target):
                coords[0] = (target.x, target.y)
                which = "start"
            else:
                coords[-1] = (target.x, target.y)
                which = "end"
            out.at[pidx, gcol] = LineString(coords)
            detail = f"{which} endpoint extended to {e.get('target')!r}"

        else:  # unreachable — EDIT_DECISIONS is validated in qa_review
            raise ValueError(f"unknown pipe-edit decision {dec!r}")

        log.append({
            "decision": dec,
            "facilityid": fid,
            "pidx": pidx,
            "x": rep.x if rep is not None else e.get("x"),
            "y": rep.y if rep is not None else e.get("y"),
            "target": e.get("target", ""),
            "detail": detail,
        })

    out = gpd.GeoDataFrame(out, geometry=gcol, crs=pipes.crs)
    return out, pd.DataFrame(log, columns=EDIT_LOG_COLUMNS)


def apply_pipe_edits(pipes: gpd.GeoDataFrame,
                     cfg: dict,
                     manholes: gpd.GeoDataFrame | None = None
                     ) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """
    Config-gated entry point: read flip/delete/extend rows from the QA review
    decisions file and apply them. Gated by `parameters.apply_pipe_edits`
    (default True); loads the manholes layer for extend targets when the caller
    doesn't supply one.
    """
    from qa_review import load_review_decisions, pipe_edits

    params = cfg["parameters"]
    if not params.get("apply_pipe_edits", True):
        return _no_edits(pipes)

    edits = pipe_edits(load_review_decisions(cfg["inputs"].get("qa_review_decisions")))
    if not edits:
        return _no_edits(pipes)

    if manholes is None and any(e["decision"] == "extend" for e in edits):
        mh_path = cfg["inputs"].get("manholes_shapefile")
        if mh_path:
            manholes = gpd.read_file(mh_path).to_crs(params["crs"])

    return apply_edits(pipes, edits, manholes=manholes,
                       locate_tol_ft=params.get("pipe_edit_locate_tol_ft", 50.0))
