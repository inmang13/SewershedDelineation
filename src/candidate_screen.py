"""
Candidate sampling-site screening, Phase A — population and overlap filters only.

The lab needs new eDNA sampling sites. Some criteria a computer can decide
(served population, overlap with the existing 24 sewersheds); the physical ones
(in a roadway, in the woods, cart-haulable from parking) need a human looking at
imagery. This module does the automatable half and, critically, reports how many
*distinct branches* survive rather than how many manholes — a single sewer branch
holds many manholes in a row, each tracing a slightly larger version of the same
catchment, so a raw manhole list is massively redundant.

Why not just trace every node
-----------------------------
`traversal.traverse_upstream` per node is O(N^2) on a network this size. Instead
population is accumulated for every node in one topological pass (`accumulate_
upstream`). That accumulation double-counts wherever flow paths diverge and
reconverge, so it is used only as a *prefilter*: the accumulated value is always
>= the true upstream sum of the charged population, so no node is screened out on
account of the shortcut, and survivors then get an exact recompute
(`exact_upstream_pop`) over their real ancestor sets.

That guarantee covers the accumulation shortcut ONLY. It says nothing about the
underlying dasymetric charge, which measurably runs low against a real delineated
polygon (~0.94 of true on the 24 existing sites). The population floor must absorb
that separately — see the `candidate_sites.population_floor` comment in
config.yaml. Do not read "upper bound" as "safe to screen at the lab's literal
threshold"; those are different claims about different error sources.

Two distinct estimates, do not conflate them
--------------------------------------------
`upstream_pop_*` here is a dasymetric estimate of population served by the traced
pipes: census-block counts pushed onto residential parcels, each parcel charged to
its nearest main. It is NOT the population of a delineated sewershed polygon —
that number comes later from `demographics.demographics_for_site` on a real
boundary. This module's job is to get from ~50k nodes down to a reviewable list,
not to produce a publishable population.

Gravity-only, same caveat as the rest of the pipeline: the network is
gravity_mains with no force-main layer, so a subbasin reaching a node through an
upstream lift station is undercounted.
"""

import networkx as nx
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point


# ---------------------------------------------------------------------------
# Dasymetric parcel population
# ---------------------------------------------------------------------------

def parcel_population(res: gpd.GeoDataFrame,
                      blocks: gpd.GeoDataFrame,
                      pop_col: str = "P1_001N") -> pd.Series:
    """
    Push census-block population onto residential parcels by area share.

    Each block's population is split among the residential parcel area inside it:
    a parcel's share is (its area in the block) / (all residential area in the
    block). A block with no residential parcels contributes nothing to any parcel
    — its population is unplaceable, and silently spreading it over commercial or
    vacant land would invent households where none live. `population_placed` on
    the result records how much was placed so the caller can report the shortfall
    instead of hiding it.

    Returns a Series aligned to `res.index` (parcels with no overlapping block get
    0.0), carrying `.attrs["population_placed"]` and `.attrs["population_total"]`.

    A parcel spanning two blocks correctly receives a share from each.
    """
    if pop_col not in blocks.columns:
        raise KeyError(f"blocks layer has no population column '{pop_col}'")

    pop_total = float(pd.to_numeric(blocks[pop_col], errors="coerce").fillna(0).sum())

    res_g = res[["geometry"]].copy()
    res_g["_res_idx"] = res_g.index
    blk = blocks[["geometry", pop_col]].copy()
    blk["_blk_idx"] = blk.index
    blk["_blk_pop"] = pd.to_numeric(blk[pop_col], errors="coerce").fillna(0.0)

    # Intersect parcels with blocks; each row is one parcel-in-block piece.
    pieces = gpd.overlay(res_g, blk[["geometry", "_blk_idx", "_blk_pop"]],
                         how="intersection", keep_geom_type=False)
    if pieces.empty:
        out = pd.Series(0.0, index=res.index)
        out.attrs["population_placed"] = 0.0
        out.attrs["population_total"] = pop_total
        return out

    pieces["_area"] = pieces.geometry.area
    denom = pieces.groupby("_blk_idx")["_area"].transform("sum")
    # denom > 0 wherever a piece exists, so the divide is safe; guard anyway
    # against zero-area slivers produced by the overlay.
    share = np.where(denom > 0, pieces["_area"] / denom, 0.0)
    pieces["_pop"] = pieces["_blk_pop"] * share

    by_parcel = pieces.groupby("_res_idx")["_pop"].sum()
    out = by_parcel.reindex(res.index).fillna(0.0)
    out.attrs["population_placed"] = float(out.sum())
    out.attrs["population_total"] = pop_total
    return out


# ---------------------------------------------------------------------------
# Parcel -> pipe -> node
# ---------------------------------------------------------------------------

class LocalPop:
    """
    Population charged to each graph node, plus the accounting that proves none
    was invented or silently lost.

    by_node             node_id -> population charged to that node
    pop_charged         total placed on the network
    pop_unplaced        population on parcels with no main within the radius
    n_parcels_charged / n_parcels_unplaced   the same split by parcel count

    `pop_unplaced` is the honest one to read: a large value means the selection
    radius is missing served parcels, which would understate every catchment
    downstream of them.
    """

    def __init__(self, by_node, pop_charged, pop_unplaced,
                 n_parcels_charged, n_parcels_unplaced):
        self.by_node = by_node
        self.pop_charged = pop_charged
        self.pop_unplaced = pop_unplaced
        self.n_parcels_charged = n_parcels_charged
        self.n_parcels_unplaced = n_parcels_unplaced


def local_pop_by_node(G: nx.MultiDiGraph,
                      pipes: gpd.GeoDataFrame,
                      res: gpd.GeoDataFrame,
                      parcel_pop: pd.Series,
                      selection_radius_ft: float) -> LocalPop:
    """
    Charge each residential parcel's population to one graph node.

    A parcel is charged to its NEAREST gravity main within `selection_radius_ft`,
    and that pipe's population lands on the pipe's head (downstream) node — the
    first node whose upstream trace would pick the pipe up. Nearest-pipe rather
    than every-pipe-within-radius is deliberate: this is an accounting split, so
    each person must be charged exactly once or the accumulated totals inflate.

    Parcels with no main inside the radius are dropped, and the dropped total is
    reported on the result rather than absorbed.
    """
    pidx_to_head = {}
    for u, v, data in G.edges(data=True):
        p = data.get("pidx")
        if p is not None:
            pidx_to_head[int(p)] = v

    left = res[["geometry"]].copy()
    left["_pop"] = parcel_pop.reindex(res.index).fillna(0.0).values
    right = pipes[["geometry"]].copy()
    right["_pidx"] = np.arange(len(pipes))

    joined = gpd.sjoin_nearest(left, right, how="left",
                              max_distance=selection_radius_ft)
    # sjoin_nearest emits one row per tied nearest pipe; a tie must not multiply
    # the parcel's population, so keep the first match per parcel.
    joined = joined[~joined.index.duplicated(keep="first")]

    placed = joined["_pidx"].notna()
    local = {}
    n_charged = 0
    for pidx, pop in zip(joined.loc[placed, "_pidx"].astype(int),
                         joined.loc[placed, "_pop"]):
        node = pidx_to_head.get(pidx)
        if node is None:
            # Pipe matched but is absent from the graph (single-point geometry,
            # so build_graph skipped it). Counted as unplaced, same as an
            # out-of-range parcel — the parcel-count and population figures must
            # agree on what "unplaced" means or the accounting stops being a check.
            continue
        local[node] = local.get(node, 0.0) + float(pop)
        n_charged += 1

    charged = float(sum(local.values()))
    total = float(joined["_pop"].sum())
    return LocalPop(
        by_node=local,
        pop_charged=charged,
        pop_unplaced=total - charged,
        n_parcels_charged=n_charged,
        n_parcels_unplaced=len(joined) - n_charged,
    )


# ---------------------------------------------------------------------------
# One-pass upstream accumulation
# ---------------------------------------------------------------------------

class UpstreamPop:
    """
    Result of the one-pass accumulation.

    est               node_id -> accumulated upstream population INCLUDING the
                      node's own local population. An upper bound: exact on a
                      tree, inflated where flow diverges and reconverges.
    overcount_risk    node_id -> bool. True when a diverging node (out-degree > 1
                      in the condensed DAG) sits at or upstream of this node, so
                      `est` may double-count. Conservative: it flags every node
                      downstream of any divergence, whether or not the branches
                      actually reconverge.
    n_divergers       how many condensed nodes diverge at all. If this is ~0 the
                      network is dendritic and `est` is effectively exact.
    C                 the condensation DAG (SCCs collapsed).
    node_to_comp      node_id -> condensed node id.
    comp_local        condensed node id -> summed local population.
    """

    def __init__(self, est, overcount_risk, n_divergers, C, node_to_comp,
                 comp_local):
        self.est = est
        self.overcount_risk = overcount_risk
        self.n_divergers = n_divergers
        self.C = C
        self.node_to_comp = node_to_comp
        self.comp_local = comp_local


def accumulate_upstream(G: nx.MultiDiGraph, local_pop) -> UpstreamPop:
    """
    Accumulate upstream population for EVERY node in one topological pass.

    `local_pop` is a `LocalPop` or a plain node_id -> population dict.

    Strongly-connected components (the network's known direction-error loops) are
    condensed first, so a cycle behaves as a single lump rather than deadlocking
    the topological order. Every node inside an SCC therefore reports the same
    value — honest, since inside a direction-error loop there is no defensible
    upstream/downstream split.

    Returns an `UpstreamPop`. See that class for the over-count semantics; the
    short version is that `est` is an upper bound, which is what makes it a safe
    prefilter (no false negatives when screening on a population floor).
    """
    local = local_pop.by_node if isinstance(local_pop, LocalPop) else local_pop

    # Reachability only; collapse parallel edges so condensation gets a DiGraph.
    D = nx.DiGraph()
    D.add_nodes_from(G.nodes())
    D.add_edges_from((u, v) for u, v in G.edges() if u != v)

    C = nx.condensation(D)
    node_to_comp = {}
    comp_local = {}
    for c, members in C.nodes(data="members"):
        s = 0.0
        for n in members:
            node_to_comp[n] = c
            s += float(local.get(n, 0.0))
        comp_local[c] = s

    order = list(nx.topological_sort(C))

    # acc[c] = own local + everything upstream. Predecessors precede c in
    # topological order, so a single forward pass suffices.
    acc = {}
    diverger_up = {}
    for c in order:
        preds = list(C.predecessors(c))
        acc[c] = comp_local[c] + sum(acc[p] for p in preds)
        diverger_up[c] = any(diverger_up[p] or C.out_degree(p) > 1
                             for p in preds)

    n_divergers = sum(1 for c in C.nodes() if C.out_degree(c) > 1)

    est = {n: acc[node_to_comp[n]] for n in G.nodes()}
    risk = {n: diverger_up[node_to_comp[n]] for n in G.nodes()}
    return UpstreamPop(est, risk, n_divergers, C, node_to_comp, comp_local)


def exact_upstream_pop(up: UpstreamPop, nodes) -> dict:
    """
    Recompute upstream population exactly for a handful of nodes.

    Sums local population over the node's true ancestor set (plus itself) in the
    condensation, so a diamond contributes each person once. `nx.ancestors` per
    node is why this is for survivors only — running it over the whole network is
    the O(N^2) blowup the one-pass accumulation exists to avoid.

    Returns dict node_id -> exact population.
    """
    out = {}
    cache = {}
    for n in nodes:
        c = up.node_to_comp[n]
        if c not in cache:
            comps = nx.ancestors(up.C, c) | {c}
            cache[c] = sum(up.comp_local[x] for x in comps)
        out[n] = cache[c]
    return out


# ---------------------------------------------------------------------------
# Overlap with the existing sampling sites
# ---------------------------------------------------------------------------

def distance_to_existing(G: nx.MultiDiGraph, nodes, existing_boundaries):
    """
    Distance from each candidate node to the nearest existing sewershed POLYGON
    (0.0 when the node falls inside one).

    This exists because the graph test in `overlap_excluded_nodes` is not
    sufficient, and the asymmetry is easy to get backwards. Nesting implies
    overlap — true, and that is what the graph test catches. Overlap does NOT
    imply nesting: a delineated boundary is not the served-parcel union, since
    `boundary.build_boundary` adds Delaunay gap fill, `fill_uncovered_trace` adds a
    corridor along in-trace pipe, and `bridge_parts` closes gaps up to 800 ft. So an
    existing site's polygon can cover ground served by a main on a completely
    unrelated branch — graph says independent, geometry says overlapping. It fires
    on real data; see decision_log 2026-07-29 for the counts, which move with the
    population floor and are not restated here.

    Still only a POINT test — see `trace_pipes_in_existing` for the catchment one.

    Returns dict node_id -> distance_ft.
    """
    union = existing_boundaries.geometry.union_all()
    out = {}
    for n in nodes:
        d = G.nodes[n]
        out[n] = float(Point(d["x"], d["y"]).distance(union))
    return out


def trace_pipes_in_existing(G: nx.MultiDiGraph, pipes: gpd.GeoDataFrame, nodes,
                            existing_boundaries) -> dict:
    """
    Per node: how many of its traced pipes physically fall inside an existing
    sewershed. Returns dict node_id -> (n_in_existing, n_traced).

    The strongest of the three overlap tests, and the only one that measures what
    the lab actually asked for. `overlap_excluded_nodes` compares GRAPH relations;
    `distance_to_existing` compares the candidate POINT. Neither catches a
    candidate that sits comfortably clear but whose catchment reaches into an
    existing sampling area — a 500-person catchment spans thousands of feet, and
    when the intruding pipe is on an unrelated branch the graph cannot see it.
    It fires on real data — see decision_log 2026-07-29 for the count, which moves
    with the population floor and is deliberately not restated here.

    Traces with `traversal.traverse_upstream` and tests the in-trace pipe
    geometry, so it uses the same contributing-pipe set the production
    delineation would, minus the boundary construction. That is deliberate: an
    approximation of the catchment is enough to detect intrusion, and building
    real boundaries for every candidate is the Phase B cost this avoids.

    Intended for a SHORTLIST, not the whole network — it is one graph traversal
    per node. Screening the minimal set is sufficient, because a node downstream
    of an intruding pipe traces a superset and cannot become clean.
    """
    from traversal import traverse_upstream       # local: avoids a cycle at import

    union = existing_boundaries.geometry.union_all()
    out = {}
    for n in nodes:
        edges, _nodes, _depth = traverse_upstream(G, n)
        pidx = sorted({e["pidx"] for e in edges if e["pidx"] is not None})
        if not pidx:
            out[n] = (0, 0)
            continue
        sub = pipes.iloc[pidx]
        cand = list(sub.sindex.query(union, predicate="intersects"))
        n_in = int(sub.iloc[cand].intersects(union).sum()) if cand else 0
        out[n] = (n_in, len(pidx))
    return out


def overlap_excluded_nodes(G: nx.MultiDiGraph, existing_nodes) -> set:
    """
    Every node whose catchment NESTS inside or around an existing site's catchment.

    A node upstream of an existing site drains into it (its catchment is nested
    inside); a node downstream of one contains it. Either way the two sewersheds
    overlap, so this direction of the test is a graph relation — exact and free —
    with no polygon intersection needed.

    NOT SUFFICIENT ON ITS OWN. Nesting implies overlap; overlap does not imply
    nesting. Pair this with `distance_to_existing` — see that docstring for the
    failure it catches (4 real manholes) and why the graph cannot see it.

    Returns the union of ancestors, descendants and the existing nodes themselves.
    """
    excl = set()
    for n in existing_nodes:
        if n not in G:
            continue
        excl.add(n)
        excl |= nx.ancestors(G, n)
        excl |= nx.descendants(G, n)
    return excl



def classify_existing_relation(G: nx.MultiDiGraph, existing_nodes) -> dict:
    """
    Label each node by how its catchment relates to the existing sampling sites.

    Returns dict node_id -> one of:
      "is_existing"       the node IS an existing sampling site
      "contains_existing" downstream of one, so its catchment ENCOMPASSES an
                          already-sampled branch. This is the disqualifying case:
                          sampling here re-samples sewage the lab already covers,
                          plus everything around it.
      "inside_existing"   upstream of one, so its catchment is a sub-area of an
                          already-sampled branch. Overlapping, but not the same
                          problem — a smaller catchment inside a sampled one can
                          still be a legitimate finer-grained target. Labelled,
                          not excluded; that is Grace's call, not the tool's.
      "independent"       neither.

    Labels rather than filters, because "contains" and "inside" are different
    decisions and collapsing them into one exclusion set threw away information
    the reviewer needs. Only the graph relation is expressed here — see
    `distance_to_existing` and `trace_pipes_in_existing` for the geometric tests,
    which catch overlaps the graph cannot see.
    """
    rel = {n: "independent" for n in G.nodes()}
    for n in existing_nodes:
        if n not in G:
            continue
        for d in nx.descendants(G, n):
            rel[d] = "contains_existing"
        for a in nx.ancestors(G, n):
            # A node both upstream of one site and downstream of another keeps the
            # stronger label: encompassing an existing branch is disqualifying,
            # sitting inside one is not.
            if rel[a] != "contains_existing":
                rel[a] = "inside_existing"
    for n in existing_nodes:
        if n in G:
            rel[n] = "is_existing"
    return rel
