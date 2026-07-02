"""
Phase 3 — directed network graph builder.

Geometry-first: pipe flow direction is taken from the digitized line geometry
(start point = upstream, end point = downstream), per the 2026-06-24 redesign.
FROMMH/TOMH and invert elevations are kept as cross-checks, not direction
sources.

Nodes are the snapped endpoint clusters produced by `node_layer.snap_endpoints`
(the same two-pass snap that built the QC-clean node layer), so the graph shares
exactly one topology with the node layer. Each pipe becomes one directed edge
from its start node to its end node.

A MultiDiGraph is used so parallel mains (two pipes between the same node pair)
and self-loops (a pipe whose endpoints snap to one node) are preserved rather
than silently collapsed — both are QC signals reported by build_graph().

The headline validation: a correctly-directed gravity network is a forest, so
it should contain no directed cycles. Every strongly-connected component with
more than one node is a direction error. See `summarize_graph`.
"""

import numpy as np
import pandas as pd
import geopandas as gpd
import networkx as nx
from scipy.spatial import cKDTree

from node_layer import snap_endpoints


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph(pipes: gpd.GeoDataFrame,
                snap_tol_ft: float,
                snap_gap_search_radius_ft: float = 0.0,
                manual_snaps: list[dict] | None = None) -> nx.MultiDiGraph:
    """
    Build a directed graph from pipe geometry.

    Parameters
    ----------
    pipes                      GeoDataFrame of gravity mains (any CRS; lengths
                               use the GeoDataFrame's own units)
    snap_tol_ft                Pass-1 endpoint merge tolerance (ft)
    snap_gap_search_radius_ft  Pass-2 end-node repair radius (ft)
    manual_snaps               Human-confirmed node merges from the QA review
                               decisions file ({x, y, radius_ft} dicts; see
                               qa_review.manual_snaps) — applied as pass 3 of
                               the shared snap

    Returns
    -------
    nx.MultiDiGraph
        Nodes keyed by integer node_id, with attributes:
            x, y   — node centroid coordinates
            role   — junction / start_only / end_only
        Edges directed start_node → end_node, with attributes:
            pidx        — positional index into `pipes` (geometry lookup key)
            facilityid  — FACILITYID string ("?" if null)
            length_ft   — geometry length
            slope       — SLOPE attribute (cross-check only)
            up_invert   — UPSTREAMIN invert elevation (cross-check)
            dn_invert   — DOWNSTREAM invert elevation (cross-check)

    Graph-level attributes (graph.graph):
            n_self_loops    — edges whose start and end snap to one node
            n_parallel      — extra edges beyond the first on a node pair
    """
    snap = snap_endpoints(pipes, snap_tol_ft, snap_gap_search_radius_ft,
                          manual_snaps=manual_snaps)

    G = nx.MultiDiGraph()

    # Add every node first so isolated/degenerate nodes still appear.
    for nid in range(snap.n_nodes):
        if snap.has_start[nid] and snap.has_end[nid]:
            role = "junction"
        elif snap.has_start[nid]:
            role = "start_only"
        else:
            role = "end_only"
        G.add_node(nid,
                   x=float(snap.mean_x[nid]),
                   y=float(snap.mean_y[nid]),
                   role=role)

    # Reconstruct each pipe's (start_node, end_node) from the endpoint stream.
    # Endpoints are emitted start-then-end per included pipe, so we collect both
    # by positional pipe index rather than relying on array stride.
    pipe_nodes = {}   # pidx -> [start_node, end_node]
    for i in range(len(snap.ep_pipe_idx)):
        pidx = int(snap.ep_pipe_idx[i])
        slot = pipe_nodes.setdefault(pidx, [None, None])
        if snap.ep_is_start[i]:
            slot[0] = int(snap.ep_node[i])
        else:
            slot[1] = int(snap.ep_node[i])

    n_self_loops = 0
    for pidx, (u, v) in pipe_nodes.items():
        if u is None or v is None:
            continue  # geometry with a single distinct point — skip
        row = pipes.iloc[pidx]
        geom = row.geometry
        if u == v:
            n_self_loops += 1
        G.add_edge(
            u, v,
            pidx=pidx,
            facilityid=str(row["FACILITYID"]) if pd.notna(row.get("FACILITYID")) else "?",
            length_ft=float(geom.length) if geom is not None else 0.0,
            slope=_num(row.get("SLOPE")),
            up_invert=_num(row.get("UPSTREAMIN")),
            dn_invert=_num(row.get("DOWNSTREAM")),
        )

    # Parallel edges: extra edges beyond the first on any directed node pair.
    pair_counts = {}
    for u, v in G.edges(keys=False):
        pair_counts[(u, v)] = pair_counts.get((u, v), 0) + 1
    n_parallel = sum(c - 1 for c in pair_counts.values() if c > 1)

    G.graph["n_self_loops"] = n_self_loops
    G.graph["n_parallel"]   = n_parallel
    return G


class NodeIndex:
    """Spatial index over graph nodes for nearest-node lookups."""

    def __init__(self, node_ids, tree):
        self.node_ids = node_ids
        self.tree = tree


def build_node_index(G: nx.MultiDiGraph) -> NodeIndex:
    """Build a cKDTree over node centroids for coordinate→node snapping."""
    node_ids = list(G.nodes())
    xy = np.array([[G.nodes[n]["x"], G.nodes[n]["y"]] for n in node_ids])
    return NodeIndex(node_ids, cKDTree(xy))


def nearest_node(index: NodeIndex, x: float, y: float):
    """Return (node_id, distance_ft) of the node nearest to (x, y)."""
    dist, idx = index.tree.query([x, y])
    return index.node_ids[int(idx)], float(dist)


def nodes_within(index: NodeIndex, x: float, y: float, radius: float):
    """Return [(node_id, distance_ft), ...] within radius of (x, y), nearest first."""
    out = []
    for j in index.tree.query_ball_point([x, y], r=radius):
        nx_, ny_ = index.tree.data[j]
        out.append((index.node_ids[j], float(np.hypot(nx_ - x, ny_ - y))))
    out.sort(key=lambda t: t[1])
    return out


def load_graph_from_config(cfg: dict):
    """
    Load pipes and build the directed graph from a validated config dict.

    Centralizes the read → reproject → build_graph boilerplate shared by every
    runner so the build signature lives in one place. Returns (G, pipes); pipes
    is returned because Phase 5 needs the geometries addressed by edge `pidx`.

    Manual snaps from the QA review decisions file (if configured) are applied
    here, so traversal and delineation trace the same human-repaired topology
    that QA reports on.
    """
    from qa_review import manual_snaps_from_config
    p = cfg["parameters"]
    pipes = gpd.read_file(cfg["inputs"]["gravity_main_shapefile"]).to_crs(p["crs"])
    G = build_graph(pipes,
                    p["node_snap_tolerance_ft"],
                    p.get("snap_gap_search_radius_ft", 10.0),
                    manual_snaps=manual_snaps_from_config(cfg))
    return G, pipes


def _num(val):
    """Coerce an attribute to float, mapping null/blank to NaN."""
    if val is None:
        return float("nan")
    try:
        f = float(val)
    except (TypeError, ValueError):
        return float("nan")
    return f


# ---------------------------------------------------------------------------
# Validation / reporting
# ---------------------------------------------------------------------------

def summarize_graph(G: nx.MultiDiGraph) -> dict:
    """
    Compute QC statistics for the directed graph.

    The key field is `nontrivial_sccs`: strongly-connected components with more
    than one node. A correctly-directed gravity network has none. Any that
    survive are direction errors and must be resolved before traversal (Phase 4)
    can be trusted through that area.
    """
    sccs = [c for c in nx.strongly_connected_components(G) if len(c) > 1]
    scc_sizes = sorted((len(c) for c in sccs), reverse=True)

    sources = [n for n in G.nodes if G.in_degree(n) == 0 and G.out_degree(n) > 0]
    sinks   = [n for n in G.nodes if G.out_degree(n) == 0 and G.in_degree(n) > 0]
    isolated = [n for n in G.nodes if G.degree(n) == 0]

    undirected = G.to_undirected(as_view=True)
    weak_components = nx.number_connected_components(undirected)

    return {
        "n_nodes": G.number_of_nodes(),
        "n_edges": G.number_of_edges(),
        "n_self_loops": G.graph.get("n_self_loops", 0),
        "n_parallel": G.graph.get("n_parallel", 0),
        "n_sources": len(sources),
        "n_sinks": len(sinks),
        "n_isolated": len(isolated),
        "weak_components": weak_components,
        "n_nontrivial_sccs": len(sccs),
        "scc_sizes": scc_sizes,
        "largest_scc": scc_sizes[0] if scc_sizes else 0,
        "scc_node_sets": sccs,
    }


def invert_direction_conflicts(G: nx.MultiDiGraph) -> pd.DataFrame:
    """
    Cross-check geometry direction against invert elevations.

    Geometry says flow runs start(up_invert) → end(dn_invert), so a falling
    profile has up_invert > dn_invert. Edges where up_invert < dn_invert
    (water would climb) disagree with the inverts. This corroborates the SCC
    result but never overrides geometry — it is QC only. Edges missing either
    invert (NaN or <= 0 placeholder) are skipped.

    Returns a DataFrame sorted by the size of the disagreement (worst first).
    """
    rows = []
    for u, v, d in G.edges(data=True):
        up, dn = d.get("up_invert"), d.get("dn_invert")
        if up is None or dn is None:
            continue
        if not (np.isfinite(up) and np.isfinite(dn)):
            continue
        if up <= 0 or dn <= 0:
            continue  # 0 is a common "no data" placeholder in this dataset
        if up < dn:
            rows.append({
                "facilityid": d.get("facilityid"),
                "pidx": d.get("pidx"),
                "u": u, "v": v,
                "up_invert": up, "dn_invert": dn,
                "rise_ft": round(dn - up, 2),
                "slope": d.get("slope"),
            })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("rise_ft", ascending=False).reset_index(drop=True)
    return df
