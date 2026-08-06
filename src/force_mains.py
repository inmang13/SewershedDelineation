"""
Force-main (pressurized main) ingest, topology and direction inference.

The gravity pipeline stops at a force-main discharge point, so every subbasin
that reaches a node through a lift station is missing from the sewershed. This
module is the first half of closing that gap: it reads the pressurized-main
layer, works out how it *would* attach to the gravity network, and reports every
junction it proposes — **without changing any topology**. Nothing here is wired
into `graph_builder`; a run produces a review file and a QC GeoPackage, and a
human decides what is real before the graph ever sees a force main.

Why direction has to be inferred
--------------------------------
The layer ships no FROMMH/TOMH, no invert elevations, and its Z values are 0.0
on 6297 of 6303 vertices — unusable. Geometry direction is not a documented
convention for pressurized mains the way it is for the gravity layer, so it
cannot be trusted either.

What *is* available is the already-directed gravity graph. A pump station is
where the gravity network ends (flow enters the wet well and stops), and a
discharge manhole is a gravity node that still has flow leaving it — very often
a `start_only` headwater that appears to start from nowhere precisely because a
force main feeds it. So:

    terminus on a gravity node with out_degree == 0  ->  wet well  (suction)
    terminus on a gravity node with out_degree  > 0  ->  discharge

Measured on the city's layer, this resolves ~78% of the components that touch
gravity at two ends into exactly one wet well and one discharge. The rest are
reported as ambiguous rather than guessed at. See
`docs/force_main_integration_plan.md` for the validation numbers and for the
open request to the city for a pump-station point layer, which would replace
this inference with a direct lookup.

Output contract
---------------
`QC/force_main_review.csv` deliberately uses the same columns as
`QC/qa_review_decisions.csv` (`flag_type, pipe_id, x, y, decision, radius_ft,
target, comment`) plus diagnostic columns. A reviewer sets `decision=snap` on
the rows they accept; those rows then feed `node_layer.snap_endpoints` pass 3
through the existing `qa_review.manual_snaps` route, so force mains join the one
shared topology instead of getting a parallel snap path of their own. Extra
columns are ignored by `qa_review.load_review_decisions`.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import networkx as nx
from shapely import force_2d
from shapely.geometry import Point, LineString
from scipy.spatial import cKDTree

from node_layer import _make_uf, _find, _union
from graph_builder import nearest_node


# Review-file flag types. Named so they cannot collide with the gravity flag
# types already in QC/qa_review_decisions.csv.
FLAG_JUNCTION   = "fm_junction_candidate"
FLAG_NO_CONTACT = "fm_no_gravity_contact"
FLAG_AMBIGUOUS  = "fm_direction_ambiguous"

# A "discharge" that is really the far side of a pump station. Some stations
# are drawn as a wet well manhole joined by a few feet of gravity pipe to a
# second manhole that continues downstream — the out-degree rule sees flow
# continuing past that point and calls it a discharge, when the whole thing is
# still inside the station and there is no real second connection. Confirmed
# 4 times in the 2026-08-02/03 map review (East End, Geer St x2, a stub pipe
# by Geer St): every case was a short incident pipe sitting right next to a
# confirmed station. See docs/force_main_ai_review_trends.md.
FLAG_STATION_ADJACENT = "fm_station_adjacent_discharge"

# Component verdicts, defined once. `review_rows` and any future consumer read
# the sets below rather than re-listing strings — the vocabulary drifted between
# the docstring and a hard-coded literal the first time it lived in two places.
VERDICT_RESOLVED = "resolved"

# The component reaches a confirmed treatment plant. Like `resolved` this needs
# no review — the network is supposed to end there (see `terminal_facilities`).
VERDICT_TERMINAL = "terminates_at_facility"
VERDICT_SETTLED = {VERDICT_RESOLVED, VERDICT_TERMINAL}

# Terminus `contact` values that count as a real attachment when rolling up a
# component verdict. "connected" is a within-tolerance snap; "facility" is a
# confirmed treatment plant or pump station, which is stronger evidence, not
# weaker. "candidate" and "none" are neither.
CONFIRMED_CONTACTS = ("connected", "facility")

# Attachment failures: the component cannot be placed on the gravity network at
# all, so direction never comes up. These are junction problems.
VERDICT_UNATTACHED = {
    "isolated",          # no terminus reaches gravity within the review radius
    "unconnected_only",  # contacts exist, but only as unaccepted candidates
}

# Direction failures: the component attaches, but the wet-well/discharge rule
# could not settle it. These are what `fm_direction_ambiguous` marks.
VERDICT_DIRECTION_UNRESOLVED = {
    "no_discharge",      # touches gravity, but every contact is a terminal sink
    "no_wetwell",        # a discharge, but no terminal sink to pump from
    "multi_discharge",   # two or more contacts have downstream gravity
    "cyclic",            # direction resolvable at the ends, but the interior loops
}

VERDICT_UNRESOLVED = VERDICT_UNATTACHED | VERDICT_DIRECTION_UNRESOLVED

# Widest gap that gets a pre-filled merge radius in the review file. Past this,
# a pre-filled radius is a footgun, not a convenience — see `review_rows`.
MAX_PREFILLED_RADIUS_GAP_FT = 25.0

# Inclusion defaults, applied when parameters.force_main_include is absent or
# only partially specified. Both repos' config.yaml set these explicitly; these
# values are the fallback so the module is usable without a config, matching how
# `graph_builder` defaults `snap_gap_search_radius_ft`.
DEFAULT_INCLUDE = {
    "lifecycle":       ["Active"],
    "exclude_owner":   ["PVT"],
    "min_diameter_in": 4.0,
    # Case-insensitive substrings matched against COMMENT. A bypass port is a
    # connection point for portable pumps during maintenance — it carries no
    # routine flow, so leaving it in fabricates connectivity that only exists
    # when a crew is on site. Meter bypasses are deliberately NOT excluded: a
    # meter bypass carries real flow when the meter is offline, so it is live
    # conveyance. Each pattern's hit count is reported separately so a pattern
    # that silently matches nothing is visible.
    "exclude_comment": ["bypass port", "by-pass port", "bypass pumping",
                        "by-pass pumping", "bypass pump pipe",
                        "emergency bypass"],
    # Individual FACILITYIDs a reviewer has confirmed are wrong/redundant and
    # should be dropped outright (e.g. a duplicate stub inside a station that
    # the direction rule keeps misreading as a discharge). Distinct from
    # small-stub pruning: this is a per-pipe human call, not a rule.
    "exclude_facilityid": [],
}


# ---------------------------------------------------------------------------
# Phase 1 — load and filter
# ---------------------------------------------------------------------------

def load_force_mains(cfg: dict) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """
    Read the force-main layer, reproject to the working CRS, drop Z, and apply
    the inclusion filter.

    Returns (kept, drop_report). `drop_report` has one row per exclusion reason
    with a count, so a run can state what it threw away instead of silently
    shrinking the network. A DIAMETER of 0 is missing data, not a small pipe —
    it fails `min_diameter_in` and is reported under its own reason, and each
    `exclude_comment` pattern gets its own reason for the same purpose.

    Raises FileNotFoundError if `inputs.force_main_shapefile` is set but absent.
    Callers handle the "key not set" case themselves (gravity-only mode).
    """
    path = cfg["inputs"].get("force_main_shapefile")
    if not path:
        raise ValueError("inputs.force_main_shapefile is not set")
    if not Path(path).exists():
        raise FileNotFoundError(f"force main layer not found — {path}")

    rules = dict(DEFAULT_INCLUDE)
    rules.update(cfg["parameters"].get("force_main_include") or {})

    fm = gpd.read_file(path).to_crs(cfg["parameters"]["crs"])
    fm["geometry"] = force_2d(fm.geometry)   # Z is all zeros; carrying it breaks KD-trees
    n_total = len(fm)

    reasons = []

    def _drop(predicate, reason: str) -> None:
        """
        Record and apply one exclusion. `predicate` takes the SURVIVING frame and
        returns a boolean mask — a callable rather than a mask because each drop
        rebinds `fm`, so a mask built earlier would be indexed against a frame
        that no longer exists.
        """
        nonlocal fm
        mask = predicate(fm)
        n = int(mask.sum())
        if n:
            reasons.append({"reason": reason, "n": n,
                            "facilityids": ", ".join(
                                sorted(fm.loc[mask, "FACILITYID"].astype(str))[:20])})
        fm = fm.loc[~mask].copy()

    def _diameter(frame):
        return pd.to_numeric(frame["DIAMETER"], errors="coerce")

    lifecycle = rules.get("lifecycle")
    if lifecycle:
        _drop(lambda f: ~f["LIFECYCLES"].isin(lifecycle),
              f"lifecycle not in {lifecycle}")

    exclude_owner = rules.get("exclude_owner")
    if exclude_owner:
        _drop(lambda f: f["OWNER"].isin(exclude_owner),
              f"owner in {exclude_owner}")

    min_dia = rules.get("min_diameter_in")
    if min_dia is not None:
        # DIAMETER = 0 gets its own reason rather than folding into the size cut:
        # a zero is missing data, and reporting it as "too small" would hide a
        # data-quality problem behind a modelling decision.
        _drop(lambda f: _diameter(f).isna(), "DIAMETER missing/non-numeric")
        _drop(lambda f: _diameter(f) == 0,
              "DIAMETER = 0 (missing data, not a small pipe)")
        _drop(lambda f: _diameter(f) < min_dia,
              f"DIAMETER < {min_dia} in (private grinder lateral)")

    # Comment exclusions run per pattern, not as one combined regex, so the drop
    # report attributes each removal to the pattern that caused it — a pattern
    # matching zero rows is then obvious rather than silently inert.
    for pattern in (rules.get("exclude_comment") or []):
        _drop(lambda f, p=pattern: f["COMMENT"].fillna("").str.contains(
                  p, case=False, regex=False),
              f"COMMENT contains '{pattern}'")

    exclude_fids = rules.get("exclude_facilityid")
    if exclude_fids:
        fid_set = {str(f) for f in exclude_fids}
        _drop(lambda f: f["FACILITYID"].astype(str).isin(fid_set),
              f"FACILITYID in {sorted(fid_set)} (reviewer-confirmed drop)")

    _drop(lambda f: f.geometry.is_empty | f.geometry.isna(), "empty geometry")

    fm = fm.reset_index(drop=True)
    report = pd.DataFrame(reasons, columns=["reason", "n", "facilityids"])
    report.attrs["n_total"] = n_total
    report.attrs["n_kept"] = len(fm)
    return fm, report


def to_geopackage(shapefile_path: str, gpkg_path: str, crs: str) -> int:
    """
    Convert the delivered shapefile to a GeoPackage matching the other `data/`
    layers (and leaving the ArcGIS `.sr.lock` / `.shp.xml` clutter behind).
    Returns the feature count written. No filtering happens here — the raw layer
    is preserved so the inclusion rule stays a pipeline decision, not a
    baked-in one.
    """
    gdf = gpd.read_file(shapefile_path).to_crs(crs)
    gdf["geometry"] = force_2d(gdf.geometry)
    Path(gpkg_path).parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(gpkg_path, driver="GPKG")
    return len(gdf)


# ---------------------------------------------------------------------------
# Phase 2 — force-main-only topology
# ---------------------------------------------------------------------------

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


def _read_csv_rows(path):
    """
    Read a review CSV, tolerating what Excel does to it on a Windows machine.

    Excel writes cp1252, and a curly apostrophe typed in a comment field then
    makes a utf-8 read die mid-file. A review loop that cannot survive Excel is
    not a review loop, so fall back rather than fail.
    """
    import csv
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=encoding) as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path}: could not decode as utf-8 or cp1252")


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


# ---------------------------------------------------------------------------
# Phase 3 — classify termini against the directed gravity graph
# ---------------------------------------------------------------------------

def classify_termini(topo: ForceMainTopology, G: nx.MultiDiGraph, node_index,
                     snap_tol_ft: float, review_radius_ft: float) -> pd.DataFrame:
    """
    For every component terminus, find the nearest gravity node and classify it.

    A terminus within `snap_tol_ft` is treated as a real contact and typed by
    the gravity node's out-degree (see the module docstring). A terminus between
    `snap_tol_ft` and `review_radius_ft` is reported as a candidate but NOT
    accepted — that is the reviewer's call. Beyond `review_radius_ft` it is
    reported as having no gravity contact at all.

    Returns one row per terminus with: comp_id, cluster, x, y, gravity_node,
    dist_ft, gravity_role, in_degree, out_degree, contact, classification.
    """
    rows = [_classify_cluster(cid, comp_id, topo, G, node_index,
                              snap_tol_ft, review_radius_ft)
            for comp_id, comp in enumerate(topo.components)
            for cid in topo.termini(comp)]
    return pd.DataFrame(rows, columns=TERMINUS_COLUMNS)


TERMINUS_COLUMNS = [
    "comp_id", "cluster", "x", "y", "gravity_node", "dist_ft",
    "gravity_role", "in_degree", "out_degree", "gravity_x", "gravity_y",
    "contact", "classification",
]


def _classify_cluster(cid: int, comp_id: int, topo: ForceMainTopology,
                      G: nx.MultiDiGraph, node_index,
                      snap_tol_ft: float, review_radius_ft: float) -> dict:
    """
    Classify ONE endpoint cluster against the gravity graph.

    Shared by `classify_termini` and `add_station_junction_termini` so a station
    outlet is typed by exactly the same rule as a free end — two code paths here
    would drift, and the difference would be a silently mistyped wet well.
    """
    x, y = topo.xy[cid]
    gnode, dist = nearest_node(node_index, x, y)

    if dist <= snap_tol_ft:
        contact = "connected"
        cls = ("discharge" if G.out_degree(gnode) > 0 else "wetwell")
    elif dist <= review_radius_ft:
        contact = "candidate"
        cls = ("discharge?" if G.out_degree(gnode) > 0 else "wetwell?")
    else:
        contact = "none"
        cls = "no_gravity_contact"

    return {
        "comp_id":      comp_id,
        "cluster":      cid,
        "x":            x,
        "y":            y,
        "gravity_node": int(gnode),
        "dist_ft":      round(dist, 2),
        "gravity_role": G.nodes[gnode]["role"],
        "in_degree":    int(G.in_degree(gnode)),
        "out_degree":   int(G.out_degree(gnode)),
        "gravity_x":    float(G.nodes[gnode]["x"]),
        "gravity_y":    float(G.nodes[gnode]["y"]),
        "contact":      contact,
        "classification": cls,
    }


def add_station_junction_termini(termini: pd.DataFrame,
                                 topo: ForceMainTopology, G: nx.MultiDiGraph,
                                 node_index, facilities,
                                 station_tol_ft: float, snap_tol_ft: float,
                                 review_radius_ft: float
                                 ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Recognise a pump-station outlet where two force mains leave one point.

    `ForceMainTopology.termini` counts a cluster as a free end only when exactly
    one distinct pipe touches it. A station with dual force mains breaks that:
    two mains leave one point, so the outlet has two neighbours and is not a
    terminus — even though it is unmistakably where the system starts. The
    facility matcher only sees termini, so it skips the real outlet and matches
    whatever free end is nearest instead. At Lick Creek that free end was 125 ft
    away in an unrelated stretch of the network, and the mismatch surfaced as a
    `facility_direction_conflict` that no coordinate fix could clear.

    Dual force mains out of a station are standard design, not a defect (Grace,
    2026-08-06: "we see a couple of these near LS and they should be considered
    harmless"). So a confirmed station sitting closer to a force-main junction
    than to any free end pins the wet well AT that junction.

    Only fires when the junction is strictly closer than the nearest free end —
    where the matcher already has a good answer, nothing changes.

    Returns (termini, added). `added` is the log; empty means no station showed
    this shape.
    """
    log_cols = ["station", "cluster", "comp_id", "dist_ft", "n_mains",
                "classification", "nearest_free_end_ft"]
    if facilities is None or len(facilities) == 0 or termini.empty:
        return termini, pd.DataFrame(columns=log_cols)
    stations = facilities[facilities.role == "station"]
    if stations.empty:
        return termini, pd.DataFrame(columns=log_cols)

    # Every cluster, its component, and how many distinct mains touch it.
    comp_of, n_nbrs = {}, {}
    for comp_id, comp in enumerate(topo.components):
        sub = topo.graph.subgraph(comp)
        for c in comp:
            comp_of[c] = comp_id
            n_nbrs[c] = len(set(sub.neighbors(c)) - {c})

    clusters = sorted(n_nbrs)
    xy = np.array([topo.xy[c] for c in clusters])
    tree = cKDTree(xy)
    existing = set(termini.cluster.tolist())

    rows, added = [], []
    for s in stations.itertuples(index=False):
        sx, sy = float(s.geometry.x), float(s.geometry.y)
        near = tree.query_ball_point([sx, sy], r=station_tol_ft)
        if not near:
            continue
        d = {i: float(np.hypot(xy[i][0] - sx, xy[i][1] - sy)) for i in near}
        free = [i for i in near if clusters[i] in existing]
        junc = [i for i in near if n_nbrs[clusters[i]] >= 2
                and clusters[i] not in existing]
        if not junc:
            continue
        j = min(junc, key=d.get)
        nearest_free = min((d[i] for i in free), default=float("inf"))
        if d[j] >= nearest_free:
            continue                    # the matcher already has a better answer
        cid = clusters[j]
        if cid in existing:
            continue                    # another station already claimed it
        row = _classify_cluster(cid, comp_of[cid], topo, G, node_index,
                                snap_tol_ft, review_radius_ft)
        # This row exists ONLY because a confirmed station sits closer to it
        # than to any free end, so the station IS the evidence — a point where
        # the mains of a known pump station converge is that station's outlet,
        # which is the wet well by definition. Letting the out-degree rule type
        # it instead re-raises the Geer St mistake: gravity continuing past a
        # station's own manhole made it read as a discharge, and every such row
        # came back as a false conflict for Grace to adjudicate one at a time.
        # The inferred reading is kept alongside, so the disagreement is
        # auditable rather than erased.
        row["inferred_classification"] = row["classification"]
        row["classification"] = "wetwell"
        row["contact"] = "facility"
        row["station_junction"] = True
        rows.append(row)
        existing.add(cid)
        added.append({
            "station":  s.name,
            "cluster":  cid,
            "comp_id":  comp_of[cid],
            "dist_ft":  round(d[j], 1),
            "n_mains":  n_nbrs[cid],
            "classification": row["inferred_classification"],
            "nearest_free_end_ft": (round(nearest_free, 1)
                                    if np.isfinite(nearest_free) else None),
        })

    if not rows:
        return termini, pd.DataFrame(columns=log_cols)
    out = pd.concat(
        [termini, pd.DataFrame(rows,
                               columns=TERMINUS_COLUMNS
                               + ["inferred_classification", "station_junction"])],
        ignore_index=True)
    out["station_junction"] = out.get(
        "station_junction", pd.Series(False, index=out.index)).fillna(False)
    # Carry any columns later steps added (station_adjacent, facility_*) so the
    # appended rows do not read as NaN-flagged.
    for col, fill in (("station_adjacent", False), ("facility_conflict", False),
                      ("facility_name", ""), ("facility_role", "")):
        if col in out.columns:
            out[col] = out[col].fillna(fill)
    return out, pd.DataFrame(added, columns=log_cols)


def load_direction_overrides(cfg: dict) -> list[dict]:
    """
    Read `inputs.force_main_direction_overrides` — a reviewer's ruling on which
    end of a system is the pump station.

    Columns: x, y, classification[, comment]. Returns [] when unset or absent.
    """
    path = cfg.get("inputs", {}).get("force_main_direction_overrides")
    if not path or not Path(path).exists():
        return []
    out = []
    for row in _read_csv_rows(path):
        cls = (row.get("classification") or "").strip()
        if not cls:
            continue
        if cls not in ("wetwell", "discharge", "terminal"):
            raise ValueError(
                f"{path}: classification '{cls}' is not one of "
                "wetwell / discharge / terminal.")
        out.append({"x": float(row["x"]), "y": float(row["y"]),
                    "classification": cls,
                    "comment": (row.get("comment") or "").strip()})
    return out


def apply_direction_overrides(termini: pd.DataFrame, overrides: list[dict],
                              tol_ft: float = 25.0
                              ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Let a reviewer overrule the out-degree rule at a named terminus.

    The direction rule reads a terminus as a discharge whenever gravity still
    flows out of the node it lands on. At a lift station whose wet-well manhole
    also passes a gravity main through it, that reading is wrong, and no amount
    of tuning fixes it — the evidence genuinely points both ways. `flag_station_
    adjacent_discharges` catches the sub-case where the giveaway is a short stub;
    this handles the rest, where only a person looking at the site can tell.

    That is also how a `facility_direction_conflict` gets closed out.
    `terminal_facilities.apply_to_termini` deliberately refuses to flip a
    contradiction on its own — it flags and moves on — so something has to carry
    the human's answer, and this is it.

    Matched by COORDINATE within `tol_ft`, for the same reason every other
    decision file in this pipeline is: component ids and row order both move
    between runs, a location does not. An override that matches nothing RAISES:
    it means the geometry moved out from under a ruling, and silently ignoring a
    reviewer's decision is worse than stopping.
    """
    log_cols = ["x", "y", "was", "now", "comment"]
    if not overrides:
        return termini, pd.DataFrame(columns=log_cols)
    if termini.empty:
        raise ValueError("direction overrides are configured but there are no "
                         "force-main termini to apply them to.")

    out = termini.copy()
    tree = cKDTree(np.c_[out.x.astype(float), out.y.astype(float)])
    rows = []
    for o in overrides:
        hits = tree.query_ball_point([o["x"], o["y"]], r=tol_ft)
        if not hits:
            raise ValueError(
                f"direction override at ({o['x']}, {o['y']}) matched no "
                f"force-main terminus within {tol_ft} ft. The pipe set changed "
                "under this ruling — check the coordinate against the current "
                "QC/force_main_review.csv, or remove the row.")
        # Nearest only: two termini of one station can sit within tolerance of
        # each other, and flipping both would invent a second wet well.
        j = min(hits, key=lambda k: (out.x.iloc[k] - o["x"]) ** 2
                                  + (out.y.iloc[k] - o["y"]) ** 2)
        i = out.index[j]
        rows.append({"x": o["x"], "y": o["y"],
                     "was": out.at[i, "classification"],
                     "now": o["classification"],
                     "comment": o["comment"]})
        out.at[i, "classification"] = o["classification"]
        # A human ruling is at least as strong as a facility match, so the
        # terminus counts as attached in component_verdicts. Without this a
        # `candidate` contact would stay uncounted and the verdict would not move.
        out.at[i, "contact"] = "facility"
        if "facility_conflict" in out.columns:
            out.at[i, "facility_conflict"] = False
    return out, pd.DataFrame(rows, columns=log_cols)


def flag_station_adjacent_discharges(termini: pd.DataFrame, topo: ForceMainTopology,
                                     fm: gpd.GeoDataFrame, facilities,
                                     max_pipe_ft: float, station_tol_ft: float
                                     ) -> pd.DataFrame:
    """
    Catch the station-adjacent-discharge misread before it reaches a verdict.

    A terminus that reads "discharge" is trusted as a real connection whenever
    its contact is "connected" — that is what lets a component resolve with no
    review. But the misread this function targets produces exactly that
    signature: a short force-main stub sitting right beside a confirmed pump
    station, discharging into a gravity junction that only continues downstream
    because of the station's own outlet pipe. Left alone, a component built
    entirely from this kind of terminus reads as `resolved` and never gets a
    second look — East End (component 63, one confirmed wetwell plus this exact
    misread) did precisely that until the 2026-08-02/03 map review caught it by
    eye. This closes that gap algorithmically instead of depending on someone
    looking at every "resolved" system by hand.

    A terminus qualifies when ALL of:
      - classification starts with "discharge" (not a wetwell reading);
      - contact is "connected" (a candidate/no-contact row already gets review,
        so there is nothing to catch there);
      - the pipe incident to this terminus is `<= max_pipe_ft` long — the
        signature in every confirmed case, never seen on a real trunk discharge;
      - a STATION facility sits within `station_tol_ft` of the terminus.

    Qualifying rows have `contact` downgraded to "candidate" (out of
    `CONFIRMED_CONTACTS`, so `component_verdicts` no longer counts them as a
    real discharge) and gain `station_adjacent=True`, which `review_rows` reads
    to explain the specific misread rather than a generic ambiguity. Returns
    the updated termini plus a small report of what was caught, for the
    runner to print.
    """
    out = termini.copy()
    out["station_adjacent"] = False
    if out.empty or facilities is None or facilities.empty:
        return out, pd.DataFrame(columns=["pipe_id", "pipe_len_ft", "dist_ft"])

    stations = facilities[facilities.role == "station"]
    if stations.empty:
        return out, pd.DataFrame(columns=["pipe_id", "pipe_len_ft", "dist_ft"])

    station_tree = cKDTree(np.c_[stations.geometry.x, stations.geometry.y])

    # Incident pipe length per terminus cluster: a terminus is a free end, so
    # exactly the edges touching it (parallel duplicates included) are its
    # local pipe(s) — take the shortest, since even one short stub is enough
    # to produce the misread.
    pipe_len = {}
    for comp in topo.components:
        sub = topo.graph.subgraph(comp)
        for cid in topo.termini(comp):
            lens = [fm.geometry.iloc[d["pidx"]].length
                    for _, _, d in sub.edges(cid, data=True)]
            pipe_len[cid] = min(lens) if lens else float("inf")

    rows = []
    is_discharge = out.classification.astype(str).str.startswith("discharge")
    is_connected = out.contact == "connected"
    for i in out[is_discharge & is_connected].index:
        r = out.loc[i]
        if pipe_len.get(r.cluster, float("inf")) > max_pipe_ft:
            continue
        d, _ = station_tree.query([r.x, r.y])
        if d > station_tol_ft:
            continue
        out.at[i, "contact"] = "candidate"
        out.at[i, "station_adjacent"] = True
        pidx = min((d2["pidx"] for _, _, d2 in
                   topo.graph.subgraph(topo.components[r.comp_id]).edges(
                       r.cluster, data=True)),
                  key=lambda p: fm.geometry.iloc[p].length, default=None)
        rows.append({
            "pipe_id": (f"FM:{fm['FACILITYID'].iloc[pidx]}"
                       if pidx is not None else "?"),
            "pipe_len_ft": round(pipe_len.get(r.cluster, 0), 1),
            "dist_ft": round(float(d), 1),
        })

    return out, pd.DataFrame(rows, columns=["pipe_id", "pipe_len_ft", "dist_ft"])


def component_verdicts(topo: ForceMainTopology, termini: pd.DataFrame,
                       fm: gpd.GeoDataFrame) -> pd.DataFrame:
    """
    Roll terminus classifications up to a per-component verdict.

    `resolved` means the direction rule succeeded: exactly one connected
    discharge and at least one connected wet well, and no cycles to make the
    interior orientation ambiguous. Everything else names *why* it failed, so
    the review file sorts by the work each component needs. The failure
    vocabulary is `VERDICT_UNRESOLVED`, defined at module top with a one-line
    gloss each — kept there rather than listed here so it cannot drift out of
    step with the code that tests against it.
    """
    rows = []
    by_comp = {c: g for c, g in termini.groupby("comp_id")} if len(termini) else {}
    for comp_id, comp in enumerate(topo.components):
        grp = by_comp.get(comp_id)
        pidx = [d["pidx"] for _, _, d in topo.graph.subgraph(comp).edges(data=True)]
        length_ft = float(fm.geometry.iloc[pidx].length.sum()) if pidx else 0.0
        cycles = topo.n_cycles(comp)

        if grp is None or grp.empty:
            n_disch = n_wet = n_cand = 0
        else:
            # "facility" is a confirmed contact, not a weaker one — a lift
            # station pins the wet well more firmly than a 10 ft snap does.
            # Omitting it here silently turned confirmed wet wells into
            # `no_wetwell` verdicts (caught 2026-08-02).
            conn = grp[grp.contact.isin(CONFIRMED_CONTACTS)]
            n_disch = int((conn.classification == "discharge").sum())
            n_wet   = int((conn.classification == "wetwell").sum())
            n_cand  = int((grp.contact == "candidate").sum())

        # A component reaching a confirmed treatment plant is finished, not
        # broken: "no_discharge" would report the correct answer as a failure.
        # Checked first because it outranks every other reading of the component.
        n_terminal = 0 if grp is None or grp.empty else \
            int((grp.classification == "terminal").sum())

        if n_terminal:
            verdict = "terminates_at_facility"
        elif n_disch == 0 and n_wet == 0:
            verdict = "unconnected_only" if n_cand else "isolated"
        elif n_disch == 0:
            verdict = "no_discharge"
        elif n_disch > 1:
            verdict = "multi_discharge"
        elif cycles > 0:
            verdict = "cyclic"
        elif n_wet == 0:
            verdict = "no_wetwell"
        else:
            verdict = "resolved"

        rows.append({
            "comp_id":     comp_id,
            "n_pipes":     len(pidx),
            "n_clusters":  len(comp),
            "n_termini":   len(topo.termini(comp)),
            "n_discharge": n_disch,
            "n_wetwell":   n_wet,
            "n_candidate": n_cand,
            "n_cycles":    cycles,
            "length_ft":   round(length_ft, 1),
            "verdict":     verdict,
        })
    return pd.DataFrame(rows, columns=[
        "comp_id", "n_pipes", "n_clusters", "n_termini", "n_discharge",
        "n_wetwell", "n_candidate", "n_cycles", "length_ft", "verdict"])


# ---------------------------------------------------------------------------
# Phase 4 — review file and QC geometry
# ---------------------------------------------------------------------------

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
        "cyclic":        "the pipes in this system form a loop, so I can't tell "
                         "which way flow runs through it.",
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
