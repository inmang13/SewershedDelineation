"""
Force-main integration, Phase 4 — review file and QC geometry.

Builds `QC/force_main_review.csv` (the human decision queue), settles rows
that no longer need asking, and writes the QC shapefile/GeoPackage for map
review. See `force_mains.py` for the phase overview and the shared
flag/verdict vocabulary.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, LineString
from scipy.spatial import cKDTree

from force_mains import (
    FLAG_JUNCTION, FLAG_NO_CONTACT, FLAG_AMBIGUOUS, FLAG_STATION_ADJACENT,
    VERDICT_DIRECTION_UNRESOLVED, MAX_PREFILLED_RADIUS_GAP_FT, _read_csv_rows,
)
from force_main_topology import ForceMainTopology


def review_rows(termini: pd.DataFrame, verdicts: pd.DataFrame,
                fm: gpd.GeoDataFrame, topo: ForceMainTopology,
                max_prefilled_gap_ft: float = MAX_PREFILLED_RADIUS_GAP_FT
                ) -> pd.DataFrame:
    """
    Build `QC/force_main_review.csv`.

    The first eight columns are exactly the `qa_review_decisions.csv` schema, so
    accepted rows can be moved into that file (or read from this one) and
    consumed by `qa_review.manual_snaps` unchanged. A row's `x`/`y` is the
    MIDPOINT of the proposed junction and `radius_ft` covers it with 25% margin,
    because pass 3 merges every cluster within `radius_ft` of the recorded point
    — half the gap plus margin is what actually reaches both sides.

    `radius_ft` is only pre-filled for gaps up to `max_prefilled_gap_ft`
    (`parameters.force_main_max_prefilled_radius_gap_ft`).
    Beyond that it ships blank on purpose: pass 3 merges EVERY cluster inside
    the radius, so a pre-filled 300 ft radius on a 500 ft candidate would
    silently swallow a whole neighbourhood of unrelated nodes if a reviewer
    accepted the row without reading it. A blank falls back to
    `qa_review.DEFAULT_SNAP_RADIUS_FT` (5 ft), which reaches nothing and raises
    — a loud failure instead of a quiet one. A reviewer who genuinely wants a
    wide merge types the number in.

    `decision` ships blank. Nothing is pre-accepted: the whole point of step 2
    is that a human approves each junction before topology changes.
    """
    verdict_by_comp = dict(zip(verdicts.comp_id, verdicts.verdict))
    fid_by_cluster = _facilityid_by_cluster(fm, topo)

    rows = []
    for r in termini.itertuples(index=False):
        gap = r.dist_ft
        spot = _plain_gravity_spot(r.in_degree, r.out_degree)
        if getattr(r, "station_adjacent", False):
            flag_type = FLAG_STATION_ADJACENT
            comment = (
                f"This end reads as a discharge {gap:.1f} ft away, but the "
                "force main here is a short stub sitting right next to a pump "
                "station. That pattern usually means this is still inside the "
                "station, not a real discharge into the gravity sewer - the "
                "gravity flow just happens to continue past the station's own "
                "outlet. Is there a real connection here, or is this part of "
                "the station itself? (Confirmed at East End and Geer St in the "
                "2026-08-02/03 review.)")
        elif r.contact == "none":
            flag_type = FLAG_NO_CONTACT
            comment = (
                f"This force main end is {gap:.0f} ft from the nearest gravity "
                "pipe end, too far to be a connection. Usually means a gravity "
                "main is missing from the data, or this pipe is drawn wrong.")
        elif verdict_by_comp.get(r.comp_id) in VERDICT_DIRECTION_UNRESOLVED:
            flag_type = FLAG_AMBIGUOUS
            if r.contact == "facility":
                # This specific end is already answered — a station facility
                # confirmed it. Asking "which end is the pump station?" here
                # is the wrong question and reads as broken (2026-08-03: Grace
                # flagged exactly this on the Snow Hill LS row). The system's
                # open problem is one of its OTHER termini.
                comment = (
                    f"This end is already confirmed as the pump station "
                    f"({getattr(r, 'facility_name', '') or 'matched facility'}). "
                    f"No action needed on this specific point. System "
                    f"{r.comp_id}'s open problem is elsewhere: "
                    f"{_plain_verdict(verdict_by_comp.get(r.comp_id))}")
            else:
                comment = (
                    f"System {r.comp_id}: "
                    f"{_plain_verdict(verdict_by_comp.get(r.comp_id))} "
                    f"This end is {gap:.0f} ft from a gravity pipe end where {spot}. "
                    "Which end of this system is the pump station?")
        else:
            flag_type = FLAG_JUNCTION
            reads = ("a pump station" if str(r.classification).startswith("wetwell")
                     else "where the force main empties into the gravity sewer")
            comment = (
                f"This force main end is {gap:.1f} ft from a gravity pipe end "
                f"where {spot}. That pattern means this is {reads}. "
                "Should the force main connect here?")

        rows.append({
            # --- qa_review_decisions.csv schema ---
            "flag_type": flag_type,
            "pipe_id":   fid_by_cluster.get(r.cluster, ("FM:?", ""))[0],
            "x":         round((r.x + r.gravity_x) / 2.0, 3),
            "y":         round((r.y + r.gravity_y) / 2.0, 3),
            "decision":  "",
            "radius_ft": (round(max(gap / 2.0 * 1.25, 1.0), 2)
                          if gap <= max_prefilled_gap_ft else ""),
            "target":    "",
            "comment":   comment,
            # --- diagnostics (ignored by qa_review.load_review_decisions) ---
            "fm_end":         fid_by_cluster.get(r.cluster, ("FM:?", ""))[1],
            "comp_id":        r.comp_id,
            "comp_verdict":   verdict_by_comp.get(r.comp_id, ""),
            "classification": r.classification,
            "contact":        r.contact,
            "dist_ft":        gap,
            "gravity_node":   r.gravity_node,
            "gravity_role":   r.gravity_role,
            "fm_x":           round(r.x, 3),
            "fm_y":           round(r.y, 3),
        })

    # A component that is one closed ring has no degree-1 node, so it produces no
    # terminus rows and would vanish from the review file entirely while still
    # appearing in the verdicts, the runner counts and fm_pipes. Emit one row per
    # such component so nothing in the network is silently unreviewable.
    for v in verdicts.itertuples(index=False):
        if v.n_termini == 0:
            rows.append({
                "flag_type": FLAG_AMBIGUOUS,
                "pipe_id":   _any_facilityid(fm, topo, v.comp_id),
                "x": "", "y": "", "decision": "", "radius_ft": "", "target": "",
                "fm_end": "",
                "comment": (f"component {v.comp_id} is a closed ring "
                            f"({v.n_pipes} pipes, {v.length_ft / 5280:.2f} mi) "
                            "with no free end — it attaches to gravity nowhere "
                            "and has no wet well or discharge to infer. Check "
                            "for a missing connecting segment."),
                "comp_id": v.comp_id, "comp_verdict": v.verdict,
                "classification": "ring", "contact": "none",
                "dist_ft": float("nan"), "gravity_node": "", "gravity_role": "",
                "fm_x": "", "fm_y": "",
            })

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    # Shortest gaps first within each flag type: the cheapest decisions to make,
    # and the ones most likely to be real.
    order = {FLAG_JUNCTION: 0, FLAG_AMBIGUOUS: 1, FLAG_NO_CONTACT: 2}
    return (out.assign(_o=out.flag_type.map(order))
               .sort_values(["_o", "dist_ft"])
               .drop(columns="_o")
               .reset_index(drop=True))


def settle_reviewed_rows(rows: pd.DataFrame, cfg: dict,
                         auto_accept_ft: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Take questions off the review list that no longer need asking.

    Two ways a row gets settled:

    `auto_accepted` — a junction candidate whose gap is at or below
    `auto_accept_ft`. The force-main end and the gravity node are the same point;
    there is no judgement to make. Only `fm_junction_candidate` rows qualify.
    A `fm_direction_ambiguous` row at 0 ft is NOT auto-accepted: the connection
    is obvious but which way flow runs through the system is the open question,
    and that is what the row is asking.

    `already_decided` — the row matches one Grace has previously ruled on in
    `inputs.force_main_decisions`. Matched on the force-main terminus
    coordinate (`fm_x`/`fm_y`) rather than on row order or component id, because
    component numbering shifts whenever the pipe set changes and row order is
    not stable at all. The coordinate is a physical location and does not move.

    Returns (still_open, settled). Nothing is deleted — settled rows are written
    out separately so an auto-accept is auditable rather than invisible.
    """
    empty = rows.iloc[0:0]
    if rows.empty:
        return rows, empty

    obvious = ((rows.flag_type == FLAG_JUNCTION)
               & (pd.to_numeric(rows.dist_ft, errors="coerce") <= auto_accept_ft))
    settled = rows[obvious].assign(settled_as="auto_accepted",
                                   settled_why=f"gap <= {auto_accept_ft} ft")

    # A facility already confirmed this specific end - there is no decision to
    # make on THIS row (Grace, 2026-08-05: "why is this entry even here???").
    # The system's real problem, if any, is on another terminus and gets its
    # own row; component_verdicts still reports the gap either way, so nothing
    # about the underlying issue goes unreported.
    # Any flag type, not just ambiguous: contact == "facility" means a
    # confirmed plant or station pinned this end, so there is nothing for a
    # reviewer to decide here regardless of which question the row was asking.
    facility_confirmed = rows.contact == "facility"
    settled = pd.concat([settled, rows[facility_confirmed].assign(
        settled_as="facility_confirmed",
        settled_why="this end is already matched to a confirmed facility; "
                    "no decision possible here")], ignore_index=True)

    open_rows = rows[~(obvious | facility_confirmed)]

    prior = _load_prior_decisions(cfg.get("inputs", {}).get("force_main_decisions"))
    if prior is not None and not prior.empty and not open_rows.empty:
        tree = cKDTree(prior[["fm_x", "fm_y"]].to_numpy())
        dist, j = tree.query(np.c_[open_rows.fm_x.astype(float),
                                   open_rows.fm_y.astype(float)])
        hit = dist <= 1.0          # same physical terminus, not a nearby one
        if hit.any():
            done = open_rows[hit].assign(
                settled_as="already_decided",
                settled_why=prior.note.to_numpy()[j[hit]])
            settled = pd.concat([settled, done], ignore_index=True)
            open_rows = open_rows[~hit]

    return open_rows.reset_index(drop=True), settled.reset_index(drop=True)


def _load_prior_decisions(path) -> pd.DataFrame | None:
    """
    Read a previously reviewed copy of the review file.

    Tolerates what a real review round does to it: cp1252 from Excel, and a
    duplicated `decision` header (pandas renames the second one `decision.1`,
    and a reviewer typing into the wrong one is easy to do). Any column whose
    name starts with `decision` counts, so a note in either lands.
    """
    if not path or not Path(path).exists():
        return None
    rows = _read_csv_rows(path)
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    if not {"fm_x", "fm_y"}.issubset(frame.columns):
        raise ValueError(
            f"{path}: needs fm_x and fm_y columns to match decisions to rows — "
            "this should be a filled-in copy of the generated review file.")

    note_cols = [c for c in frame.columns if c.lower().startswith("decision")]
    notes = (frame[note_cols].fillna("").astype(str)
             .apply(lambda r: " | ".join(v.strip() for v in r if v.strip()), axis=1))
    frame = frame.assign(note=notes)
    frame = frame[frame.note.str.strip() != ""]
    frame[["fm_x", "fm_y"]] = frame[["fm_x", "fm_y"]].apply(
        pd.to_numeric, errors="coerce")
    return frame.dropna(subset=["fm_x", "fm_y"])


def _plain_gravity_spot(in_degree: int, out_degree: int) -> str:
    """
    Describe a gravity pipe end in words rather than in degrees.

    The review file is read by a person deciding whether a connection is real,
    not by someone holding the graph vocabulary in their head. "in=1, out=1"
    told the reviewer nothing; "sewage flows through and keeps going downstream"
    is the same fact and is directly checkable against imagery.
    """
    if in_degree and out_degree:
        return "sewage flows through and keeps going downstream"
    if in_degree:
        return "sewage flows in and stops"
    if out_degree:
        return "sewage starts there and flows away, with nothing upstream feeding it"
    return "no gravity pipe flows either way"


def _plain_verdict(verdict: str) -> str:
    """Why a system could not be settled, in words the reviewer can act on."""
    return {
        "no_discharge":  "I couldn't find anywhere this system empties back "
                         "into the gravity sewer.",
        "no_wetwell":    "I couldn't find the pump station this system pumps from.",
        "multi_discharge": "two or more ends look like they empty into the "
                           "gravity sewer, so I can't tell which one is real.",

        "isolated":      "this system doesn't reach the gravity sewer anywhere.",
        "unconnected_only": "nothing in this system quite touches the gravity "
                            "sewer — every contact is a near miss.",
    }.get(verdict, f"unresolved ({verdict}).")


def _facilityid_by_cluster(fm: gpd.GeoDataFrame,
                           topo: ForceMainTopology) -> dict[int, tuple[str, str]]:
    """
    Map each endpoint cluster to (FACILITYID, which_end) for the review file.

    The id is prefixed `FM:` because FACILITYID is NOT unique across layers —
    218 ids in this layer also exist in the gravity mains. An unprefixed id in a
    review file would be genuinely ambiguous to both a human and to any lookup
    keyed on it.

    `which_end` is "start" or "end" of that main as digitized. It carries no
    flow meaning — direction is inferred, not read from geometry — but a
    reviewer opening the pipe in GIS needs to know which end of it is meant.
    """
    out = {}
    for pidx in range(len(fm)):
        fid = fm["FACILITYID"].iloc[pidx]
        fid = f"FM:{fid}" if pd.notna(fid) else "FM:?"
        out.setdefault(int(topo.cluster[2 * pidx]), (fid, "start"))
        out.setdefault(int(topo.cluster[2 * pidx + 1]), (fid, "end"))
    return out


def _any_facilityid(fm: gpd.GeoDataFrame, topo: ForceMainTopology,
                    comp_id: int) -> str:
    """One representative FACILITYID from a component, for ring rows with no terminus."""
    comp = topo.components[comp_id]
    for _, _, d in topo.graph.subgraph(comp).edges(data=True):
        fid = fm["FACILITYID"].iloc[d["pidx"]]
        return f"FM:{fid}" if pd.notna(fid) else "FM:?"
    return "FM:?"


# Shapefile column names are capped at 10 characters and silently truncated,
# which turns `classification` and `gravity_role` into collisions. Naming them
# explicitly keeps the layer readable in ArcGIS/QGIS instead of leaving a column
# called `classifi_1`.
SHP_FIELDS = {
    "pipe_id":        "FM_ID",
    "fm_end":         "FM_END",
    "flag_type":      "ASKING",
    "dist_ft":        "GAP_FT",
    "comp_id":        "SYSTEM",
    "comp_verdict":   "VERDICT",
    "classification": "READS_AS",
    "gravity_node":   "GRAV_NODE",
    "gravity_role":   "GRAV_ROLE",
    "comment":        "COMMENT",
}

# dBase text fields cap at 254 characters. Longer comments are truncated rather
# than allowed to raise at write time, three rows into a 63-row export.
SHP_TEXT_LIMIT = 254


def write_review_shapefile(path, crs, rows: pd.DataFrame) -> int:
    """
    Write the still-open review rows as a point shapefile, one point per row.

    Points, not the junction lines from `write_qc_gpkg`: every open row has a
    force-main terminus to stand on, but the `fm_no_gravity_contact` rows have
    no meaningful other end to draw a line to. One geometry type also keeps this
    to a single shapefile rather than a set of them.

    The point sits on the force-main end being asked about (`fm_x`/`fm_y`), so
    zooming to it puts the question under the cursor. Returns the feature count.
    """
    rows = rows[rows.fm_x.astype(str).str.strip() != ""].copy()
    if rows.empty:
        return 0

    out = rows.rename(columns=SHP_FIELDS)[list(SHP_FIELDS.values())].copy()
    out["COMMENT"] = out["COMMENT"].astype(str).str.slice(0, SHP_TEXT_LIMIT)

    gdf = gpd.GeoDataFrame(
        out,
        geometry=gpd.points_from_xy(pd.to_numeric(rows.fm_x),
                                    pd.to_numeric(rows.fm_y)),
        crs=crs)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path)
    return len(gdf)


def write_qc_gpkg(path, crs, fm: gpd.GeoDataFrame, topo: ForceMainTopology,
                  termini: pd.DataFrame, verdicts: pd.DataFrame) -> list[str]:
    """
    Write three review layers to a GeoPackage and return the layer names.

      fm_pipes     the filtered force mains, tagged with comp_id and verdict
      fm_termini   terminus points, coloured by classification/contact
      fm_junctions one line per proposed junction (terminus -> gravity node),
                   so the gap is visible at a glance in GIS

    `fm_junctions` is the layer to open first: its length IS the snap gap.
    """
    path = Path(path)
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    verdict_by_comp = dict(zip(verdicts.comp_id, verdicts.verdict))
    comp_of_pidx = {}
    for comp_id, comp in enumerate(topo.components):
        for _, _, d in topo.graph.subgraph(comp).edges(data=True):
            comp_of_pidx[d["pidx"]] = comp_id

    pipes = fm[["FACILITYID", "DIAMETER", "OWNER", "geometry"]].copy()
    pipes["comp_id"] = [comp_of_pidx.get(i, -1) for i in range(len(fm))]
    pipes["verdict"] = pipes["comp_id"].map(verdict_by_comp).fillna("")
    pipes.to_file(path, layer="fm_pipes", driver="GPKG")
    written = ["fm_pipes"]

    if not termini.empty:
        pts = gpd.GeoDataFrame(
            termini.drop(columns=["gravity_x", "gravity_y"]),
            geometry=[Point(xy) for xy in zip(termini.x, termini.y)], crs=crs)
        pts["verdict"] = pts["comp_id"].map(verdict_by_comp).fillna("")
        pts.to_file(path, layer="fm_termini", driver="GPKG")
        written.append("fm_termini")

        link = termini[termini.contact != "none"]
        if not link.empty:
            lines = gpd.GeoDataFrame(
                link[["comp_id", "dist_ft", "classification", "contact",
                      "gravity_node", "gravity_role"]].copy(),
                geometry=[LineString([(a, b), (c, d)]) for a, b, c, d in
                          zip(link.x, link.y, link.gravity_x, link.gravity_y)],
                crs=crs)
            lines.to_file(path, layer="fm_junctions", driver="GPKG")
            written.append("fm_junctions")

    return written
