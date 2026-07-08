"""
CLI runner: competing-pipe review batch (QC round 1 item 2, step 3).

Runs the production delineation (configured selection radius + boundary method)
for every validation site and collects the per-parcel competing-pipe flags into
one reviewable pair of files (paths from config outputs):

    QC/competing_pipe_review.csv    one row per contested parcel, with blank
                                    `decision` / `comment` columns for Grace to
                                    fill (same feedback pattern as
                                    QC/qa_review_decisions.csv; x/y carried so
                                    a consuming pass can proximity-match if a
                                    parcel id ever drifts)
    QC/competing_pipe_review.gpkg   layers:
                                      contested_parcels  parcel polygons + cp_* metrics
                                      boundary           final boundary per site (+ IoU)
                                      truth              hand-drawn truth polygons

The check is flag-only: nothing is excluded here. Exclusion (or reassignment)
happens in a later pass that consumes the filled-in decision column.

Sites and target coordinates come from the validation layers (truth SiteID ==
point AssetID_tx, geometry-first snap). Target resolution goes through
resolve_target_node — the guarded production resolver (false-headwater guard),
NOT the sweep's bare nearest_node — so a site the sweep scored may fail
resolution here; such sites (and headwaters) still get a CSV/printout row with
a status instead of vanishing.

Run:  python run_competing_review.py --config config.yaml
"""

import argparse
import statistics
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                              # noqa: E402
from graph_builder import (                                  # noqa: E402
    load_graph_from_config, build_node_index, small_dangling_pidx,
)
from traversal import trace_manhole, TargetResolutionError  # noqa: E402
from population_join import (                               # noqa: E402
    load_units, assign_population_units, competing_pipe_check,
    split_border_contested, assign_remaining_by_buffer,
)
from polygon_output import CP_SEVERITY, unit_id_column      # noqa: E402
from boundary import build_boundary                         # noqa: E402
from validation import load_truth, iou                      # noqa: E402

CSV_COLUMNS = [
    "tract", "manhole", "parcel", "severity", "cp_pos", "cp_din", "cp_dout",
    "cp_fpipe", "cp_owner", "cp_cross", "cp_keep", "cp_asgn", "x", "y",
    "decision", "comment",
]


def load_tract_labels(cfg) -> dict:
    """AssetID_tx -> Tract label from the sampling-points layer, for readability."""
    pts = gpd.read_file(cfg["inputs"]["validation_points"])
    return {str(r["AssetID_tx"]).strip(): str(r["Tract"]).strip()
            for _, r in pts.iterrows()}


def main():
    ap = argparse.ArgumentParser(description="Competing-pipe review batch over all validation sites.")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    params = cfg["parameters"]
    outputs = cfg["outputs"]
    crs = params["crs"]
    sel_r = params["selection_radius_ft"]
    method = params["boundary_method"]
    close_ft = params["close_radius_ft"]
    max_edge_ft = params.get("delaunay_max_edge_ft", 1000.0)
    base = cfg["_base_dir"]
    csv_path = base / outputs["competing_review_csv"]
    gpkg_path = base / outputs["competing_review_gpkg"]
    csv_path.parent.mkdir(exist_ok=True)

    print(f"Loading graph : {cfg['inputs']['gravity_main_shapefile']}")
    G, pipes = load_graph_from_config(cfg)
    index = build_node_index(G)
    parcels = load_units(cfg, "parcels")

    # Small dangling networks (isolated ≤N-pipe stubs) are ignored by the
    # competing check — a 2-3 pipe fragment crossing a parcel is noise, not a
    # rival network. Computed once; the pipes stay in the graph and traces.
    ignore_pidx = small_dangling_pidx(
        G, params.get("competing_ignore_dangling_max_pipes", 3))
    print(f"Ignoring {len(ignore_pidx)} pipes in dangling networks "
          f"(<={params.get('competing_ignore_dangling_max_pipes', 3)} pipes) "
          "for the competing check")

    sites, points_xy = load_truth(cfg)
    tract_of = load_tract_labels(cfg)

    # Pre-pass: trace every site once, cache the results, and build a map from
    # each pipe FACILITYID to the set of tracts whose trace contains it. A
    # contested parcel's crossing pipe is then labelled with the neighbouring
    # site it belongs to (cp_owner) — turning "unknown foreign main" into
    # "reassign to Tract X". A blank cp_owner means the crossing pipe is in no
    # sampled trace (a genuinely non-sampled main to investigate). General, not
    # the city-specific: it just cross-references the traces produced this run.
    traces, statuses = {}, []
    pidx_owner: dict = {}
    for sid in sorted(sites):
        tract = tract_of.get(sid, "")
        x, y = points_xy[sid]
        cfg["inputs"]["manhole_id"] = None
        cfg["inputs"]["manhole_coordinate"] = [x, y]
        try:
            res = trace_manhole(G, cfg, index=index)
        except TargetResolutionError as e:
            statuses.append({"tract": tract, "manhole": sid,
                             "status": f"target resolution failed: {e}"})
            continue
        if res.is_empty:
            statuses.append({"tract": tract, "manhole": sid,
                             "status": "headwater — no upstream pipes"})
            continue
        traces[sid] = res
        # Key by positional pipe index, not FACILITYID: FACILITYID is null/
        # non-unique here, so an attribute join could credit the wrong tract.
        for pidx in res.pidx_list:
            pidx_owner.setdefault(int(pidx), set()).add(tract)

    def owner_of(fpidx, own_tract: str) -> str:
        """Tracts (other than this one) whose trace contains pipe index `fpidx`."""
        if fpidx is None or int(fpidx) < 0:
            return ""
        return "; ".join(sorted(pidx_owner.get(int(fpidx), set()) - {own_tract}))

    csv_rows, parcel_rows, boundary_rows, truth_rows = [], [], [], []
    iou_pairs = []   # (score_before, score_after all rules) per delineated site
    split_total = 0
    asgn_keep_total = asgn_excl_total = 0
    print(f"\n{'tract':<16}{'manhole':<9}{'served':>7}{'contest':>8}"
          f"{'review':>7}{'warn':>6}{'excl':>6}{'splt':>5}{'akp':>5}{'axc':>5}"
          f"{'iou0':>7}{'iou':>7}")
    for sid in sorted(sites):
        tract = tract_of.get(sid, "")
        truth = sites[sid]
        truth_rows.append({"SiteID": sid, "tract": tract, "geometry": truth})

        res = traces.get(sid)
        if res is None:
            # already recorded in statuses during the pre-pass
            msg = next((s["status"] for s in statuses if s["manhole"] == sid), "no trace")
            print(f"{tract:<16}{sid:<9}  {msg} (IoU 0)")
            continue

        pop = assign_population_units(pipes, res.pidx_list, parcels, sel_r)

        def _score(units):
            u = None if units.empty else units.geometry.union_all()
            g = build_boundary(units, method, served_union=u, close_ft=close_ft,
                               delaunay_max_edge_ft=max_edge_ft)
            return g, round(iou(g, truth), 4)

        full_geom, score0 = _score(pop.served)   # pre-exclude/split IoU (iou0)
        ann = competing_pipe_check(
            pipes, res.pidx_list, pop.served, sel_r,
            border_ring_ft=params.get("border_ring_ft", 75.0),
            border_min_expose=params.get("border_min_expose", 0.10),
            border_min_cover_frac=params.get("border_min_cover_frac", 0.0),
            border_min_cover_area_ft2=params.get("border_min_cover_area_ft2", 0.0),
            ignore_pidx=ignore_pidx)
        id_col = unit_id_column(ann)

        # Drop the auto-excluded (cp_excl) border+din>0 units, then equidistant-
        # split the border+din==0 units (keep only the in-trace-closer piece),
        # rebuild the boundary, re-score.
        kept = ann[ann["cp_excl"] == 0]
        split = split_border_contested(kept, pipes, res.pidx_list, sel_r, cfg,
                                       ignore_pidx=ignore_pidx)
        # Resolve the still-open contested units by buffered-pipe area (label
        # only), then drop the exclude-assigned ones before building the boundary.
        assigned = assign_remaining_by_buffer(split, pipes, res.pidx_list, cfg,
                                              ignore_pidx=ignore_pidx)
        for_boundary = assigned[assigned["cp_asgn"] != "exclude"]
        geom, score = _score(for_boundary)
        n_excl = int((ann["cp_excl"] == 1).sum())
        n_asgn_excl = int((assigned["cp_asgn"] == "exclude").sum())
        n_asgn_keep = int((assigned["cp_asgn"] == "keep").sum())
        if geom is not None and not geom.is_empty:
            boundary_rows.append({"SiteID": sid, "tract": tract, "iou": score,
                                  "method": method, "sel_r": sel_r,
                                  "close_ft": close_ft, "geometry": geom})

        # Which units the splitter touched: kept-fraction (0.0 = split to empty
        # and dropped). Keyed by the unit id so we can flag them in the row loop
        # and count them (split-to-empty units are dropped from `split`, so
        # counting `cp_keep < 1` alone would miss them).
        split_keep = {}
        if id_col and "cp_keep" in split.columns:
            for uid, kv in zip(split[id_col].astype(str), split["cp_keep"]):
                if kv < 1.0:
                    split_keep[uid] = float(kv)
            targ = kept[(kept["cp_pos"] == "border") & (kept["cp_cross"] == 1)
                        & (kept["cp_din"] == 0)]
            live = set(split[id_col].astype(str))
            for uid in targ[id_col].astype(str):
                if uid not in live:              # split to empty -> fully dropped
                    split_keep[uid] = 0.0
        n_split = len(split_keep)

        # Buffered-pipe assignment per unit id (keep / exclude), for the row loop.
        asgn_map = {}
        if id_col and "cp_asgn" in assigned.columns:
            for uid, a in zip(assigned[id_col].astype(str), assigned["cp_asgn"]):
                if a:
                    asgn_map[uid] = a

        iou_pairs.append((score0, score))
        split_total += n_split
        asgn_keep_total += n_asgn_keep
        asgn_excl_total += n_asgn_excl
        hit = ann[ann["cp_flag"] != ""]
        n_rev = int((hit["cp_flag"] == "review").sum())
        print(f"{tract:<16}{sid:<9}{len(ann):>7}{len(hit):>8}"
              f"{n_rev:>7}{len(hit) - n_rev:>6}{n_excl:>6}{n_split:>5}"
              f"{n_asgn_keep:>5}{n_asgn_excl:>5}{score0:>7.2f}{score:>7.2f}")

        for idx, r in hit.iterrows():
            c = r.geometry.representative_point()
            pid = str(r[id_col]) if id_col else str(idx)
            auto = int(r["cp_excl"]) == 1
            was_split = pid in split_keep
            asgn = asgn_map.get(pid, "")
            keepfrac = split_keep.get(pid, 0.0 if auto else 1.0)
            if auto:
                decision, comment = "exclude", ("auto: border parcel, foreign pipe "
                                                "crosses, no in-trace pipe touches")
            elif was_split:
                decision = "split"
                comment = (f"auto: equidistant split, kept {keepfrac*100:.0f}% "
                           "(in-trace side)")
            elif asgn == "exclude":
                decision, comment = "exclude", ("auto: buffered-pipe area — a "
                                                "foreign pipe's buffer covers more")
            elif asgn == "keep":
                decision, comment = "keep", ("auto: buffered-pipe area — an "
                                             "in-trace pipe's buffer covers more")
            else:
                decision, comment = "", ""
            rec = {
                "tract": tract,
                "manhole": sid,
                "parcel": pid,
                "severity": CP_SEVERITY[r["cp_flag"]],
                "cp_pos": str(r["cp_pos"]),
                "cp_din": round(float(r["cp_din"]), 1),
                "cp_dout": (round(float(r["cp_dout"]), 1)
                            if pd.notna(r["cp_dout"]) else ""),
                "cp_fpipe": str(r["cp_fpipe"]),
                "cp_owner": owner_of(r.get("cp_fpidx"), tract),
                "cp_cross": int(r["cp_cross"]),
                "cp_keep": round(keepfrac, 2),
                "cp_asgn": asgn,
                "x": round(c.x, 2),
                "y": round(c.y, 2),
                # Auto decisions are pre-filled so Grace audits/overrides rather
                # than re-deciding each; blank rows are still hers to fill.
                "decision": decision,
                "comment": comment,
            }
            csv_rows.append(rec)
            # contested_parcels holds the parcels still IN the shed (not excluded,
            # not split into a partial piece) — i.e. decision blank or "keep" — so
            # the GIS layer shows the kept/undecided parcels with their cp_asgn for
            # audit. Excluded and split units live in the CSV record only.
            if decision not in ("exclude", "split"):
                parcel_rows.append({**{k: v for k, v in rec.items()
                                       if k not in ("decision", "comment")},
                                    "geometry": r.geometry})

    # CSV — the review worksheet (decision: exclude | keep | reassign).
    pd.DataFrame(csv_rows, columns=CSV_COLUMNS).to_csv(
        csv_path, index=False, encoding="utf-8")

    # GeoPackage — the OPEN review parcels spatially (auto-excluded units are in
    # the CSV only), plus context layers. Optional layers are guarded: an
    # all-clean run must not crash after the CSV is written.
    if parcel_rows:
        gpd.GeoDataFrame(parcel_rows, crs=crs).to_file(
            gpkg_path, layer="contested_parcels", driver="GPKG")
    if boundary_rows:
        gpd.GeoDataFrame(boundary_rows, crs=crs).to_file(
            gpkg_path, layer="boundary", driver="GPKG")
    gpd.GeoDataFrame(truth_rows, crs=crs).to_file(
        gpkg_path, layer="truth", driver="GPKG")

    n_rev = sum(r["severity"] == "review_required" for r in csv_rows)
    n_border_excl = sum(r["decision"] == "exclude" for r in csv_rows) \
        - asgn_excl_total
    print(f"\nContested parcels: {len(csv_rows)} "
          f"({n_rev} review_required, {len(csv_rows) - n_rev} warning)")
    print(f"Border auto-excluded (foreign-cross, no in-trace touch): "
          f"{n_border_excl}")
    print(f"Equidistant-split (border + both pipes cross, cp_din==0): "
          f"{split_total} units kept partial")
    print(f"Buffered-pipe assignment (remaining contested): "
          f"{asgn_keep_total} kept, {asgn_excl_total} excluded")
    print("(excluded + split parcels are in the CSV record only, dropped from "
          "the contested_parcels layer)")
    if iou_pairs:
        med0 = statistics.median(s0 for s0, _ in iou_pairs)
        med1 = statistics.median(s1 for _, s1 in iou_pairs)
        ge0 = sum(s1 >= 0.5 for _, s1 in iou_pairs)
        print(f"Median IoU: {med0:.4f} before -> {med1:.4f} after "
              f"exclude+split+buffer-assign ({ge0}/{len(iou_pairs)} sites >= 0.5)")
    for s in statuses:
        print(f"  [skipped delineation] {s['tract']} {s['manhole']}: {s['status']}")
    print(f"Wrote: {csv_path}")
    print(f"       {gpkg_path} (layers: contested_parcels, boundary, truth)")
    print("\nReview: fill the `decision` column (exclude | keep | reassign) "
          "and add comments; a later pass consumes it.")


if __name__ == "__main__":
    main()
