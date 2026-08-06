"""
Phase 3b — wire confirmed force mains into the directed gravity graph.

The gravity graph stops dead at every lift station: flow enters the wet well and
the network ends. Everything upstream of that wet well really does reach the
force main's discharge point, but a gravity-only trace can never get there, so
the whole pumped basin is missing from the sewershed. This module closes that
gap by adding one directed edge per confirmed force-main system, from its
wet-well gravity node to its discharge gravity node.

What gets wired
---------------
Only components whose direction is settled (`resolved`), and only their
CONFIRMED contacts (`connected` / `facility`) — never a `candidate`. A candidate
is a proposal the reviewer has not accepted; wiring one would fabricate
connectivity, which is the exact failure the review file exists to prevent.

`terminates_at_facility` components are deliberately NOT wired. They end at a
treatment plant, which is terminal: no trace passes through a plant, so an edge
there would connect nothing while implying the plant has an outlet. They are
reported as skipped rather than omitted silently.

Force mains convey; they do not collect
---------------------------------------
A pressurized main has no service laterals — nothing discharges into it along
its length, so it contributes no population. A force-main edge therefore carries
`pidx=None`: it is connectivity only and must never reach the buffer/population
geometry Phase 5 builds from `pipes.iloc[pidx]`. `TraversalResult.pidx_list`
filters on that, and `layer == FM_LAYER` lets QA code exclude them from
gravity-digitization checks.

Why the edge list is a file
---------------------------
`run_force_mains.py` writes it; `graph_builder.load_graph_from_config` reads it.
Keeping the halves apart means the delineation pipeline doesn't re-run the whole
force-main classification on every trace, and the edge list is a reviewable
artifact rather than a hidden in-memory step.

The two halves are joined by COORDINATE, not by node or component id. Component
numbering shifts whenever the pipe set changes and node ids are regenerated on
every build, but a gravity node's location is physical and stable — the same
lesson `settle_reviewed_rows` learned matching decisions across runs.
"""

from pathlib import Path

import networkx as nx
import pandas as pd

from force_mains import (
    CONFIRMED_CONTACTS, VERDICT_RESOLVED, VERDICT_TERMINAL,
    _facilityid_by_cluster,
)

# Edge attribute marking a pressurized connectivity edge. Anything validating
# the gravity layer's digitization must exclude these — a force main is not a
# gravity pipe and reads as a direction error if treated as one.
FM_LAYER = "force_main"

EDGE_COLUMNS = [
    "comp_id", "wetwell_pipe", "discharge_pipe",
    "wetwell_x", "wetwell_y", "discharge_x", "discharge_y",
    "n_pipes", "length_ft", "wetwell_contact", "discharge_contact", "note",
]


def build_force_main_edges(termini: pd.DataFrame, verdicts: pd.DataFrame,
                           fm, topo) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build the wetwell -> discharge edge list for every settled component.

    Returns (edges, skipped). `skipped` is never empty in normal operation — the
    unresolved and terminates-at-facility components land there with a reason,
    so a run states what it did not wire instead of quietly wiring less than the
    reader expects.

    A `resolved` verdict already guarantees exactly one confirmed discharge and
    at least one confirmed wet well. That is re-checked here rather than trusted:
    the verdict rule and this function can drift apart, and wiring the wrong end
    would reverse a whole basin.
    """
    fid_by_cluster = _facilityid_by_cluster(fm, topo)
    len_by_comp   = dict(zip(verdicts.comp_id, verdicts.length_ft))
    npipe_by_comp = dict(zip(verdicts.comp_id, verdicts.n_pipes))
    verdict_by_comp = dict(zip(verdicts.comp_id, verdicts.verdict))

    rows, skipped = [], []

    def _skip(comp_id, reason):
        skipped.append({
            "comp_id":   comp_id,
            "verdict":   verdict_by_comp.get(comp_id, "?"),
            "n_pipes":   npipe_by_comp.get(comp_id, 0),
            "length_ft": len_by_comp.get(comp_id, 0.0),
            "reason":    reason,
        })

    confirmed = (termini[termini.contact.isin(CONFIRMED_CONTACTS)]
                 if len(termini) else termini)
    by_comp = ({c: g for c, g in confirmed.groupby("comp_id")}
               if len(confirmed) else {})

    for comp_id in verdicts.comp_id:
        verdict = verdict_by_comp.get(comp_id)

        if verdict == VERDICT_TERMINAL:
            # Correct to skip, but say so: a plant is the end of the line, so
            # there is nothing downstream for a trace to pass through.
            _skip(comp_id, "ends at a treatment plant — nothing traces through it")
            continue
        if verdict != VERDICT_RESOLVED:
            _skip(comp_id, f"direction not settled ({verdict}) — still in review")
            continue

        grp = by_comp.get(comp_id)
        if grp is None or grp.empty:
            _skip(comp_id, "no confirmed termini")
            continue

        disch = grp[grp.classification == "discharge"]
        wets  = grp[grp.classification == "wetwell"]
        if len(disch) != 1 or wets.empty:
            _skip(comp_id,
                  f"expected 1 confirmed discharge and >=1 wet well, found "
                  f"{len(disch)} and {len(wets)} — verdict and wiring disagree")
            continue

        d = disch.iloc[0]
        for w in wets.itertuples(index=False):
            if w.gravity_node == d.gravity_node:
                # Both ends of the station collapsed onto one gravity node. An
                # edge here would be a self-loop conveying nothing, and it means
                # the wet well and the discharge manhole snapped together.
                _skip(comp_id,
                      f"wet well and discharge are the same gravity node "
                      f"({w.gravity_node}) — station collapsed to one point "
                      f"[{fid_by_cluster.get(w.cluster, ('FM:?',))[0]}]")
                continue
            rows.append({
                "comp_id":           comp_id,
                "wetwell_pipe":      fid_by_cluster.get(w.cluster, ("FM:?", ""))[0],
                "discharge_pipe":    fid_by_cluster.get(d.cluster, ("FM:?", ""))[0],
                # The GRAVITY node coordinates, not the force-main terminus:
                # this is the point the edge must attach to.
                "wetwell_x":         round(float(w.gravity_x), 3),
                "wetwell_y":         round(float(w.gravity_y), 3),
                "discharge_x":       round(float(d.gravity_x), 3),
                "discharge_y":       round(float(d.gravity_y), 3),
                "n_pipes":           npipe_by_comp.get(comp_id, 0),
                "length_ft":         len_by_comp.get(comp_id, 0.0),
                "wetwell_contact":   w.contact,
                "discharge_contact": d.contact,
                "note":              "",
            })

    return (pd.DataFrame(rows, columns=EDGE_COLUMNS),
            pd.DataFrame(skipped, columns=["comp_id", "verdict", "n_pipes",
                                           "length_ft", "reason"]))


def load_force_main_edges(cfg: dict) -> pd.DataFrame | None:
    """
    Read the edge list named by `inputs.force_main_edges`, or None if unset.

    Returns None (not an empty frame) when the key is absent, so a gravity-only
    config is distinguishable from one whose force mains all failed review.
    """
    path = cfg.get("inputs", {}).get("force_main_edges")
    if not path:
        return None
    if not Path(path).exists():
        raise FileNotFoundError(
            f"inputs.force_main_edges is set but the file is missing — {path}. "
            "Run run_force_mains.py to generate it, or unset the key to build "
            "a gravity-only graph.")
    edges = pd.read_csv(path, encoding="utf-8-sig")
    missing = {"wetwell_x", "wetwell_y", "discharge_x",
               "discharge_y"} - set(edges.columns)
    if missing:
        raise ValueError(f"{path}: missing required column(s) {sorted(missing)}")
    return edges


def add_force_main_edges(G: nx.MultiDiGraph, edges: pd.DataFrame,
                         tol_ft: float = 10.0) -> pd.DataFrame:
    """
    Add one directed wet-well -> discharge edge per row, in place.

    Coordinates are resolved to graph nodes by nearest-node lookup within
    `tol_ft`. A coordinate that resolves to nothing raises rather than skipping:
    a silent skip drops an entire pumped basin out of every downstream trace with
    no signal that it happened, which is precisely the failure mode this project
    keeps hitting. The message carries the coordinate and the tolerance so the
    row can be found and fixed.

    Returns a log frame with the resolved node ids and snap distances.
    """
    from graph_builder import build_node_index, nearest_node

    if edges is None or edges.empty:
        return pd.DataFrame(columns=["comp_id", "u", "v", "snap_ft"])

    index = build_node_index(G)
    log = []
    for r in edges.itertuples(index=False):
        u, du = nearest_node(index, float(r.wetwell_x), float(r.wetwell_y))
        v, dv = nearest_node(index, float(r.discharge_x), float(r.discharge_y))
        for which, dist, x, y in (("wet well", du, r.wetwell_x, r.wetwell_y),
                                  ("discharge", dv, r.discharge_x, r.discharge_y)):
            if dist > tol_ft:
                raise ValueError(
                    f"force-main component {r.comp_id}: its {which} coordinate "
                    f"({x}, {y}) is {dist:.1f} ft from the nearest graph node, "
                    f"over the {tol_ft} ft tolerance. The gravity network moved "
                    "under this edge — re-run run_force_mains.py to rebuild the "
                    "edge list, or raise parameters.force_main_edge_snap_tol_ft.")
        if u == v:
            # Reachable even though build_force_main_edges filters it: the
            # gravity graph can change between writing and reading the file.
            continue
        G.add_edge(u, v,
                   pidx=None,                     # conveys, collects nothing
                   layer=FM_LAYER,
                   facilityid=str(getattr(r, "wetwell_pipe", "FM:?")),
                   comp_id=int(r.comp_id),
                   length_ft=float(getattr(r, "length_ft", 0.0) or 0.0),
                   slope=float("nan"),
                   up_invert=float("nan"),
                   dn_invert=float("nan"))
        log.append({"comp_id": int(r.comp_id), "u": u, "v": v,
                    "snap_ft": round(max(du, dv), 2)})
    return pd.DataFrame(log, columns=["comp_id", "u", "v", "snap_ft"])


def cycles_through_force_mains(G: nx.MultiDiGraph) -> pd.DataFrame:
    """
    Find directed cycles that a force-main edge closes.

    A clean gravity network is a forest, so any strongly-connected component
    containing a force-main edge means that edge made flow loop back on itself —
    the discharge drains to its own wet well. That is either a wrong direction
    call or a gravity direction error underneath it, and it inflates every trace
    passing through the loop.

    Asking "which SCC contains a force-main edge?" is narrower and more useful
    than diffing SCC counts before and after: the gravity graph already has its
    own SCCs, and a count can stay flat while membership changes. This names the
    force main to look at.
    """
    rows = []
    for scc in nx.strongly_connected_components(G):
        if len(scc) < 2:
            continue
        sub = G.subgraph(scc)
        for u, v, d in sub.edges(data=True):
            if d.get("layer") == FM_LAYER:
                rows.append({
                    "comp_id":    d.get("comp_id"),
                    "facilityid": d.get("facilityid"),
                    "u": u, "v": v,
                    "scc_nodes":  len(scc),
                })
    return pd.DataFrame(rows, columns=["comp_id", "facilityid", "u", "v",
                                       "scc_nodes"])
