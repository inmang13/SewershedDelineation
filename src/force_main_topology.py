"""
Force-main integration, Phase 2 — force-main-only topology.

Builds the undirected graph of the filtered force-main layer against itself
(before any contact with the gravity network), clusters coincident endpoints,
and prunes small dangling stubs. See `force_mains.py` for the phase overview
and the shared flag/verdict vocabulary.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import networkx as nx
from scipy.spatial import cKDTree

from node_layer import _make_uf, _find, _union
from graph_builder import nearest_node
from force_mains import _read_csv_rows


class ForceMainTopology:
    """
    Undirected force-main topology, before any contact with the gravity network.

    Attributes
    ----------
    graph        nx.MultiGraph over endpoint-cluster ids; each edge carries
                 `pidx` (positional index into the filtered force-main frame).
                 Multi, not simple, for the reason `graph_builder` gives for
                 using a MultiDiGraph: dual force mains out of one lift station
                 are standard practice, and a simple graph would drop the second
                 one's `pidx` — undercounting pipes and length, and hiding the
                 cycle that makes the component's interior direction ambiguous.
    cluster      (2*n_pipes,) int — cluster id per endpoint, start-then-end
    xy           dict cluster_id -> (x, y) representative coordinate
    components   list[set[int]] of cluster ids, one per connected component
    """

    def __init__(self, graph, cluster, xy, components):
        self.graph = graph
        self.cluster = cluster
        self.xy = xy
        self.components = components

    def termini(self, comp: set) -> list[int]:
        """
        Free-end cluster ids in a component — its candidate wet wells/discharges.

        A free end is a cluster with exactly one DISTINCT neighbour, not one of
        degree 1. On a MultiGraph those differ, and the difference is the very
        case MultiGraph exists for: two parallel mains ending at one cluster give
        it degree 2, so a degree test would silently drop a real free end.
        Self-loops are excluded from the neighbour set for the same reason — a
        loop back to the same cluster is not a connection onward.
        """
        sub = self.graph.subgraph(comp)
        return [n for n in comp if len(set(sub.neighbors(n)) - {n}) == 1]

    def n_cycles(self, comp: set) -> int:
        """
        Independent cycles in one component (E - V + 1). A component with cycles
        cannot be oriented unambiguously by walking outward from its discharge,
        so this is a review signal, not a statistic. `number_of_edges` counts
        parallel mains individually on a MultiGraph, so a dual force main
        correctly reads as one cycle rather than none.
        """
        sub = self.graph.subgraph(comp)
        return sub.number_of_edges() - sub.number_of_nodes() + 1


def prune_small_stubs(fm: gpd.GeoDataFrame, topo: "ForceMainTopology",
                      node_index, small_diameter_in: float,
                      max_component_ft: float, contact_tol_ft: float
                      ) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """
    Drop small-diameter force mains that dangle, keeping the ones that don't.

    Small-diameter mains are a mix. Most private ones are grinder-pump laterals
    serving a single building and are excluded by owner. The city-owned small
    mains are different: on the city's network, 19 of 20 belong to real systems
    (108-1221 ft, sitting exactly on the gravity network) and one of them,
    00243, is the only main serving the Geer St lift station. A flat diameter cut
    threw all 20 away.

    So the test is not size, it's whether the pipe goes anywhere. A component is
    a stub when it is BOTH shorter than `max_component_ft` in total AND has no
    terminus within `contact_tol_ft` of the gravity network. Both conditions
    matter: a short component that touches gravity is a legitimate connector,
    and a long component that touches nothing is a snap gap to investigate, not
    litter to discard.

    The test is deliberately at COMPONENT level, not pipe level. Pipe 2467 is
    24.5 ft long and would fail any per-pipe length rule, but it is one segment
    of a healthy 909 ft nine-pipe system. This is the same lesson the review pass
    produced: pipe length alone does not identify a dangling stub.

    Only components whose pipes are ALL below `small_diameter_in` are considered,
    so behaviour for normal-sized mains is unchanged.

    Returns (kept_pipes, dropped_report). Callers must rebuild the topology from
    `kept_pipes` — the positional indices this function filters on do not survive
    it.
    """
    dia = pd.to_numeric(fm["DIAMETER"], errors="coerce")
    drop_pidx, rows = set(), []

    for comp_id, comp in enumerate(topo.components):
        pidx = [d["pidx"] for _, _, d in topo.graph.subgraph(comp).edges(data=True)]
        if not pidx or not (dia.iloc[pidx] < small_diameter_in).all():
            continue

        length_ft = float(fm.geometry.iloc[pidx].length.sum())
        if length_ft >= max_component_ft:
            continue

        termini = topo.termini(comp)
        gaps = [nearest_node(node_index, *topo.xy[t])[1] for t in termini]
        if gaps and min(gaps) <= contact_tol_ft:
            continue   # short, but it does attach — a real connector

        drop_pidx.update(pidx)
        rows.append({
            "comp_id":   comp_id,
            "n_pipes":   len(pidx),
            "length_ft": round(length_ft, 1),
            "min_gap_ft": round(min(gaps), 1) if gaps else float("nan"),
            "facilityids": ", ".join(
                sorted(fm["FACILITYID"].astype(str).iloc[pidx])),
        })

    keep = fm.drop(index=fm.index[sorted(drop_pidx)]).reset_index(drop=True)
    report = pd.DataFrame(rows, columns=[
        "comp_id", "n_pipes", "length_ft", "min_gap_ft", "facilityids"])
    return keep, report


def load_manual_joins(cfg: dict) -> list[dict]:
    """
    Human-confirmed repairs to breaks INSIDE the pressurized network.

    Read from `inputs.force_main_joins` (optional; missing file returns []).
    Columns: x, y, radius_ft, and any number of ignored annotation columns.
    Every endpoint cluster within radius_ft of (x, y) is merged.

    These are not junctions to the gravity network — they are two force mains
    that should be one and are drawn a few feet apart. They matter more than
    their size suggests: a break inside the pressurized network splits one
    system into two components, and each half then reports a spurious
    `no_discharge` or `unconnected_only` verdict. Repairing intra-layer
    connectivity BEFORE asking about inter-layer junctions dissolves questions
    rather than answering them (review finding, 2026-08-01).
    """
    path = cfg["inputs"].get("force_main_joins")
    if not path or not Path(path).exists():
        return []
    joins = []
    for lineno, row in enumerate(_read_csv_rows(path), start=2):
        try:
            joins.append({"x": float(row["x"]), "y": float(row["y"]),
                          "radius_ft": float(row.get("radius_ft") or 0) or 5.0})
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                f"{path}, line {lineno}: force-main join rows need numeric x and y "
                f"(and optional radius_ft) — got {dict(row)!r}") from None
    return joins


def build_topology(fm: gpd.GeoDataFrame, tol_ft: float,
                   manual_joins: list[dict] | None = None) -> ForceMainTopology:
    """
    Cluster force-main endpoints within `tol_ft` and build the undirected
    force-main graph. Uses the same union-find primitives as
    `node_layer.snap_endpoints` so "coincident endpoint" means the same thing
    here as it does for gravity mains.

    Pass 1 merges endpoints within `tol_ft`. Pass 2 applies `manual_joins` —
    human-confirmed repairs to breaks inside the pressurized network, mirroring
    `snap_endpoints` pass 3. A join that reaches fewer than two distinct clusters
    raises rather than silently merging nothing, for the same reason
    `snap_endpoints` does: a repair that quietly no-ops is worse than a loud one.

    A self-loop pipe (both endpoints in one cluster) is kept as an edge — it is
    a QC signal, and dropping it would silently change component structure.
    """
    if fm.empty:
        # Same early return `node_layer.snap_endpoints`, `build_node_layer` and
        # `find_snap_gap_flags` all make: an empty frame is an empty topology,
        # not a cKDTree shape error.
        return ForceMainTopology(nx.MultiGraph(), np.empty(0, dtype=int), {}, [])

    coords = []
    for geom in fm.geometry:
        pts = list(geom.coords)
        coords.append(pts[0][:2])
        coords.append(pts[-1][:2])
    coords = np.asarray(coords, dtype=float)

    tree = cKDTree(coords)
    parent = _make_uf(len(coords))
    for i, j in tree.query_pairs(tol_ft):
        _union(parent, i, j)

    # Pass 2 — human-confirmed intra-network repairs.
    for join in (manual_joins or []):
        hits = tree.query_ball_point([join["x"], join["y"]], r=join["radius_ft"])
        if len({_find(parent, i) for i in hits}) < 2:
            raise ValueError(
                f"force-main join at ({join['x']}, {join['y']}) with radius "
                f"{join['radius_ft']} ft reaches fewer than two distinct endpoint "
                "clusters — it would merge nothing. Widen radius_ft or fix the "
                "coordinate.")
        for i in hits[1:]:
            _union(parent, hits[0], i)

    cluster = np.array([_find(parent, i) for i in range(len(coords))], dtype=int)

    xy = {}
    for i, cid in enumerate(cluster):
        xy.setdefault(int(cid), (float(coords[i, 0]), float(coords[i, 1])))

    G = nx.MultiGraph()
    G.add_nodes_from(int(c) for c in cluster)
    for pidx in range(len(fm)):
        G.add_edge(int(cluster[2 * pidx]), int(cluster[2 * pidx + 1]), pidx=pidx)

    return ForceMainTopology(G, cluster, xy,
                             [set(c) for c in nx.connected_components(G)])
