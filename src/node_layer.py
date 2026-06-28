"""
Geometry-derived endpoint node layer for QC.

Extracts the start and end point of every pipe, snaps coincident endpoints
within node_snap_tolerance_ft into shared nodes, and classifies each node:

  junction    — appears as both a pipe start and a pipe end (real manhole)
  start_only  — appears only as a pipe start (upstream headwater / dead end)
  end_only    — appears only as a pipe end (outlet / dead end)

Snap gap detection runs only on end nodes (start_only / end_only). Junctions
already have continuity, so there is no gap to flag there. Restricting the
search to end nodes also avoids false positives on short pipe segments.
"""

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point, LineString
from scipy.spatial import cKDTree


# ---------------------------------------------------------------------------
# Union-find helpers
# ---------------------------------------------------------------------------

def _make_uf(n):
    return list(range(n))

def _find(parent, i):
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i

def _union(parent, i, j):
    pi, pj = _find(parent, i), _find(parent, j)
    if pi != pj:
        parent[pj] = pi


# ---------------------------------------------------------------------------
# Endpoint snapping core (shared by the node layer and the graph builder)
# ---------------------------------------------------------------------------

class SnapResult:
    """
    Result of the two-pass endpoint snap.

    Attributes
    ----------
    ep_pipe_idx  (n_ep,) int   — positional pipe index each endpoint came from
    ep_is_start  (n_ep,) bool  — True for a pipe start point, False for an end
    ep_fids      list[str]     — FACILITYID per endpoint ("?" if null)
    ep_coords    (n_ep, 2)     — raw endpoint coordinates
    ep_node      (n_ep,) int   — final node (cluster) id each endpoint belongs to
    n_nodes      int           — number of final nodes
    mean_x, mean_y (n_nodes,)  — node centroid coordinates
    has_start, has_end (n_nodes,) bool — role inputs per node
    """

    def __init__(self, ep_pipe_idx, ep_is_start, ep_fids, ep_coords,
                 ep_node, n_nodes, mean_x, mean_y, has_start, has_end):
        self.ep_pipe_idx = ep_pipe_idx
        self.ep_is_start = ep_is_start
        self.ep_fids     = ep_fids
        self.ep_coords   = ep_coords
        self.ep_node     = ep_node
        self.n_nodes     = n_nodes
        self.mean_x      = mean_x
        self.mean_y      = mean_y
        self.has_start   = has_start
        self.has_end     = has_end


def snap_endpoints(pipes: gpd.GeoDataFrame,
                   snap_tol_ft: float,
                   snap_gap_search_radius_ft: float = 0.0) -> SnapResult:
    """
    Collect pipe endpoints and snap them into shared nodes (two passes).

    Pass 1 — merge raw endpoints within snap_tol_ft (coincident endpoints).
    Pass 2 — targeted repair: merge pass-1 end nodes (start_only / end_only)
             within snap_gap_search_radius_ft. Junctions are never touched,
             so already-connected short segments cannot be collapsed.

    Returns a SnapResult exposing the exact endpoint→node assignment so callers
    can build both the node layer and the directed graph from one snap. The
    positional pipe index is tracked through the snap so each pipe maps cleanly
    to its (start_node, end_node) pair regardless of null/duplicate FACILITYIDs
    or skipped empty geometries.
    """
    ep_coords   = []
    ep_is_start = []
    ep_fids     = []
    ep_pipe_idx = []

    for pos, (_, row) in enumerate(pipes.iterrows()):
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        coords = list(geom.coords)
        fid = str(row["FACILITYID"]) if pd.notna(row.get("FACILITYID")) else "?"
        ep_coords.append(coords[0])
        ep_is_start.append(True)
        ep_fids.append(fid)
        ep_pipe_idx.append(pos)
        ep_coords.append(coords[-1])
        ep_is_start.append(False)
        ep_fids.append(fid)
        ep_pipe_idx.append(pos)

    if not ep_coords:
        return SnapResult(
            ep_pipe_idx=np.array([], dtype=int),
            ep_is_start=np.array([], dtype=bool),
            ep_fids=[],
            ep_coords=np.empty((0, 2), dtype=float),
            ep_node=np.array([], dtype=int),
            n_nodes=0,
            mean_x=np.array([]), mean_y=np.array([]),
            has_start=np.array([], dtype=bool),
            has_end=np.array([], dtype=bool),
        )

    coords_arr   = np.array(ep_coords, dtype=float)
    is_start_arr = np.array(ep_is_start, dtype=bool)
    pipe_idx_arr = np.array(ep_pipe_idx, dtype=int)
    n_ep         = len(ep_coords)

    # ------------------------------------------------------------------
    # Pass 1: merge raw endpoints within snap_tol_ft
    # ------------------------------------------------------------------
    parent = _make_uf(n_ep)
    tree   = cKDTree(coords_arr)
    for a, b in tree.query_pairs(r=snap_tol_ft):
        _union(parent, a, b)

    root_to_cid = {}
    ep_cid = []
    for i in range(n_ep):
        root = _find(parent, i)
        if root not in root_to_cid:
            root_to_cid[root] = len(root_to_cid)
        ep_cid.append(root_to_cid[root])

    ep_cid  = np.array(ep_cid)
    n_c1    = len(root_to_cid)

    # Compute pass-1 cluster properties
    sum_x1    = np.zeros(n_c1)
    sum_y1    = np.zeros(n_c1)
    cnt1      = np.zeros(n_c1, dtype=int)
    hs1       = np.zeros(n_c1, dtype=bool)  # has_start
    he1       = np.zeros(n_c1, dtype=bool)  # has_end

    for i, cid in enumerate(ep_cid):
        sum_x1[cid] += coords_arr[i, 0]
        sum_y1[cid] += coords_arr[i, 1]
        cnt1[cid]   += 1
        if is_start_arr[i]:
            hs1[cid] = True
        else:
            he1[cid] = True

    cx1 = sum_x1 / cnt1
    cy1 = sum_y1 / cnt1

    # ------------------------------------------------------------------
    # Pass 2: merge end-node clusters within snap_gap_search_radius_ft
    # Only clusters that are pure end nodes (not junctions) are eligible.
    # ------------------------------------------------------------------
    parent2 = _make_uf(n_c1)

    if snap_gap_search_radius_ft > snap_tol_ft:
        is_end_node = ~(hs1 & he1)          # True if NOT a junction
        end_cids    = np.where(is_end_node)[0]

        if len(end_cids) > 1:
            end_coords = np.stack([cx1[end_cids], cy1[end_cids]], axis=1)
            tree2      = cKDTree(end_coords)
            for ia, ib in tree2.query_pairs(r=snap_gap_search_radius_ft):
                ca = end_cids[ia]
                cb = end_cids[ib]
                _union(parent2, ca, cb)

    # Map pass-1 cluster → final cluster
    root2_to_cid = {}
    c1_to_cf = []
    for i in range(n_c1):
        root = _find(parent2, i)
        if root not in root2_to_cid:
            root2_to_cid[root] = len(root2_to_cid)
        c1_to_cf.append(root2_to_cid[root])

    c1_to_cf = np.array(c1_to_cf)
    n_final  = len(root2_to_cid)

    # Map each raw endpoint → final cluster
    ep_cf = c1_to_cf[ep_cid]

    # ------------------------------------------------------------------
    # Aggregate final clusters
    # ------------------------------------------------------------------
    sum_xf    = np.zeros(n_final)
    sum_yf    = np.zeros(n_final)
    cntf      = np.zeros(n_final, dtype=int)
    has_start = np.zeros(n_final, dtype=bool)
    has_end   = np.zeros(n_final, dtype=bool)

    for i, cid in enumerate(ep_cf):
        sum_xf[cid] += coords_arr[i, 0]
        sum_yf[cid] += coords_arr[i, 1]
        cntf[cid]   += 1
        if is_start_arr[i]:
            has_start[cid] = True
        else:
            has_end[cid] = True

    mean_x = sum_xf / cntf
    mean_y = sum_yf / cntf

    return SnapResult(
        ep_pipe_idx=pipe_idx_arr,
        ep_is_start=is_start_arr,
        ep_fids=ep_fids,
        ep_coords=coords_arr,
        ep_node=ep_cf,
        n_nodes=n_final,
        mean_x=mean_x, mean_y=mean_y,
        has_start=has_start, has_end=has_end,
    )


# ---------------------------------------------------------------------------
# Node layer
# ---------------------------------------------------------------------------

def build_node_layer(pipes: gpd.GeoDataFrame,
                     snap_tol_ft: float,
                     snap_gap_search_radius_ft: float = 0.0) -> gpd.GeoDataFrame:
    """
    Derive unique endpoint nodes from pipe geometry.

    Thin wrapper over snap_endpoints() that materializes the final clusters as
    a point GeoDataFrame with role classification:
      junction   — contains both pipe starts and pipe ends
      start_only — contains only pipe starts
      end_only   — contains only pipe ends

    Parameters
    ----------
    pipes                      GeoDataFrame of gravity mains
    snap_tol_ft                Pass-1 tolerance (ft)
    snap_gap_search_radius_ft  Pass-2 repair radius (ft). 0 disables pass 2.
    """
    snap = snap_endpoints(pipes, snap_tol_ft, snap_gap_search_radius_ft)

    if snap.n_nodes == 0:
        return gpd.GeoDataFrame(
            columns=["node_id", "x", "y", "pipe_count", "pipe_ids", "role", "geometry"],
            crs=pipes.crs,
        )

    cntf      = np.zeros(snap.n_nodes, dtype=int)
    fid_lists = [[] for _ in range(snap.n_nodes)]
    for i, cid in enumerate(snap.ep_node):
        cntf[cid] += 1
        fid_lists[cid].append(snap.ep_fids[i])

    def _dedup(lst):
        seen, out = set(), []
        for v in lst:
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    rows = []
    for cid in range(snap.n_nodes):
        if snap.has_start[cid] and snap.has_end[cid]:
            role = "junction"
        elif snap.has_start[cid]:
            role = "start_only"
        else:
            role = "end_only"

        rows.append({
            "node_id":    cid,
            "x":          round(snap.mean_x[cid], 2),
            "y":          round(snap.mean_y[cid], 2),
            "pipe_count": int(cntf[cid]),
            "pipe_ids":   ",".join(_dedup(fid_lists[cid])),
            "role":       role,
            "geometry":   Point(snap.mean_x[cid], snap.mean_y[cid]),
        })

    return gpd.GeoDataFrame(rows, crs=pipes.crs)


# ---------------------------------------------------------------------------
# Snap gap detection (end nodes only)
# ---------------------------------------------------------------------------

def find_snap_gap_flags(nodes: gpd.GeoDataFrame,
                        snap_tol_ft: float,
                        search_radius_ft: float) -> gpd.GeoDataFrame:
    """
    Find potential snap gaps by looking for pairs of end nodes that are close
    but not yet merged.

    Only end nodes (start_only / end_only) are checked — junctions already
    have continuity. This prevents short pipe segments from being flagged
    (their endpoints either snap into a junction in pass 1, or remain as
    isolated end nodes that are genuinely disconnected).

    Returns a line GeoDataFrame — one row per near-miss pair — for GIS review.

    Parameters
    ----------
    nodes            Output of build_node_layer()
    snap_tol_ft      Lower bound: pairs this close were already merged (skip)
    search_radius_ft Upper bound: only flag pairs within this distance
    """
    end_nodes = nodes[nodes["role"].isin(["start_only", "end_only"])].copy()
    end_nodes = end_nodes.reset_index(drop=True)

    if end_nodes.empty or search_radius_ft <= snap_tol_ft:
        return gpd.GeoDataFrame(
            columns=["dist_ft", "node_id_a", "node_id_b",
                     "role_a", "role_b", "pipe_ids_a", "pipe_ids_b", "geometry"],
            crs=nodes.crs,
        )

    xy   = np.stack([end_nodes["x"].values, end_nodes["y"].values], axis=1)
    tree = cKDTree(xy)

    rows = []
    for a, b in tree.query_pairs(r=search_radius_ft):
        dist = float(np.hypot(xy[a, 0] - xy[b, 0], xy[a, 1] - xy[b, 1]))
        if dist <= snap_tol_ft:
            continue
        na = end_nodes.iloc[a]
        nb = end_nodes.iloc[b]
        rows.append({
            "dist_ft":    round(dist, 2),
            "node_id_a":  int(na["node_id"]),
            "node_id_b":  int(nb["node_id"]),
            "role_a":     na["role"],
            "role_b":     nb["role"],
            "pipe_ids_a": na["pipe_ids"],
            "pipe_ids_b": nb["pipe_ids"],
            "geometry":   LineString([(na["x"], na["y"]), (nb["x"], nb["y"])]),
        })

    if not rows:
        return gpd.GeoDataFrame(
            columns=["dist_ft", "node_id_a", "node_id_b",
                     "role_a", "role_b", "pipe_ids_a", "pipe_ids_b", "geometry"],
            crs=nodes.crs,
        )
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=nodes.crs)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def write_node_layer(cfg: dict):
    """
    Load pipes, build node layer, flag snap gaps at end nodes, write outputs.

    Outputs (in order):
      1. pipe_endpoint_nodes.gpkg  — all nodes
      2. snap_gap_pairs.gpkg       — near-miss end node pairs (QC flags)
      3. end_nodes.gpkg            — start_only + end_only nodes only

    Returns (nodes_gdf, pairs_gdf, end_nodes_gdf).
    """
    params          = cfg["parameters"]
    snap_tol        = params["node_snap_tolerance_ft"]
    search_radius   = params.get("snap_gap_search_radius_ft", 10.0)
    crs             = params["crs"]
    outputs         = cfg["outputs"]

    node_path     = outputs.get("node_layer",    "output/pipe_endpoint_nodes.gpkg")
    pairs_path    = outputs.get("snap_gap_pairs","output/snap_gap_pairs.gpkg")
    end_node_path = outputs.get("end_nodes",     "output/end_nodes.gpkg")

    pipes = gpd.read_file(cfg["inputs"]["gravity_main_shapefile"]).to_crs(crs)

    # Pass search radius into build so snap gap repair happens before outputs
    nodes = build_node_layer(pipes, snap_tol, search_radius)
    _write_gpkg(nodes, node_path, "pipe_endpoint_nodes")

    pairs = find_snap_gap_flags(nodes, snap_tol, search_radius)
    _write_gpkg(pairs, pairs_path, "snap_gap_pairs")

    end_nodes = nodes[nodes["role"].isin(["start_only", "end_only"])].copy()
    end_nodes = end_nodes.reset_index(drop=True)
    _write_gpkg(end_nodes, end_node_path, "end_nodes")

    return nodes, pairs, end_nodes


def _write_gpkg(gdf: gpd.GeoDataFrame, path: str, layer: str) -> None:
    if path.endswith(".gpkg"):
        gdf.to_file(path, driver="GPKG", layer=layer)
    else:
        gdf.to_file(path)
