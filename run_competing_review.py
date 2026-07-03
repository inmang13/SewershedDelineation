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
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                              # noqa: E402
from graph_builder import load_graph_from_config, build_node_index  # noqa: E402
from traversal import trace_manhole, TargetResolutionError  # noqa: E402
from population_join import (                               # noqa: E402
    load_units, assign_population_units, competing_pipe_check,
)
from polygon_output import CP_SEVERITY, unit_id_column      # noqa: E402
from boundary import build_boundary                         # noqa: E402
from validation import load_truth, iou                      # noqa: E402

CSV_COLUMNS = [
    "tract", "manhole", "parcel", "severity", "cp_din", "cp_dout",
    "cp_fpipe", "cp_cross", "x", "y", "decision", "comment",
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
    base = cfg["_base_dir"]
    csv_path = base / outputs["competing_review_csv"]
    gpkg_path = base / outputs["competing_review_gpkg"]
    csv_path.parent.mkdir(exist_ok=True)

    print(f"Loading graph : {cfg['inputs']['gravity_main_shapefile']}")
    G, pipes = load_graph_from_config(cfg)
    index = build_node_index(G)
    parcels = load_units(cfg, "parcels")

    sites, points_xy = load_truth(cfg)
    tract_of = load_tract_labels(cfg)

    csv_rows, parcel_rows, boundary_rows, truth_rows, statuses = [], [], [], [], []
    print(f"\n{'tract':<16}{'manhole':<9}{'served':>7}{'contest':>8}"
          f"{'review':>7}{'warn':>6}{'iou':>7}")
    for sid in sorted(sites):
        tract = tract_of.get(sid, "")
        truth = sites[sid]
        truth_rows.append({"SiteID": sid, "tract": tract, "geometry": truth})

        # Guarded production resolver, driven by the sampling-point geometry
        # (geometry-first; also matches sites whose AssetID isn't a manhole id).
        x, y = points_xy[sid]
        cfg["inputs"]["manhole_id"] = None
        cfg["inputs"]["manhole_coordinate"] = [x, y]
        try:
            res = trace_manhole(G, cfg, index=index)
        except TargetResolutionError as e:
            statuses.append({"tract": tract, "manhole": sid,
                             "status": f"target resolution failed: {e}"})
            print(f"{tract:<16}{sid:<9}  TARGET RESOLUTION FAILED (IoU 0): {e}")
            continue
        if res.is_empty:
            statuses.append({"tract": tract, "manhole": sid,
                             "status": "headwater — no upstream pipes"})
            print(f"{tract:<16}{sid:<9}  headwater — no upstream pipes (IoU 0)")
            continue

        pop = assign_population_units(pipes, res.pidx_list, parcels, sel_r)
        ann = competing_pipe_check(pipes, res.pidx_list, pop.served, sel_r)
        id_col = unit_id_column(ann)

        served_union = None if pop.served.empty else pop.served.geometry.union_all()
        geom = build_boundary(pop.served, method,
                              served_union=served_union, close_ft=close_ft)
        score = round(iou(geom, truth), 4)
        if geom is not None and not geom.is_empty:
            boundary_rows.append({"SiteID": sid, "tract": tract, "iou": score,
                                  "method": method, "sel_r": sel_r,
                                  "close_ft": close_ft, "geometry": geom})

        hit = ann[ann["cp_flag"] != ""]
        n_rev = int((hit["cp_flag"] == "review").sum())
        print(f"{tract:<16}{sid:<9}{len(ann):>7}{len(hit):>8}"
              f"{n_rev:>7}{len(hit) - n_rev:>6}{score:>7.2f}")

        for idx, r in hit.iterrows():
            c = r.geometry.representative_point()
            rec = {
                "tract": tract,
                "manhole": sid,
                "parcel": str(r[id_col]) if id_col else str(idx),
                "severity": CP_SEVERITY[r["cp_flag"]],
                "cp_din": round(float(r["cp_din"]), 1),
                "cp_dout": (round(float(r["cp_dout"]), 1)
                            if pd.notna(r["cp_dout"]) else ""),
                "cp_fpipe": str(r["cp_fpipe"]),
                "cp_cross": int(r["cp_cross"]),
                "x": round(c.x, 2),
                "y": round(c.y, 2),
                "decision": "",
                "comment": "",
            }
            csv_rows.append(rec)
            parcel_rows.append({**{k: v for k, v in rec.items()
                                   if k not in ("decision", "comment")},
                                "geometry": r.geometry})

    # CSV — the review worksheet (decision: exclude | keep | reassign).
    pd.DataFrame(csv_rows, columns=CSV_COLUMNS).to_csv(
        csv_path, index=False, encoding="utf-8")

    # GeoPackage — same parcels spatially, plus context layers. Optional layers
    # are guarded: an all-clean run must not crash after the CSV is written.
    if parcel_rows:
        gpd.GeoDataFrame(parcel_rows, crs=crs).to_file(
            gpkg_path, layer="contested_parcels", driver="GPKG")
    if boundary_rows:
        gpd.GeoDataFrame(boundary_rows, crs=crs).to_file(
            gpkg_path, layer="boundary", driver="GPKG")
    gpd.GeoDataFrame(truth_rows, crs=crs).to_file(
        gpkg_path, layer="truth", driver="GPKG")

    n_rev = sum(r["severity"] == "review_required" for r in csv_rows)
    print(f"\nContested parcels: {len(csv_rows)} "
          f"({n_rev} review_required, {len(csv_rows) - n_rev} warning)")
    for s in statuses:
        print(f"  [skipped delineation] {s['tract']} {s['manhole']}: {s['status']}")
    print(f"Wrote: {csv_path}")
    print(f"       {gpkg_path} (layers: contested_parcels, boundary, truth)")
    print("\nReview: fill the `decision` column (exclude | keep | reassign) "
          "and add comments; a later pass consumes it.")


if __name__ == "__main__":
    main()
