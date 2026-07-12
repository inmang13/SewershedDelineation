"""
Meter service-area estimation (one-off analysis, 2026-07-07).

For a list of flow-meter locations (meter name + estimated manhole FACILITYID),
run the production full-rules delineation (trace -> parcel membership ->
competing-pipe exclude/split/buffer-assign -> delaunay boundary ->
fill_uncovered_trace, matching run_competing_review.py) and report an estimated
service-area polygon + area for each. No hand-drawn truth
exists for these meters, so the honesty check is the manhole MONITORBAS label:

  basin_capture_%  of the manholes labeled with the meter's EXPECTED basin, the
                   fraction the upstream trace actually reached. High = trust it;
                   low = the trace missed part of the basin (bad manhole estimate
                   OR an upstream force main the gravity network can't follow).
  dominant_basin   most common MONITORBAS among the traced manholes, and its %.
                   If this isn't the expected basin, the manhole estimate probably
                   landed in the wrong basin.

CAVEAT (stated, not hidden): this is a GRAVITY-ONLY estimate. The network is
gravity_mains with no force-main layer, so any subbasin that reaches the meter
through an upstream lift station / force main is silently dropped. A low
basin_capture_% is the flag for that.

Two meters (CBO, NC2R-2A) have no manhole estimate. For each we trace the ~N
deepest (lowest-invert = most-downstream / outlet) manholes in that MONITORBAS
group and pick the one whose trace captures the most of the basin. These rows are
tagged CANDIDATE — Grace confirms the manhole before the number is trusted.

Run:  python run_meter_service_areas.py --config config.yaml
Outputs (output/meter_service_areas/):
  meter_service_areas.csv      the spreadsheet
  meter_service_areas.gpkg     layers: boundaries (one polygon/meter),
                               traces (contributing pipes tagged by meter)
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                                    # noqa: E402
from graph_builder import (                                        # noqa: E402
    load_graph_from_config, build_node_index, nearest_node,
    small_dangling_pidx,
)
from traversal import trace_manhole, TargetResolutionError         # noqa: E402
from population_join import (                                      # noqa: E402
    load_units, assign_population_units, competing_pipe_check,
    split_border_contested, assign_remaining_by_buffer,
)
from boundary import build_boundary, fill_uncovered_trace         # noqa: E402

SQFT_PER_ACRE = 43560.0

# Grace's meter -> estimated manhole FACILITYID (2026-07-07).
KNOWN_METERS = [
    ("GC1", "21660"), ("TF5", "28112"), ("TF2", "10289"), ("LCO", "17551"),
    ("HRO", "23384"), ("DBO", "21350"), ("FAO", "WW104"), ("HVO", "04796"),
    ("NH1", "4586"), ("NH2", "04609"),
]
# meter -> MONITORBAS code it is expected to drain (for the capture check).
EXPECTED_BASIN = {
    "GC1": "GC1", "TF5": "TF5", "TF2": "TF2", "LCO": "LCO", "HRO": "HRO",
    "DBO": "DBO", "FAO": "FAO", "HVO": "HVO", "NH1": "NH1", "NH2": "NH2",
    "CBO": "CBO", "NC2R-2A": "NC2R",
}
# meters with no manhole estimate -> the MONITORBAS group to search for an outlet.
UNKNOWN_METERS = {"CBO": "CBO", "NC2R-2A": "NC2R"}
CANDIDATE_POOL = 12   # trace this many deepest manholes per unknown basin


def delineate(G, cfg, index, parcels, ignore_pidx, manhole_id):
    """Full production path for one manhole_id. Returns dict or None (no trace)."""
    params = cfg["parameters"]
    sel_r = params["selection_radius_ft"]
    method = params["boundary_method"]
    close_ft = params["close_radius_ft"]
    # Align with the shipped full-rules path (run_competing_review.py): pass the
    # tuned delaunay edge, the nick-gate floors, and apply fill_uncovered_trace.
    max_edge_ft = params.get("delaunay_max_edge_ft", 500.0)
    fill_uncov = params.get("fill_uncovered_enabled", True)
    fill_uncov_buf = params.get("fill_uncovered_buffer_ft", 100.0)

    cfg["inputs"]["manhole_id"] = str(manhole_id)
    cfg["inputs"]["manhole_coordinate"] = None
    try:
        res = trace_manhole(G, cfg, index=index)
    except TargetResolutionError as e:
        return {"status": f"resolution failed: {e}"}
    if res.is_empty:
        return {"status": "headwater - no upstream pipes"}

    pop = assign_population_units(pipes_g, res.pidx_list, parcels, sel_r)
    served = pop.served
    # Full competing-pipe membership path. It's a parcel-scale edge correction —
    # negligible at basin scale — so if any of it errors, fall back to the raw
    # served union rather than losing the area estimate.
    try:
        ann = competing_pipe_check(
            pipes_g, res.pidx_list, served, sel_r,
            border_ring_ft=params.get("border_ring_ft", 75.0),
            border_min_expose=params.get("border_min_expose", 0.10),
            border_min_cover_frac=params.get("border_min_cover_frac", 0.0),
            border_min_cover_area_ft2=params.get("border_min_cover_area_ft2", 0.0),
            ignore_pidx=ignore_pidx)
        kept = ann[ann["cp_excl"] == 0]
        split = split_border_contested(kept, pipes_g, res.pidx_list, sel_r, cfg,
                                       ignore_pidx=ignore_pidx)
        assigned = assign_remaining_by_buffer(split, pipes_g, res.pidx_list, cfg,
                                              ignore_pidx=ignore_pidx)
        for_boundary = assigned[assigned["cp_asgn"] != "exclude"]
    except Exception as e:                                    # noqa: BLE001
        print(f"    [membership fallback: {e}]")
        for_boundary = served

    raw_union = None if served.empty else served.geometry.union_all()
    geom = build_boundary(for_boundary, method,
                          served_union=(None if for_boundary.empty
                                        else for_boundary.geometry.union_all()),
                          close_ft=close_ft, delaunay_max_edge_ft=max_edge_ft)
    # Patch voids the parcel boundary missed but in-trace pipes cross (the
    # 2026-07-06 void-fill update). in_pipes = the traced mains only.
    if geom is not None and fill_uncov:
        in_pipes = pipes_g.iloc[sorted(set(res.pidx_list))]
        geom = fill_uncovered_trace(geom, in_pipes, fill_uncov_buf)
    return {
        "status": "ok",
        "res": res,
        "boundary": geom,
        "boundary_acres": (geom.area / SQFT_PER_ACRE) if geom is not None else 0.0,
        "raw_parcel_acres": (raw_union.area / SQFT_PER_ACRE) if raw_union else 0.0,
        "n_parcels": len(for_boundary),
        "n_pipes": len(res.pidx_list),
        "max_depth": res.max_depth,
        "snap_ft": round(res.snap_dist_ft, 1),
    }


def capture_metrics(res, index, mh, mh_node, expected_basin, snap_tol):
    """basin-capture % and dominant traced-basin label for a trace result."""
    contributing = set(res.nodes) | {res.target_node}
    # manholes whose snapped node is in the contributing set = manholes reached
    reached = mh_node["node"].isin(contributing) & mh_node["node"].notna()
    reached_mh = mh.loc[reached.values]
    exp = mh[mh["MONITORBAS"] == expected_basin]
    exp_reached = reached_mh[reached_mh["MONITORBAS"] == expected_basin]
    capture = (len(exp_reached) / len(exp)) if len(exp) else float("nan")
    labels = Counter(reached_mh["MONITORBAS"].dropna())
    if labels:
        dom, dom_n = labels.most_common(1)[0]
        dom_pct = dom_n / sum(labels.values())
    else:
        dom, dom_pct = "", 0.0
    return {
        "expected_basin": expected_basin,
        "expected_mh_in_basin": len(exp),
        "expected_mh_reached": len(exp_reached),
        "basin_capture_pct": round(100 * capture, 1) if len(exp) else "",
        "manholes_reached": len(reached_mh),
        "dominant_basin": dom,
        "dominant_pct": round(100 * dom_pct, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    params = cfg["parameters"]
    crs = params["crs"]
    base = cfg["_base_dir"]
    out_dir = base / "output" / "meter_service_areas"
    out_dir.mkdir(parents=True, exist_ok=True)

    global pipes_g
    print("Loading graph + parcels ...")
    G, pipes_g = load_graph_from_config(cfg)
    index = build_node_index(G)
    parcels = load_units(cfg, "parcels")
    ignore_pidx = small_dangling_pidx(
        G, params.get("competing_ignore_dangling_max_pipes", 3))

    mh = gpd.read_file(cfg["inputs"]["manholes_shapefile"]).to_crs(crs)
    # Pre-snap every manhole to its nearest graph node (once) for capture scoring.
    snap_tol = params["manhole_node_snap_ft"]
    nodes = []
    for pt in mh.geometry:
        if pt is None or pt.is_empty:
            nodes.append(None); continue
        n, d = nearest_node(index, pt.x, pt.y)
        nodes.append(n if d <= snap_tol else None)
    mh_node = pd.DataFrame({"node": nodes})

    # Existing MonitoringBasins polygon areas, for a reference column.
    mb_path = ("../RDII/scripts/MRMSQPE_comparison/data/MonitoringBasins.shp")
    existing_area = {}
    try:
        mb = gpd.read_file(base / mb_path).to_crs(crs)
        bcol = next((c for c in mb.columns if c.upper().startswith("MONITORBAS")), None)
        if bcol:
            for b, g in mb.dissolve(by=bcol).geometry.items():
                existing_area[str(b)] = g.area / SQFT_PER_ACRE
    except Exception as e:                                    # noqa: BLE001
        print(f"[existing MonitoringBasins not read: {e}]")

    rows, bnd_rows, trace_rows = [], [], []

    def run_one(meter, mh_id, kind):
        exp_basin = EXPECTED_BASIN[meter]
        d = delineate(G, cfg, index, parcels, ignore_pidx, mh_id)
        if d.get("status") != "ok":
            print(f"{meter:8} mh={mh_id:6} {kind:9} -> {d['status']}")
            rows.append({"meter": meter, "manhole": mh_id, "kind": kind,
                         "status": d.get("status"), "expected_basin": exp_basin})
            return
        cap = capture_metrics(d["res"], index, mh, mh_node, exp_basin, snap_tol)
        row = {
            "meter": meter, "manhole": mh_id, "kind": kind, "status": "ok",
            "service_area_acres": round(d["boundary_acres"], 1),
            "raw_parcel_acres": round(d["raw_parcel_acres"], 1),
            "existing_basin_acres": round(existing_area.get(exp_basin, float("nan")), 1)
                if exp_basin in existing_area else "",
            "n_parcels": d["n_parcels"], "n_pipes": d["n_pipes"],
            "max_depth": d["max_depth"], "snap_ft": d["snap_ft"],
            **cap,
        }
        rows.append(row)
        bnd_rows.append({"meter": meter, "manhole": mh_id, "kind": kind,
                         "acres": round(d["boundary_acres"], 1),
                         "capture_pct": cap["basin_capture_pct"],
                         "geometry": d["boundary"]})
        sub = pipes_g.iloc[d["res"].pidx_list].copy()
        sub["meter"] = meter
        trace_rows.append(sub[["meter", "geometry"]])
        print(f"{meter:8} mh={mh_id:6} {kind:9} {d['boundary_acres']:8.1f} ac  "
              f"capture={cap['basin_capture_pct']}%  dom={cap['dominant_basin']}"
              f"({cap['dominant_pct']}%)  pipes={d['n_pipes']}")

    print("\n== Known meters ==")
    for meter, mh_id in KNOWN_METERS:
        run_one(meter, mh_id, "given")

    print("\n== Unknown meters: outlet search (deepest manholes in basin) ==")
    for meter, basin in UNKNOWN_METERS.items():
        grp = mh[mh["MONITORBAS"] == basin].copy()
        # deepest = lowest invert (outlet). INVERTELEV may have nulls/zeros.
        inv = pd.to_numeric(grp["INVERTELEV"], errors="coerce")
        grp = grp.assign(_inv=inv).dropna(subset=["_inv"])
        grp = grp[grp["_inv"] > 0].sort_values("_inv").head(CANDIDATE_POOL)
        print(f"  {meter}: trying {len(grp)} deepest {basin} manholes ...")
        best = None
        for _, r in grp.iterrows():
            fid = str(r["FACILITYID"])
            d = delineate(G, cfg, index, parcels, ignore_pidx, fid)
            if d.get("status") != "ok":
                continue
            cap = capture_metrics(d["res"], index, mh, mh_node,
                                  EXPECTED_BASIN[meter], snap_tol)
            c = cap["basin_capture_pct"]
            c = c if isinstance(c, (int, float)) else -1
            if best is None or c > best[0]:
                best = (c, fid)
        if best is None:
            print(f"    {meter}: no traceable candidate found")
            rows.append({"meter": meter, "manhole": "", "kind": "candidate",
                         "status": "no traceable outlet"})
            continue
        print(f"    {meter}: best candidate mh={best[1]} capture={best[0]}%")
        run_one(meter, best[1], "CANDIDATE")

    df = pd.DataFrame(rows)
    csv_path = out_dir / "meter_service_areas.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8")

    gpkg = out_dir / "meter_service_areas.gpkg"
    if bnd_rows:
        gpd.GeoDataFrame(bnd_rows, crs=crs).to_file(gpkg, layer="boundaries",
                                                    driver="GPKG")
    if trace_rows:
        gpd.GeoDataFrame(pd.concat(trace_rows, ignore_index=True), crs=crs).to_file(
            gpkg, layer="traces", driver="GPKG")

    print(f"\nWrote {csv_path}")
    print(f"Wrote {gpkg} (layers: boundaries, traces)")
    print("\nGRAVITY-ONLY estimate: subbasins fed through an upstream force main "
          "are undercounted; low basin_capture_% is the flag.")


if __name__ == "__main__":
    main()
