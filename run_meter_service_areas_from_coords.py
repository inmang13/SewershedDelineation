"""
Meter service-area estimation from TRUE meter coordinates (2026-07-23).

Supersedes the manhole-ID targeting in run_meter_service_areas.py (2026-07-07).
the city supplied surveyed lat/lon for every flow meter, so the delineation no
longer depends on Grace's estimated manhole FACILITYIDs or on the MONITORBAS
name crosswalk. That crosswalk is what blocked ENOR / MCO / RMO — with real
coordinates those three trace like any other meter.

FORCE MATCH (approved by Grace, 2026-07-23)
-------------------------------------------
A surveyed meter sits in a manhole; the gravity-main graph only has nodes at
pipe endpoints. When the nearest node is farther than the strict tolerance
(parameters.manhole_snap_distance_ft, 50 ft) the meter is not missing from the
network — the nearest *modelled* endpoint is simply offset. Rather than drop
the meter, this script retries with the tolerance widened to the observed
distance and records the row as `forced_match` with the true snap distance, so
every downstream consumer can see exactly how far the target moved.

This is deliberately a two-pass design: strict first, forced only on failure.
A meter that snaps cleanly is never silently widened, and `snap_ft` is always
the real measured distance, never the tolerance that permitted it.

The honesty check is unchanged from the 2026-07-07 run: `dominant_basin` is the
most common MONITORBAS label among the manholes the trace actually reached. It
needs no crosswalk, so it works for ENOR/MCO/RMO too — if a trace's dominant
label disagrees with the meter name, the coordinate landed somewhere unexpected.

CAVEAT (stated, not hidden): gravity-only, same as the 2026-07-07 run. The
network is gravity_mains with no force-main layer, so any subbasin reaching the
meter through an upstream lift station is silently dropped.

Run:  python run_meter_service_areas_from_coords.py --config config.yaml
Outputs (output/meter_service_areas_from_coords/):
  meter_service_areas_from_coords.csv   the spreadsheet
  meter_service_areas_from_coords.gpkg  layers: boundaries (one polygon/meter),
                                        traces (contributing pipes tagged by meter)
"""

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import Transformer
from shapely.geometry import Point

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

# capture_metrics is a pure scoring helper — imported rather than copied so the
# two service-area scripts cannot drift apart on how capture % is computed.
from run_meter_service_areas import capture_metrics, SQFT_PER_ACRE  # noqa: E402

# the city's meter coordinate export lives in the RDII project (immutable raw input).
COORDS_CSV = Path("../RDII/data/raw/meter_coordinates.csv")
COORDS_CRS = "EPSG:4326"          # LATITUDE / LONGITUDE columns are WGS84 lat/lon
# LOCATION NAME is "<CITY>_<meter>" (e.g. one real export uses a 6-letter city
# code). Stripped generically — up to and including the first underscore —
# rather than hardcoding the source city's name in a public repo.

# meter -> MONITORBAS code it is expected to drain. Only used for the capture-%
# QC column; meters absent here still get a dominant_basin label.
#
# ENOR/MCO/RMO were deliberately absent on the first run — resolving their basin
# code was the open question dominant_basin existed to inform, not assume. That
# run answered it (2026-07-23): ENOR->ENO 61.5%, MCO->MCP 100.0%, RMO->RMP 98.8%
# of each trace's reached manholes, so the O<->P difference is the city's naming
# convention rather than a mis-assignment. Filling them in here is what lets the
# capture-% cross-check run for them too — without it those three were the only
# meters whose area had no independent confirmation, which is exactly backwards
# for the three the surveyed coordinates were obtained to unblock.
EXPECTED_BASIN = {
    "GC1": "GC1", "TF5": "TF5", "TF2": "TF2", "LCO": "LCO", "HRO": "HRO",
    "DBO": "DBO", "FAO": "FAO", "HVO": "HVO", "NH1": "NH1", "NH2": "NH2",
    "CBO": "CBO", "NC2R-2A": "NC2R",
    "ENOR": "ENO", "MCO": "MCP", "RMO": "RMP",
}


def load_meter_targets(base, crs):
    """Read the city's meter coordinates and project them to the network CRS.

    Returns a list of (meter, x, y) in `crs`. Fails loudly on a missing file or
    unexpected columns — a silently empty target list would produce an empty
    run that looks like a successful one.
    """
    path = (base / COORDS_CSV).resolve()
    if not path.exists():
        raise FileNotFoundError(f"meter coordinates not found: {path}")
    df = pd.read_csv(path)
    required = {"LOCATION NAME", "LATITUDE", "LONGITUDE"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path} missing columns: {sorted(missing)}")

    transformer = Transformer.from_crs(COORDS_CRS, crs, always_xy=True)
    targets = []
    for _, r in df.iterrows():
        site = str(r["LOCATION NAME"]).strip()
        meter = site.split("_", 1)[1] if "_" in site else site
        x, y = transformer.transform(float(r["LONGITUDE"]), float(r["LATITUDE"]))
        targets.append((meter, x, y))
    if not targets:
        raise ValueError(f"{path} contained no meter rows")
    return targets


def nearest_manhole(mh, x, y):
    """(FACILITYID, distance_ft) of the manhole closest to a surveyed coordinate.

    Downstream (RDII pipe_attributes) identifies a meter's own outlet pipe by
    matching this FACILITYID against the gravity mains' FROMMH/TOMH. Deriving it
    from the surveyed coordinate replaces the estimated manhole IDs the
    2026-07-07 run relied on — the distance column is what makes a bad
    association visible instead of silent.
    """
    d = mh.geometry.distance(Point(x, y))
    i = d.idxmin()
    return str(mh.at[i, "FACILITYID"]), float(d.at[i])


def delineate_at_coord(G, cfg, pipes_g, index, parcels, ignore_pidx, x, y, snap_tol):
    """Full production delineation path for one coordinate target.

    Mirrors run_meter_service_areas.delineate() step for step — the only
    difference is the target (a coordinate, passed through trace_manhole's
    `target` argument, rather than a manhole FACILITYID read from cfg) and the
    per-call snap tolerance. Keep the two in sync if the production path moves.
    """
    params = cfg["parameters"]
    sel_r = params["selection_radius_ft"]
    method = params["boundary_method"]
    close_ft = params["close_radius_ft"]
    max_edge_ft = params.get("delaunay_max_edge_ft", 500.0)
    fill_uncov = params.get("fill_uncovered_enabled", True)
    fill_uncov_buf = params.get("fill_uncovered_buffer_ft", 100.0)

    # resolve_target_node reads the coordinate tolerance from this parameter.
    # Set it per call (restored by the caller) so a forced meter widens the
    # tolerance for itself alone and never for the meters traced after it.
    prev_tol = params["manhole_snap_distance_ft"]
    params["manhole_snap_distance_ft"] = snap_tol
    try:
        res = trace_manhole(G, cfg, index=index, target=("coordinate", [x, y]))
    except TargetResolutionError as e:
        return {"status": f"target resolution failed: {e}"}
    finally:
        params["manhole_snap_distance_ft"] = prev_tol

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--force-margin-ft", type=float, default=1.0,
                    help="extra tolerance added above the observed snap distance "
                         "when force-matching a meter beyond the strict tolerance")
    args = ap.parse_args()

    cfg = load_config(args.config)
    params = cfg["parameters"]
    crs = params["crs"]
    base = cfg["_base_dir"]
    out_dir = base / "output" / "meter_service_areas_from_coords"
    out_dir.mkdir(parents=True, exist_ok=True)

    strict_tol = params["manhole_snap_distance_ft"]
    targets = load_meter_targets(base, crs)
    print(f"Loaded {len(targets)} meter coordinates "
          f"(strict snap tolerance {strict_tol} ft)")

    print("Loading graph + parcels ...")
    G, pipes_g = load_graph_from_config(cfg)
    index = build_node_index(G)
    parcels = load_units(cfg, "parcels")
    ignore_pidx = small_dangling_pidx(
        G, params.get("competing_ignore_dangling_max_pipes", 3))

    mh = gpd.read_file(cfg["inputs"]["manholes_shapefile"]).to_crs(crs)
    mh_snap_tol = params["manhole_node_snap_ft"]
    nodes = []
    for pt in mh.geometry:
        if pt is None or pt.is_empty:
            nodes.append(None)
            continue
        n, d = nearest_node(index, pt.x, pt.y)
        nodes.append(n if d <= mh_snap_tol else None)
    mh_node = pd.DataFrame({"node": nodes})

    rows, bnd_rows, trace_rows = [], [], []

    for meter, x, y in targets:
        # Measure the real distance first, then decide strict vs forced. Reading
        # the distance up front is what lets `snap_ft` stay honest: it is the
        # measured offset, not whatever tolerance the trace was allowed.
        _, dist = nearest_node(index, x, y)
        forced = dist > strict_tol
        tol = (dist + args.force_margin_ft) if forced else strict_tol
        mh_id, mh_dist = nearest_manhole(mh, x, y)

        d = delineate_at_coord(G, cfg, pipes_g, index, parcels, ignore_pidx,
                               x, y, tol)
        match_kind = "forced_match" if forced else "ok"

        if d.get("status") != "ok":
            print(f"{meter:8} -> {d['status']}")
            rows.append({"meter": meter, "status": d["status"],
                         "nearest_node_ft": round(dist, 1)})
            continue

        exp_basin = EXPECTED_BASIN.get(meter)
        cap = capture_metrics(d["res"], index, mh, mh_node,
                              exp_basin if exp_basin else "__none__", mh_snap_tol)
        if not exp_basin:
            # No expected basin to score against — blank the capture columns
            # rather than reporting a 0% capture against a sentinel that does
            # not exist. dominant_basin is the QC signal for these meters.
            cap["expected_basin"] = ""
            cap["expected_mh_in_basin"] = ""
            cap["expected_mh_reached"] = ""
            cap["basin_capture_pct"] = ""

        rows.append({
            "meter": meter, "manhole": mh_id, "status": match_kind,
            "service_area_acres": round(d["boundary_acres"], 2),
            "raw_parcel_acres": round(d["raw_parcel_acres"], 1),
            "n_parcels": d["n_parcels"], "n_pipes": d["n_pipes"],
            "max_depth": d["max_depth"], "snap_ft": d["snap_ft"],
            "manhole_dist_ft": round(mh_dist, 1),
            "forced_match": forced, "strict_tolerance_ft": strict_tol,
            **cap,
        })
        bnd_rows.append({"meter": meter, "manhole": mh_id, "status": match_kind,
                         "acres": round(d["boundary_acres"], 2),
                         "snap_ft": d["snap_ft"], "forced_match": forced,
                         "capture_pct": cap["basin_capture_pct"],
                         "geometry": d["boundary"]})
        sub = pipes_g.iloc[d["res"].pidx_list].copy()
        sub["meter"] = meter
        trace_rows.append(sub[["meter", "geometry"]])

        flag = "FORCED" if forced else "     "
        print(f"{meter:8} {flag} {d['boundary_acres']:9.2f} ac  "
              f"snap={d['snap_ft']:6.1f} ft  pipes={d['n_pipes']:5d}  "
              f"mh={mh_id:>8}({mh_dist:5.1f} ft)  "
              f"dom={cap['dominant_basin']}({cap['dominant_pct']}%)", flush=True)

    df = pd.DataFrame(rows)
    csv_path = out_dir / "meter_service_areas_from_coords.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8")

    gpkg = out_dir / "meter_service_areas_from_coords.gpkg"
    if gpkg.exists():
        gpkg.unlink()   # GPKG layer writes append; a stale layer would survive
    if bnd_rows:
        gpd.GeoDataFrame(bnd_rows, crs=crs).to_file(gpkg, layer="boundaries",
                                                    driver="GPKG")
    if trace_rows:
        gpd.GeoDataFrame(pd.concat(trace_rows, ignore_index=True), crs=crs).to_file(
            gpkg, layer="traces", driver="GPKG")

    n_forced = int(df["forced_match"].sum()) if "forced_match" in df else 0
    print(f"\nWrote {csv_path}")
    print(f"Wrote {gpkg} (layers: boundaries, traces)")
    print(f"{n_forced} of {len(targets)} meters force-matched beyond the "
          f"{strict_tol} ft strict tolerance — see snap_ft for the real offset.")
    print("GRAVITY-ONLY estimate: subbasins fed through an upstream force main "
          "are undercounted; low basin_capture_% is the flag.")


if __name__ == "__main__":
    main()
