"""
Phase 8 — boundary-method validation sweep.

Scores each candidate boundary method (src/boundary.py) against the 25-site
hand-drawn truth set and reports which (method, selection_radius, close/ratio)
best reproduces the manual delineations by IoU.

Truth data (in the sibling CommunityWastewaterDashboard project):
  Sampling_Polygons_05212026.shp   24 truth polygons, key SiteID
  Sampling_Locations_05212026.shp  25 sampling points, key AssetID_tx
Join is truth SiteID == location AssetID_tx (same id space as manhole FACILITYID;
the points layer has no FACILITYID field). The point *geometry* — not the id — is
snapped to the graph node, consistent with the geometry-first approach. The join
is asserted to yield ~24 matched pairs; a mismatch aborts loudly rather than
producing an all-garbage IoU table.

Scoring rules (decision_log 2026-07-01, amended 2026-07-02):
  - IoU = intersection.area / union.area of generated vs truth polygon.
  - No site exclusions. The old 30804 DROP ("pumped site, gravity trace can't
    reproduce it") was a wrong premise: a lift station at the sampling point is
    a terminal end — everything upstream is gravity-fed and traces normally.
    The old 02201/03442 FLAGs were a SiteID label rotation in the truth
    shapefile, fixed at the source 2026-07-02.
  - Winner = highest median IoU subject to the IoU floor (median >= T1 AND
    >= N sites at IoU >= 0.5). T1/N are set by the user AFTER seeing this table —
    the sweep prints the table and STOPS for that input; it does not auto-finalize.

Leave-one-out validation (--loo):
  The sweep winner's median IoU is tuned and scored on the same ~21 aggregate
  sites, so it is optimistically biased. LOO re-aggregates the per-site rows:
  hold out one aggregate site, pick the best combo by median IoU over the other
  20 (tie-break: more sites at IoU >= 0.5, then combo order), then score the
  held-out site at that combo. Repeat for all sites; the median of the held-out
  scores is the honest generalization number to quote alongside the in-sample
  median. The IoU floor is NOT applied inside folds — its site-count term
  (>= N of the ~21 aggregate sites) doesn't translate to a 20-site fold; fold
  selection mirrors only the "highest median" part of the production rule. No geometry is recomputed:
  --loo reuses output/validation_sweep.csv (or fresh rows when run as
  --sweep --loo).

Run:  python run_validation.py --sweep      (thin runner at repo root)
  or  python src/validation.py --sweep      (src is on sys.path when run directly)
  or  python run_validation.py --loo        (re-aggregate the saved sweep CSV)
"""

import argparse
import statistics
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

from config import load_config
from graph_builder import load_graph_from_config, build_node_index, nearest_node
from traversal import traverse_upstream
from population_join import load_units, assign_population_units
from boundary import build_boundary

# Sites excluded from the aggregate (median / floor). Kept in the printed table.
# 2026-07-02: all prior exclusions removed. The FLAG_SITES (02201/03442) zero
# overlap was a SiteID label rotation in the truth shapefile, fixed at the
# source. The 30804 DROP rested on a wrong premise: a lift station at the
# SAMPLING POINT is a terminal end of a gravity basin — everything upstream of
# it is gravity-fed, so the upstream trace treats it like any other point.
# A pump only matters if a delineation would have to trace THROUGH it (a force
# main mid-basin), which the upstream trace never does.
DROP_SITES = set()
FLAG_SITES = set()
EXCLUDE_FROM_AGGREGATE = DROP_SITES | FLAG_SITES

# Default sweep grids (override on the CLI with comma lists).
DEFAULT_SEL_GRID = [50.0, 100.0, 150.0, 200.0]     # selection_radius_ft
DEFAULT_CLOSE_GRID = [50.0, 100.0, 150.0]          # close_radius_ft (morph_close, hybrid)
DEFAULT_RATIO_GRID = [0.1, 0.3, 0.5]               # concave_hull ratio (concave)

# Which unit layer each method selects, and which second parameter it sweeps.
METHOD_UNIT = {
    "morph_close": "parcels",
    "blocks_dissolve": "blocks",
    "hybrid": "parcels",
    "concave": "parcels",
}


def _norm_id(v) -> str:
    """Normalize a site id to a stripped string (ids are zero-padded, e.g. '02201')."""
    return str(v).strip()


def iou(gen, truth) -> float:
    """Intersection-over-union of two polygons. 0.0 if either is missing/empty."""
    if gen is None or truth is None or gen.is_empty or truth.is_empty:
        return 0.0
    inter = gen.intersection(truth).area
    uni = gen.union(truth).area
    return inter / uni if uni > 0 else 0.0


def load_truth(cfg: dict):
    """
    Load truth polygons + sampling points and join them.

    Returns (sites, points_xy) where `sites` maps site_id -> truth polygon and
    `points_xy` maps site_id -> (x, y) snap coordinate, for every matched pair.
    Aborts (SystemExit) if the join yields far fewer pairs than expected — the
    linchpin check for the whole deliverable.
    """
    inp = cfg["inputs"]
    crs = cfg["parameters"]["crs"]
    for key in ("validation_truth_polygons", "validation_points"):
        if not inp.get(key):
            sys.exit(f"config inputs.{key} is not set — cannot run the validation sweep")
        if not Path(inp[key]).exists():
            sys.exit(f"validation file not found: {inp[key]}")

    poly = gpd.read_file(inp["validation_truth_polygons"])
    pts = gpd.read_file(inp["validation_points"])

    # Truth polygons ship no CRS metadata; they are EPSG:2264 (decision_log
    # 2026-06-28, join validated). Assign then reproject both to the working CRS.
    if poly.crs is None:
        poly = poly.set_crs(crs)
    poly = poly.to_crs(crs)
    if pts.crs is None:
        pts = pts.set_crs(crs)
    pts = pts.to_crs(crs)

    truth_by_id = {}
    for _, r in poly.iterrows():
        g = r.geometry
        if g is not None and not g.is_empty:
            truth_by_id[_norm_id(r["SiteID"])] = g
    point_by_id = {}
    for _, r in pts.iterrows():
        g = r.geometry
        if g is not None and not g.is_empty:
            point_by_id[_norm_id(r["AssetID_tx"])] = (g.x, g.y)

    matched = sorted(set(truth_by_id) & set(point_by_id))
    n_expected = len(truth_by_id)
    print(f"Join truth SiteID == point AssetID_tx: {len(matched)} matched "
          f"({len(truth_by_id)} truth polygons, {len(point_by_id)} points)")
    # Fail loud: a broken join (id-space drift, wrong field) yields near-zero
    # matches and an all-garbage table. Require most truth polygons to match.
    if len(matched) < max(20, n_expected - 2):
        only_truth = sorted(set(truth_by_id) - set(point_by_id))
        only_pts = sorted(set(point_by_id) - set(truth_by_id))
        sys.exit(
            "Validation join FAILED — only "
            f"{len(matched)} matched pairs (expected ~{n_expected}).\n"
            f"  truth-only ids: {only_truth}\n  point-only ids: {only_pts}\n"
            "Check the SiteID / AssetID_tx fields before trusting any IoU."
        )

    sites = {sid: truth_by_id[sid] for sid in matched}
    points_xy = {sid: point_by_id[sid] for sid in matched}
    return sites, points_xy


def trace_sites(cfg, G, index, points_xy):
    """
    Snap each site's point to a graph node and trace its upstream pipe set.
    Returns site_id -> (pidx_list, snap_dist_ft). Empty traces are kept (they
    score IoU 0). Uses point geometry for snapping (geometry-first).
    """
    tol = cfg["parameters"].get("manhole_snap_distance_ft", 50.0)
    traces = {}
    for sid, (x, y) in points_xy.items():
        node, dist = nearest_node(index, x, y)
        if dist > tol:
            print(f"  [warn] site {sid} snapped {dist:.1f} ft from nearest node "
                  f"(> {tol:.0f} ft tolerance) — tracing anyway")
        edges, _nodes, _depth = traverse_upstream(G, node)
        pidx = [e["pidx"] for e in edges if e["pidx"] is not None]
        traces[sid] = (sorted(set(pidx)), dist)
    return traces


def _param_grid(method, close_grid, ratio_grid):
    """The second-parameter values to sweep for a method (close_r / ratio / none)."""
    if method == "concave":
        return list(ratio_grid)
    if method == "blocks_dissolve":
        return [None]
    return list(close_grid)   # morph_close, hybrid use close_r


def _boundary_for(method, served_gdf, served_union, p2, blocks_gdf):
    """Build one boundary for a method + second parameter, reusing the cached union."""
    kwargs = {"served_union": served_union}
    if method == "concave":
        kwargs["concave_ratio"] = p2
    elif method in ("morph_close", "hybrid"):
        kwargs["close_ft"] = p2
    if method == "hybrid":
        kwargs["blocks_gdf"] = blocks_gdf
    return build_boundary(served_gdf, method, **kwargs)


def sweep(cfg, methods, sel_grid, close_grid, ratio_grid):
    """
    Run the full method x selection_radius x (close_r|ratio) sweep over all sites.
    Returns (rows, geom_cache, sites): rows is the per-site-per-combo IoU list;
    geom_cache holds overlay geometries keyed (site, method, sel_r, p2); sites maps
    site_id -> truth polygon (for the overlay).
    """
    print(f"Loading graph : {cfg['inputs']['gravity_main_shapefile']}")
    G, pipes = load_graph_from_config(cfg)
    index = build_node_index(G)

    sites, points_xy = load_truth(cfg)
    traces = trace_sites(cfg, G, index, points_xy)

    need_parcels = any(METHOD_UNIT[m] == "parcels" for m in methods)
    need_blocks = any(METHOD_UNIT[m] == "blocks" for m in methods) or ("hybrid" in methods)
    parcels = load_units(cfg, "parcels") if need_parcels else None
    blocks = load_units(cfg, "blocks") if need_blocks else None
    units_by_key = {"parcels": parcels, "blocks": blocks}

    rows = []
    geom_cache = {}   # (site, method, sel_r, p2) -> (generated_boundary, served_union)
    n = len(sites)
    for i, sid in enumerate(sorted(sites), 1):
        pidx, _snap = traces[sid]
        truth = sites[sid]
        served_cache = {}   # (unit_key, sel_r) -> (served_gdf, served_union)

        def get_served(unit_key, sel_r):
            k = (unit_key, sel_r)
            if k not in served_cache:
                pr = assign_population_units(pipes, pidx, units_by_key[unit_key], sel_r)
                u = None if pr.served.empty else pr.served.geometry.union_all()
                served_cache[k] = (pr.served, u)
            return served_cache[k]

        for method in methods:
            unit_key = METHOD_UNIT[method]
            for sel_r in sel_grid:
                served_gdf, served_union = get_served(unit_key, sel_r)
                for p2 in _param_grid(method, close_grid, ratio_grid):
                    geom = _boundary_for(method, served_gdf, served_union, p2, blocks)
                    score = iou(geom, truth)
                    rows.append({
                        "site": sid,
                        "tag": ("DROP" if sid in DROP_SITES
                                else "FLAG" if sid in FLAG_SITES else ""),
                        "method": method,
                        "sel_r": sel_r,
                        "param2": p2,
                        "iou": round(score, 4),
                    })
                    geom_cache[(sid, method, sel_r, p2)] = (geom, served_union)
        print(f"  scored site {i}/{n}: {sid}", flush=True)

    return rows, geom_cache, sites


def summarize(rows):
    """
    Collapse per-site rows to a per-combo summary: median IoU (aggregate sites
    only), count of aggregate sites at IoU >= 0.5, and n aggregate sites.
    Returns a DataFrame sorted by median IoU descending.
    """
    df = pd.DataFrame(rows)
    agg = df[~df["site"].isin(EXCLUDE_FROM_AGGREGATE)]
    out = []
    for (method, sel_r, p2), g in agg.groupby(["method", "sel_r", "param2"], dropna=False):
        out.append({
            "method": method,
            "sel_r": sel_r,
            "param2": p2,
            "median_iou": round(g["iou"].median(), 4),
            "mean_iou": round(g["iou"].mean(), 4),
            "n_ge_0.5": int((g["iou"] >= 0.5).sum()),
            "n_sites": int(len(g)),
        })
    return pd.DataFrame(out).sort_values("median_iou", ascending=False).reset_index(drop=True)


def loo_reaggregate(df: pd.DataFrame) -> pd.DataFrame:
    """
    Leave-one-out re-aggregation of per-site, per-combo sweep rows.

    For each aggregate site (same exclusions as summarize()): pick the winning
    combo on the other sites by median IoU (tie-break: n sites >= 0.5, then
    combo sort order), then look up the held-out site's IoU at that combo.
    Pure pandas — no traces or geometry are recomputed.

    Returns one row per fold: held_out site, winning combo, the training
    median that picked it, and the held-out IoU.
    """
    agg = df[~df["site"].astype(str).isin(EXCLUDE_FROM_AGGREGATE)].copy()
    if agg.empty:
        raise ValueError("no aggregate sites in the sweep rows — nothing to fold")

    # param2 is None/NaN for methods without a second parameter; NaN breaks
    # equality lookups, so key combos on its string form throughout.
    agg["p2_key"] = agg["param2"].astype(str)
    combo_cols = ["method", "sel_r", "p2_key"]

    # combos x sites matrix of IoU (one score per cell by construction).
    pivot = agg.pivot_table(index=combo_cols, columns="site",
                            values="iou", aggfunc="first").sort_index()
    if pivot.isna().any().any():
        missing = int(pivot.isna().sum().sum())
        raise ValueError(
            f"sweep rows are not a full combo x site grid ({missing} missing "
            "cells) — folds would silently drop scores. Re-run --sweep so "
            "every combo is scored on every site."
        )

    folds = []
    for site in sorted(pivot.columns):
        train = pivot.drop(columns=site)
        med = train.median(axis=1)
        n_ge = (train >= 0.5).sum(axis=1)
        # Deterministic winner: best median, then most sites >= 0.5, then the
        # sorted combo index order (stable sort keeps it).
        order = pd.DataFrame({"med": med, "n_ge": n_ge}).sort_values(
            ["med", "n_ge"], ascending=False, kind="stable")
        winner = order.index[0]
        folds.append({
            "held_out": site,
            "method": winner[0],
            "sel_r": winner[1],
            "param2": winner[2],
            "train_median": round(float(med.loc[winner]), 4),
            "held_out_iou": round(float(pivot.loc[winner, site]), 4),
        })
    return pd.DataFrame(folds)


def report_loo(rows_df: pd.DataFrame) -> None:
    """Print the LOO fold table, stability summary, and LOO vs in-sample medians."""
    folds = loo_reaggregate(rows_df)

    print("\n===== LEAVE-ONE-OUT VALIDATION (aggregate excludes "
          f"{sorted(EXCLUDE_FROM_AGGREGATE)}) =====")
    print(folds.to_string(index=False))

    combo_counts = (folds.groupby(["method", "sel_r", "param2"])
                    .size().sort_values(ascending=False))
    print(f"\nFold-stability — winning combo per fold ({len(folds)} folds):")
    for combo, n in combo_counts.items():
        print(f"  {combo}: won {n} fold(s)")
    if len(combo_counts) > max(2, len(folds) // 3):
        print("  [!] winner flips across many folds — the tuned combo is not "
              "stably identified by 20 sites; treat the in-sample choice with care")

    loo_median = folds["held_out_iou"].median()
    # Comparator = highest median IoU over all aggregate sites (same selection
    # rule as the folds; the IoU floor is deliberately not applied here).
    in_sample = summarize(rows_df.to_dict("records")).iloc[0]
    print(f"\nIn-sample median IoU (best combo, all aggregate sites): "
          f"{in_sample['median_iou']:.4f}  "
          f"({in_sample['method']}, sel_r={in_sample['sel_r']}, "
          f"param2={in_sample['param2']})")
    print(f"LOO median IoU (held-out scores):                        "
          f"{loo_median:.4f}")
    print(f"Optimism gap: {in_sample['median_iou'] - loo_median:+.4f}")


def write_overlay(cfg, geom_cache, sites, winner):
    """
    Write output/validation_overlay.gpkg for the winning combo: truth polygons,
    generated_parcels (dissolved served union, intermediate), generated_boundary
    (final), each tagged with per-site IoU.
    """
    method, sel_r, p2 = winner
    crs = cfg["parameters"]["crs"]
    out = Path(cfg["outputs"]["validation_overlay"])

    truth_rows, parcel_rows, boundary_rows = [], [], []
    for sid, truth in sites.items():
        geom, served_union = geom_cache.get((sid, method, sel_r, p2), (None, None))
        score = round(iou(geom, truth), 4)
        tag = ("DROP" if sid in DROP_SITES else "FLAG" if sid in FLAG_SITES else "")
        truth_rows.append({"SiteID": sid, "tag": tag, "geometry": truth})
        if served_union is not None and not served_union.is_empty:
            parcel_rows.append({"SiteID": sid, "iou": score, "geometry": served_union})
        if geom is not None and not geom.is_empty:
            boundary_rows.append({"SiteID": sid, "iou": score, "method": method,
                                  "sel_r": sel_r, "param2": ("" if p2 is None else p2),
                                  "geometry": geom})

    gpd.GeoDataFrame(truth_rows, crs=crs).to_file(out, layer="truth", driver="GPKG")
    if parcel_rows:
        gpd.GeoDataFrame(parcel_rows, crs=crs).to_file(
            out, layer="generated_parcels", driver="GPKG")
    if boundary_rows:
        gpd.GeoDataFrame(boundary_rows, crs=crs).to_file(
            out, layer="generated_boundary", driver="GPKG")
    print(f"\nOverlay written: {out}")
    print("  layers: truth, generated_parcels, generated_boundary "
          f"(winner: {method}, sel_r={sel_r}, param2={p2})")


def _parse_grid(s, default, cast=float):
    if not s:
        return default
    return [cast(x) for x in s.split(",")]


def main():
    ap = argparse.ArgumentParser(description="Boundary-method validation sweep.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--sweep", action="store_true",
                    help="run the full method x radius sweep")
    ap.add_argument("--loo", action="store_true",
                    help="leave-one-out re-aggregation of the sweep rows "
                         "(uses output/validation_sweep.csv unless combined "
                         "with --sweep)")
    ap.add_argument("--methods", default=",".join(METHOD_UNIT),
                    help="comma list of methods to sweep")
    ap.add_argument("--sel", default="", help="comma list of selection radii (ft)")
    ap.add_argument("--close", default="", help="comma list of close radii (ft)")
    ap.add_argument("--ratio", default="", help="comma list of concave ratios")
    args = ap.parse_args()

    if not args.sweep and not args.loo:
        ap.error("nothing to do — pass --sweep and/or --loo")

    cfg = load_config(args.config)
    sweep_csv = Path(cfg["_base_dir"]) / "output" / "validation_sweep.csv"

    if args.loo and not args.sweep:
        # Pure re-aggregation of the saved per-site table — no geometry work.
        if not sweep_csv.exists():
            sys.exit(f"{sweep_csv} not found — run --sweep first (or --sweep --loo)")
        rows_df = pd.read_csv(sweep_csv, dtype={"site": str})
        print(f"LOO re-aggregation of {sweep_csv} "
              f"({len(rows_df)} rows, {rows_df['site'].nunique()} sites)")
        report_loo(rows_df)
        return
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    bad = [m for m in methods if m not in METHOD_UNIT]
    if bad:
        ap.error(f"unknown methods: {bad}; valid: {list(METHOD_UNIT)}")
    sel_grid = _parse_grid(args.sel, DEFAULT_SEL_GRID)
    close_grid = _parse_grid(args.close, DEFAULT_CLOSE_GRID)
    ratio_grid = _parse_grid(args.ratio, DEFAULT_RATIO_GRID)

    rows, geom_cache, sites = sweep(cfg, methods, sel_grid, close_grid, ratio_grid)

    # Full per-site table -> CSV (many rows, unreadable inline); combo summary
    # printed in full — that IS the decision table.
    df = pd.DataFrame(rows)
    df.to_csv(sweep_csv, index=False, encoding="utf-8")

    summary = summarize(rows)

    pd.set_option("display.max_rows", None, "display.width", 160)
    print("\n===== SWEEP SUMMARY (aggregate excludes "
          f"{sorted(EXCLUDE_FROM_AGGREGATE)}) =====")
    print(summary.to_string(index=False))
    print(f"\nFull per-site table ({len(df)} rows): {sweep_csv}")

    # Winner selection, respecting the IoU floor if it is set.
    t1 = cfg["parameters"].get("iou_floor_median")
    n_floor = cfg["parameters"].get("iou_floor_n_sites")
    if t1 is not None and n_floor is not None:
        ok = summary[(summary["median_iou"] >= t1) & (summary["n_ge_0.5"] >= n_floor)]
        if ok.empty:
            print(f"\nNo combo clears the IoU floor (median >= {t1} AND "
                  f">= {n_floor} sites at IoU >= 0.5). Loosen the method or radii.")
            return
        win = ok.iloc[0]
        # param2 is NaN (not None) for methods without a 2nd param (blocks_dissolve),
        # because the DataFrame coerces None -> NaN. The geom_cache was keyed with
        # None, so normalize back before the lookup or the overlay comes up empty.
        p2 = None if pd.isna(win["param2"]) else win["param2"]
        winner = (win["method"], win["sel_r"], p2)
        print(f"\nWINNER (clears floor): {winner}  median IoU {win['median_iou']}, "
              f"{win['n_ge_0.5']} sites >= 0.5")
        write_overlay(cfg, geom_cache, sites, winner)
    else:
        top = summary.iloc[0]
        print("\n----------------------------------------------------------------")
        print("IoU floor is UNSET (parameters.iou_floor_median / iou_floor_n_sites).")
        print("This is the hard stop: review the table above, then set T1 (median")
        print("floor) and N (sites at IoU >= 0.5) in config.yaml and re-run to")
        print("finalize the winner + overlay.")
        print(f"Best median so far: {top['method']} sel_r={top['sel_r']} "
              f"param2={top['param2']} -> median IoU {top['median_iou']}, "
              f"{top['n_ge_0.5']}/{top['n_sites']} sites >= 0.5")
        print("----------------------------------------------------------------")

    if args.loo:
        report_loo(df)


if __name__ == "__main__":
    # Running this file directly puts its own dir (src) on sys.path, so the bare
    # imports above resolve. The repo-root runner (run_validation.py) does the same.
    main()
