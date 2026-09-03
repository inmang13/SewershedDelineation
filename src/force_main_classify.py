"""
Force-main integration, Phase 3 — classify termini against the directed
gravity graph.

Types every force-main component terminus as a wet well or discharge using
the out-degree rule (see `force_mains.py` module docstring), then rolls those
classifications up to a per-component verdict. See `force_mains.py` for the
phase overview and the shared flag/verdict vocabulary.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
import networkx as nx
from scipy.spatial import cKDTree

from graph_builder import nearest_node
from force_mains import CONFIRMED_CONTACTS, _read_csv_rows
from force_main_topology import ForceMainTopology


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
        d, si = station_tree.query([r.x, r.y])
        if d > station_tol_ft:
            continue

        # False-positive guard: a short discharge stub sitting near a station
        # is only a misread if it plausibly belongs to THAT station. If this
        # terminus's own component already has a facility-confirmed wet well
        # anchored to a DIFFERENTLY NAMED station, the nearby station is
        # coincidence, not the system's own outlet, and downgrading here would
        # delete a real discharge instead of catching a fake one. Caught
        # 2026-08-11: adding Celeste Circle Lift Station flagged FM:2766
        # (component 34) this way, even though that component's real wet well
        # is a confirmed station 0.84 mi away — nothing to do with Celeste
        # Circle. When there is no facility-named wet well yet (the inference
        # rule alone, as at East End and Geer St), this guard does not apply
        # and the original behaviour holds.
        station_name = (stations["name"].iloc[int(si)]
                        if "name" in stations.columns else None)
        comp_wetwells = out[(out.comp_id == r.comp_id)
                            & (out.classification == "wetwell")
                            & (out.contact.isin(CONFIRMED_CONTACTS))]
        if station_name is not None and not comp_wetwells.empty \
                and "facility_name" in comp_wetwells.columns:
            named = comp_wetwells[comp_wetwells.facility_name.astype(str)
                                  .str.strip() != ""]
            if not named.empty and not (named.facility_name == station_name).any():
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
    discharge and at least one connected wet well. A loop in the geometry does
    NOT block it — see the note above VERDICT_DIRECTION_UNRESOLVED in
    `force_mains.py`. Everything else names *why* it failed, so the review file
    sorts by the work each component needs. The failure vocabulary is
    `VERDICT_UNRESOLVED`, defined in `force_mains.py` with a one-line gloss
    each — kept there rather than listed here so it cannot drift out of step
    with the code that tests against it.
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
