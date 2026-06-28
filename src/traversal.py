"""
Phase 4 — upstream traversal.

Given a target manhole (by FACILITYID or coordinate), resolve it to a graph node
and walk the directed graph in reverse to find every pipe whose flow reaches that
manhole — the contributing sewer network.

Direction is geometry-derived (Phase 3), so "upstream" means following edges from
head back to tail. The result is the set of contributing edges, keyed by `pidx`
(the positional index into the pipes GeoDataFrame) with the traversal depth of
each, plus the contributing node set. That edge set is the entire handoff to
Phase 5 (buffer + population join).

Resolution rules (tolerances from config):
  - manhole_id    → look up the manhole point in manholes.shp, snap to the
                    nearest graph node within manhole_node_snap_ft (~5 ft;
                    manholes sit on pipe endpoints, so this is tight).
  - coordinate    → snap directly to the nearest node within
                    manhole_snap_distance_ft (~50 ft).
A snap beyond tolerance raises TargetResolutionError — the manhole is not on the
modeled gravity-main network and the run should stop, not silently mis-resolve.

A target with no upstream edges (a headwater, in_degree 0) is a valid result, not
an error: it returns an empty edge set, which Phase 6 reports as no_upstream_found.
"""

import geopandas as gpd
import networkx as nx

from graph_builder import build_node_index, nearest_node, nodes_within


class TargetResolutionError(Exception):
    """Raised when a target manhole/coordinate can't be matched to a graph node."""


class TraversalResult:
    """
    Outcome of an upstream trace.

    Attributes
    ----------
    target_node     resolved graph node id
    target_xy       (x, y) coordinate the target snapped from
    snap_dist_ft    distance from target point to the resolved node
    source          how the target was given ("manhole_id" / "coordinate")
    source_value    the id or coordinate used
    edges           list of dicts: {pidx, facilityid, u, v, key, depth}
    nodes           set of contributing node ids (excludes the target itself)
    max_depth       deepest traversal level reached
    """

    def __init__(self, target_node, target_xy, snap_dist_ft, source, source_value,
                 edges, nodes, max_depth):
        self.target_node  = target_node
        self.target_xy    = target_xy
        self.snap_dist_ft = snap_dist_ft
        self.source       = source
        self.source_value = source_value
        self.edges        = edges
        self.nodes        = nodes
        self.max_depth    = max_depth

    @property
    def pidx_list(self):
        """Positional pipe indices of all contributing edges (the Phase 5 handoff)."""
        return [e["pidx"] for e in self.edges]

    @property
    def n_edges(self):
        return len(self.edges)

    @property
    def is_empty(self):
        return len(self.edges) == 0


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def resolve_target_node(G, cfg, manholes: gpd.GeoDataFrame = None, index=None):
    """
    Resolve the configured target (manhole_id or coordinate) to a graph node.

    Returns (node_id, (x, y), snap_dist_ft, source, source_value).
    Raises TargetResolutionError if the target can't be matched within tolerance.

    `index` (a NodeIndex) may be passed in to avoid rebuilding the KD-tree when
    tracing many targets against one graph; it is built on demand otherwise.
    """
    params  = cfg["parameters"]
    inputs  = cfg["inputs"]
    if index is None:
        index = build_node_index(G)

    mh_id    = inputs.get("manhole_id")
    mh_coord = inputs.get("manhole_coordinate")

    if mh_id:
        if manholes is None:
            manholes = gpd.read_file(inputs["manholes_shapefile"]).to_crs(params["crs"])
        match = _match_facilityid(manholes, mh_id)
        if match.empty:
            raise TargetResolutionError(
                f"manhole_id '{mh_id}' not found in {inputs['manholes_shapefile']}"
            )
        if len(match) > 1:
            raise TargetResolutionError(
                f"manhole_id '{mh_id}' matches {len(match)} manholes — ambiguous"
            )
        pt = match.geometry.iloc[0]
        if pt is None or pt.is_empty:
            raise TargetResolutionError(f"manhole_id '{mh_id}' has no geometry")
        x, y = pt.x, pt.y
        tol = params["manhole_node_snap_ft"]
        source, source_value = "manhole_id", str(mh_id)
    elif mh_coord is not None:
        x, y = float(mh_coord[0]), float(mh_coord[1])
        tol = params["manhole_snap_distance_ft"]
        source, source_value = "coordinate", f"({x:.1f}, {y:.1f})"
    else:
        raise TargetResolutionError(
            "no target given — set inputs.manhole_id or inputs.manhole_coordinate"
        )

    node, dist = nearest_node(index, x, y)
    if dist > tol:
        raise TargetResolutionError(
            f"target {source} {source_value} is {dist:.1f} ft from the nearest "
            f"network node (tolerance {tol} ft) — not on the gravity-main network"
        )

    # False-headwater guard: at a manhole where the incoming and outgoing pipe
    # endpoints didn't merge, a degenerate start_only node (in_degree 0) can sit
    # marginally closer than the real junction, which would silently return an
    # empty upstream trace. If the nearest node has no upstream but another node
    # at the same manhole (within tolerance) does, prefer the one with upstream.
    if G.in_degree(node) == 0:
        upstream = [(nid, d) for nid, d in nodes_within(index, x, y, tol)
                    if G.in_degree(nid) > 0]
        if upstream:
            node, dist = upstream[0]

    return node, (x, y), dist, source, source_value


def _match_facilityid(manholes: gpd.GeoDataFrame, mh_id):
    """
    Match a manhole by FACILITYID, tolerating zero-pad / unpadded differences.

    Exact string match first (so non-numeric IDs like 'PS103' work), then an
    integer-normalized fallback so '17506', '017506', and a config value of
    17506 all resolve to the same stored ID.
    """
    fid = manholes["FACILITYID"].astype(str)
    exact = manholes[fid == str(mh_id)]
    if not exact.empty:
        return exact
    try:
        target = int(str(mh_id).strip())
    except ValueError:
        return exact  # non-numeric id, no numeric fallback possible
    norm = fid.str.fullmatch(r"0*\d+")
    numeric = fid.where(norm).str.lstrip("0").replace("", "0")
    return manholes[numeric == str(target)]


# ---------------------------------------------------------------------------
# Upstream traversal
# ---------------------------------------------------------------------------

def traverse_upstream(G: nx.MultiDiGraph, target_node):
    """
    Reverse-BFS from target_node, collecting every contributing edge with depth.

    Each node is expanded once (visited-set), so the 2-node SCC and self-loop are
    safe and each contributing edge is recorded exactly once. An edge u→v
    contributes iff its head v is reachable downstream to the target, which holds
    for every in-edge of a contributing node.

    Returns (edges, contributing_nodes, max_depth).
    """
    visited = {target_node}      # nodes whose in-edges have been collected
    edges   = []
    frontier = [target_node]
    depth = 0

    while frontier:
        depth += 1
        next_frontier = []
        for node in frontier:
            for u, v, k, data in G.in_edges(node, keys=True, data=True):
                edges.append({
                    "pidx":       data.get("pidx"),
                    "facilityid": data.get("facilityid"),
                    "u": u, "v": v, "key": k,
                    "depth": depth,
                })
                if u not in visited:
                    visited.add(u)
                    next_frontier.append(u)
        frontier = next_frontier

    contributing_nodes = visited - {target_node}
    max_depth = max((e["depth"] for e in edges), default=0)
    return edges, contributing_nodes, max_depth


def trace_manhole(G, cfg, manholes: gpd.GeoDataFrame = None, index=None) -> TraversalResult:
    """Resolve the configured target and trace its upstream network end to end."""
    node, xy, dist, source, source_value = resolve_target_node(G, cfg, manholes, index)
    edges, nodes, max_depth = traverse_upstream(G, node)
    return TraversalResult(
        target_node=node, target_xy=xy, snap_dist_ft=dist,
        source=source, source_value=source_value,
        edges=edges, nodes=nodes, max_depth=max_depth,
    )
