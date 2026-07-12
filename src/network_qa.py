"""
Network QA for gravity main shapefiles — geometry-first, on the shared graph.

Geometry is the primary source of truth for pipe direction. Attribute fields
(FROMMH, TOMH, SLOPE, UPSTREAMIN, DOWNSTREAM) are used as cross-checks only.

All topology (node snapping, direction, connectivity, cycles) comes from
`graph_builder.build_graph()` — the same two-pass snap and MultiDiGraph the
traversal and delineation phases use. QA no longer maintains a private
re-snapped graph, so its flags describe exactly the network the tool traces.
Pipes are addressed by positional index (`pidx`, the graph edge key) because
FACILITYID is null on some pipes and not unique; FACILITYID is still reported
in flag text for the GIS maintainer.

Flag types:
  missing_direction        FROMMH/TOMH null — geometry still gives direction (warning)
  invert_conflict          Invert elevations imply uphill flow vs geometry direction;
                           geometry is treated as correct (warning)
  attribute_slope_error    SLOPE field negative but geometry direction is used (warning)
  snap_gap                 Near-miss end-node gap. Within the pass-2 repair radius it IS
                           auto-connected in the graph (warning — source geometry still
                           offset); beyond it, the pipes will not connect (review_required)
  directed_cycle           Small strongly-connected component — local flip (review_required)
  large_cycle              Large SCC — systematic direction error (review_required)
  disconnected_component   Small isolated fragment (<= disconnected_component_max_nodes
                           nodes); large separate components are distinct drainage
                           basins, not errors (warning)
  isolated_manhole         Manhole not connected to any pipe by ID or coordinate (warning)
"""

import numpy as np
import pandas as pd
import geopandas as gpd
import networkx as nx
from scipy.spatial import cKDTree
from shapely.geometry import Point

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from graph_builder import build_graph, invert_direction_conflicts, _num
from node_layer import snap_endpoints
from pipe_splits import apply_midspan_splits
from pipe_edits import apply_pipe_edits
from qa_review import load_review_decisions, manual_snaps, apply_review


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_qa(cfg: dict) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, list[dict]]:
    """
    Load gravity mains and manholes, run all QA checks on the shared graph.

    Returns:
        pipes_repaired  GeoDataFrame with QA_STATUS and QA_FLAGS columns added
        manholes        GeoDataFrame (used by pdf_maps for labeling)
        flags           list of flag dicts (each pipe flag carries `pidx` /
                        `member_pidx`; manhole flags carry neither)
    """
    params           = cfg["parameters"]
    snap_tol         = params["node_snap_tolerance_ft"]
    snap_gap_radius  = params.get("snap_gap_search_radius_ft", 10.0)
    mh_snap_ft       = params.get("manhole_node_snap_ft", 5.0)
    crs              = params["crs"]

    pipes    = gpd.read_file(cfg["inputs"]["gravity_main_shapefile"]).to_crs(crs)
    manholes = gpd.read_file(cfg["inputs"]["manholes_shapefile"]).to_crs(crs)
    # pidx is the positional index; make label index == position so .loc/.at
    # writes and graph pidx values address the same rows.
    pipes = pipes.reset_index(drop=True)

    # Midspan-junction splits — the same in-memory repair that
    # load_graph_from_config applies, so QA flags describe the topology the
    # tool actually traces. Split segments are appended rows (existing pidx
    # values stay valid); each junction is flagged for the GIS maintainer.
    pipes, split_log = apply_midspan_splits(pipes, cfg, manholes=manholes)
    # Human-directed edits (flip/delete/extend), after splits — same topology as
    # load_graph_from_config, so QA describes exactly what the tool traces.
    pipes, edit_log = apply_pipe_edits(pipes, cfg, manholes=manholes)

    pipes["QA_STATUS"] = "original"
    pipes["QA_FLAGS"]  = ""
    # Appended split segments are tool-made geometry, not source rows — label
    # them so the repaired shapefile / QC layers don't pass them off as data.
    if not split_log.empty:
        pipes.loc[split_log["new_pidx"].to_numpy(), "QA_STATUS"] = "split_segment"
    if not edit_log.empty:
        pipes.loc[edit_log["pidx"].to_numpy(), "QA_STATUS"] = "manual_edit"

    # Prior human review (QC decisions file): snap rows repair the graph below;
    # resolved/keep rows annotate the flags after the checks run.
    decisions = load_review_decisions(cfg["inputs"].get("qa_review_decisions"))

    # One topology for everything: same snap + graph as traversal/delineation.
    G = build_graph(pipes, snap_tol, snap_gap_radius,
                    manual_snaps=manual_snaps(decisions))

    flags = []
    flags += _check_midspan_junctions(split_log)
    flags += _check_manual_edits(edit_log, pipes)
    flags += _check_missing_direction(pipes)
    flags += _check_invert_conflict(G, pipes)
    flags += _check_negative_slope(pipes, {f["pidx"] for f in flags
                                           if f["flag_type"] == "invert_conflict"})
    flags += _check_snap_gaps(pipes, G, snap_tol, snap_gap_radius)
    flags += _check_directed_cycles(G, pipes,
                                    params.get("max_mappable_cycle_nodes", 10))
    flags += _check_disconnected_components(
        G, pipes, params.get("disconnected_component_max_nodes", 50))
    flags += _check_isolated_manholes(pipes, manholes, G, mh_snap_ft)

    _apply_flag_fields(pipes, flags)
    apply_review(flags, decisions,
                 match_radius_ft=params.get("review_match_radius_ft", 50.0))

    return pipes, manholes, flags


def _fid(row) -> str:
    """FACILITYID as a display string ('?' if null)."""
    v = row.get("FACILITYID")
    return str(v) if pd.notna(v) else "?"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_midspan_junctions(split_log) -> list[dict]:
    """
    T-junctions digitized without splitting the receiving main.

    These come from pipe_splits.apply_midspan_splits: a lateral endpoint and/or
    manhole sits on this pipe's interior, so the tool has split the pipe in
    memory and the network now traces through the junction. Severity is warning
    (auto-repaired, same convention as tier-1 snap_gap) — but the source layer
    still needs the pipe physically split at the junction, which only the GIS
    maintainer can do.
    """
    flags = []
    for _, r in split_log.iterrows():
        flags.append({
            "flag_type":  "midspan_junction",
            "severity":   "warning",
            "pipe_id":    str(r["facilityid"]),
            "pidx":       int(r["parent_pidx"]),
            "geometry":   Point(r["x"], r["y"]),
            "description": (
                f"Junction on this pipe's interior ({r['trigger']}: "
                f"{r['sources']}) — the receiving main was never split there. "
                "Auto-split in memory so the network traces through; split the "
                "pipe at this point in the source layer to fix permanently."
            ),
        })
    return flags


def _check_manual_edits(edit_log, pipes) -> list[dict]:
    """
    Human-directed flip/delete/extend edits applied from the QA review file.

    These come from pipe_edits.apply_pipe_edits: the reviewer decided the source
    geometry is wrong and the tool has applied the fix in memory. Severity is
    warning (the tool already traces the corrected topology), but the source
    layer still needs the same edit, so each is flagged for the GIS maintainer.
    """
    flags = []
    for _, r in edit_log.iterrows():
        flags.append({
            "flag_type":  "manual_edit",
            "severity":   "warning",
            "pipe_id":    str(r["facilityid"]),
            "pidx":       int(r["pidx"]),
            "geometry":   Point(r["x"], r["y"]),
            "description": (
                f"Reviewer-directed {r['decision']}: {r['detail']}. Applied in "
                "memory so the network traces correctly; make the same edit in "
                "the source layer to fix permanently."
            ),
        })
    return flags


def _check_missing_direction(pipes: gpd.GeoDataFrame) -> list[dict]:
    """
    Pipes with null FROMMH or TOMH.

    Geometry encodes direction (start = upstream, end = downstream), so all of
    these are direction_inferrable. Inverts are noted as a cross-check only.
    Severity is warning — not review_required — because geometry handles it.
    """
    flags = []
    null_mask = pipes["FROMMH"].isna() | pipes["TOMH"].isna()

    for idx, row in pipes[null_mask].iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue

        pipes.at[idx, "QA_STATUS"] = "direction_inferrable"

        up_inv = _num(row.get("UPSTREAMIN"))
        dn_inv = _num(row.get("DOWNSTREAM"))
        has_inverts = (np.isfinite(up_inv) and np.isfinite(dn_inv)
                       and up_inv != dn_inv)

        desc = (
            "FROMMH/TOMH null. Line geometry encodes flow direction "
            "(start point = upstream). Invert elevations also present as cross-check."
            if has_inverts else
            "FROMMH/TOMH null. Line geometry encodes flow direction "
            "(start point = upstream). No invert data to cross-check."
        )

        flags.append({
            "flag_type":  "missing_direction",
            "severity":   "warning",
            "pipe_id":    _fid(row),
            "pidx":       int(idx),
            "geometry":   geom.centroid,
            "description": desc,
        })

    return flags


def _check_invert_conflict(G: nx.MultiDiGraph,
                           pipes: gpd.GeoDataFrame) -> list[dict]:
    """
    Invert elevations that imply uphill flow relative to geometry direction.

    Delegates to graph_builder.invert_direction_conflicts(G): geometry says
    flow runs start(up_invert) → end(dn_invert); an edge with
    up_invert < dn_invert disagrees. Inverts that are NaN or <= 0 (the
    dataset's no-data placeholder) are skipped there.

    Severity is warning by design: geometry is the authoritative direction
    source and the inverts never override it (decision_log 2026-06-24 /
    2026-06-28). The conflict most often means bad invert data entry; only if
    other evidence corroborates (cycle membership, caught independently) does
    the pipe itself deserve scrutiny.
    """
    flags = []
    for _, r in invert_direction_conflicts(G).iterrows():
        pidx = int(r["pidx"])
        geom = pipes.geometry.iloc[pidx]
        if geom is None or geom.is_empty:
            continue
        flags.append({
            "flag_type": "invert_conflict",
            "severity":  "warning",
            "pipe_id":   r["facilityid"],
            "pidx":      pidx,
            "geometry":  geom.centroid,
            "description": (
                f"Invert elevations imply uphill flow vs line geometry: upstream "
                f"invert {r['up_invert']:.2f} ft is {r['rise_ft']:.2f} ft below "
                f"downstream invert {r['dn_invert']:.2f} ft. Geometry direction "
                "is treated as correct; check the invert fields (or a digitizing "
                "flip if the geometry is wrong here)."
            ),
        })
    return flags


def _check_negative_slope(pipes: gpd.GeoDataFrame,
                          invert_conflict_pidx: set) -> list[dict]:
    """
    Negative SLOPE field.

    Pipes already flagged as invert_conflict are skipped (same underlying
    direction/attribute question — avoid double-flagging). Otherwise flag as
    attribute_slope_error (warning) — geometry direction is still usable.
    """
    flags = []
    slope = pipes["SLOPE"].map(_num)
    for idx in pipes.index[slope.notna() & (slope < 0)]:
        if int(idx) in invert_conflict_pidx:
            continue
        row  = pipes.loc[idx]
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        flags.append({
            "flag_type":  "attribute_slope_error",
            "severity":   "warning",
            "pipe_id":    _fid(row),
            "pidx":       int(idx),
            "geometry":   geom.centroid,
            "description": (
                f"Recorded SLOPE is {slope.loc[idx]:.3f}% (negative). "
                "Line geometry direction is treated as correct; SLOPE field "
                "may need updating to match."
            ),
        })
    return flags


def _check_snap_gaps(pipes: gpd.GeoDataFrame,
                     G: nx.MultiDiGraph,
                     snap_tol_ft: float,
                     repair_radius_ft: float) -> list[dict]:
    """
    Near-miss endpoint gaps, in two tiers.

    Tier 1 — repaired (warning): pairs of pass-1 end-node clusters within
      (snap_tol, repair_radius]. Pass 2 of the shared snap merges exactly
      these, so the graph connects them and tracing works — but the source
      geometry is still offset and worth snapping in the authoritative layer.
      (This is the fix for the old wording, which claimed these "will not
      connect" — false since the pass-2 repair.)

    Tier 2 — unconnected (review_required): node pairs of the FINAL graph
      (after the pass-2 repair) within (snap_tol, 2 x repair_radius] where at
      least one node is an end node and the two share no pipe. These are
      separate nodes in the graph, so they genuinely do not connect at this
      location. End↔junction pairs are included: pass 2 only merges end↔end,
      so a dangling end near a junction is never repaired — even inside the
      repair radius. Tier 2 is read off G rather than pass-1 clusters because
      pass-2 merges are transitive — a pass-1 pair beyond the repair radius
      can still end up connected through an intermediate end node.

    Junction↔junction pairs are excluded — both already have continuity.
    """
    flags = []

    def _pair_flag(xa, ya, xb, yb, dist, fids, pidxs, repaired):
        gap_pt = Point((xa + xb) / 2, (ya + yb) / 2)
        fids = sorted(fids)
        if repaired:
            severity = "warning"
            desc = (
                f"Endpoint gap of {dist:.2f} ft between pipe ends "
                f"({' / '.join(fids)}). Within the {repair_radius_ft:.0f} ft "
                "end-node repair radius, so the graph auto-connects them and "
                "tracing works — but the source geometry is offset and should "
                "be snapped in the authoritative layer."
            )
        else:
            severity = "review_required"
            desc = (
                f"Endpoint gap of {dist:.2f} ft between pipe ends "
                f"({' / '.join(fids)}) — survived the {repair_radius_ft:.0f} ft "
                "end-node repair as separate nodes, so these do not connect "
                "at this location in the network graph."
            )
        return {
            "flag_type":   "snap_gap",
            "severity":    severity,
            "pipe_id":     " / ".join(fids),
            "member_pidx": sorted(pidxs),
            "geometry":    gap_pt,
            "description": desc,
        }

    # ---- Tier 1: pass-1 end-node pairs the pass-2 repair merges ----------
    snap1 = snap_endpoints(pipes, snap_tol_ft, 0.0)   # pass 1 only
    if snap1.n_nodes:
        node_pidx = [set() for _ in range(snap1.n_nodes)]
        node_fids = [set() for _ in range(snap1.n_nodes)]
        for i in range(len(snap1.ep_node)):
            nid = int(snap1.ep_node[i])
            node_pidx[nid].add(int(snap1.ep_pipe_idx[i]))
            node_fids[nid].add(snap1.ep_fids[i])

        end_ids = np.where(~(snap1.has_start & snap1.has_end))[0]
        if len(end_ids) > 1:
            xy = np.stack([snap1.mean_x[end_ids], snap1.mean_y[end_ids]], axis=1)
            for a, b in cKDTree(xy).query_pairs(r=repair_radius_ft):
                dist = float(np.hypot(*(xy[a] - xy[b])))
                if dist <= snap_tol_ft:
                    continue
                na, nb = int(end_ids[a]), int(end_ids[b])
                if node_pidx[na] & node_pidx[nb]:
                    continue   # same pipe's two ends — directly connected, not a gap
                flags.append(_pair_flag(
                    xy[a][0], xy[a][1], xy[b][0], xy[b][1], dist,
                    node_fids[na] | node_fids[nb],
                    node_pidx[na] | node_pidx[nb], repaired=True))

    # ---- Tier 2: final nodes that stayed apart ----------------------------
    nodes  = list(G.nodes(data=True))
    if len(nodes) > 1:
        xy     = np.array([[d["x"], d["y"]] for _, d in nodes])
        is_end = np.array([d["role"] != "junction" for _, d in nodes])
        ids    = [n for n, _ in nodes]
        inc = {n: (set(), set()) for n in G.nodes()}   # (pidxs, fids)
        for u, v, d in G.edges(data=True):
            for n in (u, v):
                inc[n][0].add(d["pidx"])
                inc[n][1].add(d["facilityid"])
        for a, b in cKDTree(xy).query_pairs(r=2 * repair_radius_ft):
            if not (is_end[a] or is_end[b]):
                continue   # junction-junction: both already have continuity
            dist = float(np.hypot(*(xy[a] - xy[b])))
            if dist <= snap_tol_ft:
                continue
            na, nb = ids[a], ids[b]
            if inc[na][0] & inc[nb][0]:
                continue   # nodes share a pipe — directly connected, not a gap
            flags.append(_pair_flag(
                xy[a][0], xy[a][1], xy[b][0], xy[b][1], dist,
                inc[na][1] | inc[nb][1],
                inc[na][0] | inc[nb][0], repaired=False))

    return flags


def _check_directed_cycles(G: nx.MultiDiGraph,
                           pipes: gpd.GeoDataFrame,
                           max_mappable_nodes: int = 10) -> list[dict]:
    """
    Directed cycles are impossible in a gravity sewer; every non-trivial
    strongly-connected component is a direction error. Small SCCs get a
    directed_cycle flag (mappable local flip); large ones get a large_cycle
    flag plus a suspect list for large_cycle_suspects.csv.

    Membership = pipes with either endpoint in the SCC, found in one pass over
    the graph edges (node -> SCC map), not per-SCC scans of the pipe table.
    """
    flags = []
    sccs = [c for c in nx.strongly_connected_components(G) if len(c) > 1]
    sccs.sort(key=len, reverse=True)
    if not sccs:
        return flags

    node2scc = {n: i for i, scc in enumerate(sccs) for n in scc}
    members = [{} for _ in sccs]   # scc index -> {pidx: facilityid}
    for u, v, d in G.edges(data=True):
        for scc_i in {node2scc.get(u), node2scc.get(v)} - {None}:
            members[scc_i][d["pidx"]] = d["facilityid"]

    for i, scc in enumerate(sccs):
        if not members[i]:
            continue
        pidxs      = sorted(members[i])
        member_ids = [members[i][p] for p in pidxs]
        geoms      = pipes.geometry.iloc[pidxs]
        centroid   = geoms.union_all().centroid
        n_nodes    = len(scc)

        if n_nodes <= max_mappable_nodes:
            flags.append({
                "flag_type":   "directed_cycle",
                "severity":    "review_required",
                "pipe_id":     " / ".join(member_ids),
                "member_pidx": pidxs,
                "geometry":    centroid,
                "description": (
                    f"Small directed loop: {n_nodes} nodes / {len(pidxs)} pipes "
                    "form a cycle. Almost certainly one or two flipped pipes."
                ),
            })
        else:
            rows = pipes.iloc[pidxs]
            up   = rows["UPSTREAMIN"].map(_num)
            dn   = rows["DOWNSTREAM"].map(_num)
            sl   = rows["SLOPE"].map(_num)
            ic_mask  = up.notna() & dn.notna() & (up < dn)
            neg_mask = sl.notna() & (sl < 0)
            no_invert = int(up.isna().sum())

            member_rows = []
            for pidx, fid in zip(pidxs, member_ids):
                reasons = []
                if ic_mask.loc[pidx]:  reasons.append("uphill_invert")
                if neg_mask.loc[pidx]: reasons.append("negative_slope")
                c = pipes.geometry.iloc[pidx].centroid
                member_rows.append({
                    "scc_id":         i + 1,
                    "pipe_id":        fid,
                    "is_suspect":     bool(reasons),
                    "suspect_reason": ";".join(reasons),
                    "x":              round(c.x, 2),
                    "y":              round(c.y, 2),
                })

            n_suspect = int((ic_mask | neg_mask).sum())
            flags.append({
                "flag_type":    "large_cycle",
                "severity":     "review_required",
                "pipe_id":      f"SCC_{i + 1} ({len(pidxs)} pipes)",
                "member_pidx":  pidxs,
                "member_pipes": member_rows,
                "geometry":     centroid,
                "description": (
                    f"Large directed tangle (SCC {i + 1}): {n_nodes} nodes / "
                    f"{len(pidxs)} pipes. Not a physical loop — likely systematic "
                    f"direction errors. {n_suspect} prime suspect(s): "
                    f"{int(ic_mask.sum())} uphill inverts, "
                    f"{int(neg_mask.sum())} negative slopes; "
                    f"{no_invert} pipe(s) have no invert data. "
                    "See large_cycle_suspects.csv."
                ),
            })
    return flags


def _check_disconnected_components(G: nx.MultiDiGraph,
                                   pipes: gpd.GeoDataFrame,
                                   max_nodes: int = 50) -> list[dict]:
    """
    Flag SMALL weakly-connected fragments (<= max_nodes nodes).

    The study network is genuinely multiple large drainage basins — the
    largest weak component holds only ~27% of nodes, and several others hold
    thousands. A large component is a separate basin, not an error, so only
    small fragments (orphaned stubs, digitizing islands) are flagged. The
    threshold matches the sample-site pre-flight check (check_sample_sites.py,
    decision_log 2026-06-28), which uses < 50 nodes as "suspicious fragment".

    Membership found in one pass over the graph edges via a node -> component
    map, not per-component scans of the pipe table.
    """
    flags = []
    components = list(nx.connected_components(G.to_undirected(as_view=True)))
    if len(components) <= 1:
        return flags

    node2comp = {n: i for i, comp in enumerate(components) for n in comp}
    members = [{} for _ in components]   # comp index -> {pidx: facilityid}
    for u, v, d in G.edges(data=True):
        members[node2comp[u]][d["pidx"]] = d["facilityid"]

    for i, comp in enumerate(components):
        if len(comp) > max_nodes or not members[i]:
            continue
        pidxs    = sorted(members[i])
        centroid = pipes.geometry.iloc[pidxs].union_all().centroid
        flags.append({
            "flag_type":   "disconnected_component",
            "severity":    "warning",
            "pipe_id":     f"component_{i}",
            "member_pidx": pidxs,
            "geometry":    centroid,
            "description": (
                f"Small isolated fragment: {len(comp)} node(s) / "
                f"{len(pidxs)} pipe(s) not connected to any larger part of "
                "the network. Larger separate components are treated as "
                "distinct drainage basins and not flagged."
            ),
        })
    return flags


def _check_isolated_manholes(pipes: gpd.GeoDataFrame,
                             manholes: gpd.GeoDataFrame,
                             G: nx.MultiDiGraph,
                             mh_snap_ft: float) -> list[dict]:
    """
    Manholes not connected to any pipe — by FACILITYID first, then by
    coordinate proximity to the shared graph's nodes.

    A manhole that fails the FACILITYID check but snaps to a graph node within
    mh_snap_ft is connected via geometry — flagged as ID mismatch (warning)
    rather than truly isolated. These are manhole flags: they carry no pidx
    and never touch pipe QA fields.
    """
    flags = []

    connected_ids = set(pipes["FROMMH"].dropna()) | set(pipes["TOMH"].dropna())
    unmatched     = manholes[~manholes["FACILITYID"].isin(connected_ids)]

    if unmatched.empty or G.number_of_nodes() == 0:
        return flags

    node_xy = np.array([[G.nodes[n]["x"], G.nodes[n]["y"]] for n in G.nodes()])
    tree    = cKDTree(node_xy)

    for _, row in unmatched.iterrows():
        pt = row.geometry
        if pt is None or pt.is_empty:
            continue
        dist = float(tree.query([[pt.x, pt.y]])[0][0])

        if dist <= mh_snap_ft:
            desc = (
                f"Manhole {_fid(row)} not found in FROMMH/TOMH attributes, "
                f"but snaps to a pipe endpoint {dist:.1f} ft away — likely an ID "
                "mismatch between the manholes layer and the pipes layer."
            )
        else:
            desc = (
                f"Manhole {_fid(row)} does not appear in FROMMH/TOMH "
                f"and is {dist:.1f} ft from the nearest pipe endpoint — "
                "genuinely disconnected or outside the mapped network."
            )
        flags.append({
            "flag_type":  "isolated_manhole",
            "severity":   "warning",
            "pipe_id":    _fid(row),
            "geometry":   pt,
            "description": desc,
        })
    return flags


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_flag_fields(pipes: gpd.GeoDataFrame, flags: list[dict]) -> None:
    """
    Write QA_STATUS and QA_FLAGS onto the pipes GeoDataFrame, keyed by pidx.

    Flags address pipes via `pidx` (single) or `member_pidx` (list). Flags with
    neither (isolated_manhole) belong to manholes and are skipped — keying by
    FACILITYID smeared flags across same-ID / null-ID pipes and let manhole
    flags land on pipes that happened to share the ID.
    """
    flag_map: dict[int, list[str]] = {}
    for f in flags:
        pidxs = f.get("member_pidx")
        if pidxs is None:
            pidxs = [f["pidx"]] if f.get("pidx") is not None else []
        for pidx in pidxs:
            flag_map.setdefault(int(pidx), []).append(f["flag_type"])

    for pidx, types in flag_map.items():
        pipes.at[pidx, "QA_FLAGS"] = ", ".join(dict.fromkeys(types))
        if pipes.at[pidx, "QA_STATUS"] == "original":
            pipes.at[pidx, "QA_STATUS"] = "flagged"
