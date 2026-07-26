"""
Production runner — the unified spine (roadmap Phase 7 / P2-5).

Delineates one site or a whole site list end to end and writes the final
sewershed boundaries plus flags. This is the multi-site production entry point;
the per-phase run_*.py scripts remain for focused debugging.

Pipeline per site (the shipped full-rules path, identical to
run_competing_review.py's delineation):

    trace_manhole (guarded resolver)      Phase 4
    assign_population_units               Phase 5
    competing_pipe_check                  QC round 1 item 2
      -> drop cp_excl border units
      -> split_border_contested           equidistant Voronoi split
      -> assign_remaining_by_buffer       buffered-pipe area, drop foreign-won
    build_boundary (delaunay) + fill_uncovered_trace   Phase 8

Then a single cross-site pass (needs every boundary at once):

    align_seams        snap shared borders so neighbours coincide
    resolve_overlaps   equidistant midline split — OPTIONAL (config flag)
    bridge_parts       stitch a site's disjoint parts across a wide gap (last)

This reproduces output/sewershed_final.gpkg (the layer run_demographics.py
consumes) that the two-step run_competing_review.py -> run_seam_align.py chain
produces today, without needing the truth polygons.

Target resolution goes through the guarded resolver (resolve_target_node, false-
headwater guard) for every site, so the sweep/production split (roadmap P2-4)
does not apply here. Multi-site QC flags are written in a single pass, so the
gpkg layer-clobber bug (P2-6) cannot occur.

Usage:
    python run.py --config config.yaml                       # single site from config
    python run.py --config config.yaml --sites 17506,09289   # explicit FACILITYIDs
    python run.py --config config.yaml --sites-file sites.csv # CSV of sites
    python run.py --config config.yaml --qa-only             # network QA only

--sites-file is a CSV with one row per site. It is traced by coordinate if it has
`x` and `y` columns, else by manhole id from a FACILITYID / manhole / SiteID
column. An optional `SiteID` (or `label`) column names the output row and a
`tract` column is carried through for readability.
"""

import argparse
import statistics
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                                 # noqa: E402
from graph_builder import (                                     # noqa: E402
    load_graph_from_config, build_node_index, small_dangling_pidx,
)
from traversal import trace_manhole, TargetResolutionError      # noqa: E402
from population_join import (                                   # noqa: E402
    load_units, assign_population_units, competing_pipe_check,
    split_border_contested, assign_remaining_by_buffer,
)
from polygon_output import (                                    # noqa: E402
    compute_flags, compute_competing_flags, write_flags_csv,
)
from qc_output import write_qc_flags_gpkg                       # noqa: E402
from boundary import (                                          # noqa: E402
    build_boundary, fill_uncovered_trace, align_seams, resolve_overlaps,
    bridge_parts,
)

SQFT_PER_ACRE = 43560.0


def _acres(geom) -> float:
    """Area in acres. Callers here only pass a non-empty geometry."""
    return geom.area / SQFT_PER_ACRE


# ---------------------------------------------------------------------------
# Site list resolution
# ---------------------------------------------------------------------------

def _site_list(cfg, args) -> list[dict]:
    """Build the list of sites to delineate: dicts of {label, tract, target}.

    target is ("manhole_id", value) or ("coordinate", [x, y]) — passed straight
    to trace_manhole so no per-site cfg mutation is needed.
    """
    if args.sites_file:
        return _sites_from_csv(Path(cfg["_base_dir"]) / args.sites_file)
    if args.sites:
        ids = [s.strip() for s in args.sites.split(",") if s.strip()]
        return [{"label": i, "tract": "", "target": ("manhole_id", i)} for i in ids]

    # Single site from config — reuse the existing manhole_id / coordinate keys.
    mh_id = cfg["inputs"].get("manhole_id")
    coord = cfg["inputs"].get("manhole_coordinate")
    if mh_id:
        return [{"label": str(mh_id), "tract": "", "target": ("manhole_id", str(mh_id))}]
    if coord is not None:
        lab = f"({float(coord[0]):.0f},{float(coord[1]):.0f})"
        return [{"label": lab, "tract": "", "target": ("coordinate", list(coord))}]
    sys.exit("No site given — set inputs.manhole_id / manhole_coordinate, or pass "
             "--sites / --sites-file.")


def _sites_from_csv(path: Path) -> list[dict]:
    """Parse a sites CSV. Coordinate mode if x/y columns exist, else manhole id."""
    if not path.exists():
        sys.exit(f"--sites-file not found: {path}")
    # Read every column as string: FACILITYIDs are zero-padded (e.g. "02201"),
    # which pandas' default type inference would silently turn into 2201.0.
    # x/y are converted to float explicitly below.
    df = pd.read_csv(path, dtype=str)
    cols = {c.lower(): c for c in df.columns}
    xcol, ycol = cols.get("x"), cols.get("y")
    id_col = next((cols[c] for c in ("facilityid", "manhole", "siteid", "id")
                   if c in cols), None)
    label_col = next((cols[c] for c in ("siteid", "label", "name")
                      if c in cols), id_col)
    tract_col = cols.get("tract")
    sites = []
    for _, r in df.iterrows():
        if xcol and ycol and pd.notna(r[xcol]) and pd.notna(r[ycol]):
            target = ("coordinate", [float(r[xcol]), float(r[ycol])])
        elif id_col is not None and pd.notna(r[id_col]):
            target = ("manhole_id", str(r[id_col]).strip())
        else:
            continue
        label = (str(r[label_col]).strip() if label_col and pd.notna(r[label_col])
                 else target[1] if target[0] == "manhole_id" else "site")
        tract = str(r[tract_col]).strip() if tract_col and pd.notna(r[tract_col]) else ""
        sites.append({"label": label, "tract": tract, "target": target})
    if not sites:
        sys.exit(f"--sites-file {path} yielded no usable rows (need x/y or an id column).")
    return sites


# ---------------------------------------------------------------------------
# Per-site delineation (the shipped full-rules path)
# ---------------------------------------------------------------------------

def delineate_site(G, pipes, index, parcels, cfg, ignore_pidx, target):
    """Full-rules delineation for one target. Returns a result dict.

    Mirrors run_competing_review.py's per-site sequence exactly so the boundary
    geometry is identical (the validated IoU path); the only additions are the
    site + competing flags, which do not affect the boundary.
    """
    params = cfg["parameters"]
    sel_r = params["selection_radius_ft"]
    method = params["boundary_method"]
    close_ft = params["close_radius_ft"]
    max_edge_ft = params.get("delaunay_max_edge_ft", 500.0)
    fill_uncov = params.get("fill_uncovered_enabled", True)
    fill_uncov_buf = params.get("fill_uncovered_buffer_ft", 100.0)
    qc_buf = params.get("pipe_buffer_distance_ft")

    try:
        res = trace_manhole(G, cfg, index=index, target=target)
    except TargetResolutionError as e:
        return {"status": f"target resolution failed: {e}", "geom": None,
                "flags": [], "res": None, "pop": None}

    # QC buffer carried for low_population_match; the served set depends only on
    # the selection radius, so the boundary is unaffected by passing it.
    pop = assign_population_units(pipes, res.pidx_list, parcels, sel_r, qc_buf)
    flags = compute_flags(res, pop, params)

    if res.is_empty:
        return {"status": "headwater — no upstream pipes", "geom": None,
                "flags": flags, "res": res, "pop": pop}

    cp_enabled = params.get("competing_pipe_check", True)   # config toggle, not the fn
    if cp_enabled and not pop.is_empty:
        ann = competing_pipe_check(
            pipes, res.pidx_list, pop.served, sel_r,
            border_ring_ft=params.get("border_ring_ft", 75.0),
            border_min_expose=params.get("border_min_expose", 0.10),
            border_min_cover_frac=params.get("border_min_cover_frac", 0.0),
            border_min_cover_area_ft2=params.get("border_min_cover_area_ft2", 0.0),
            ignore_pidx=ignore_pidx)
        flags += compute_competing_flags(ann, str(res.source_value))
        kept = ann[ann["cp_excl"] == 0]
        split = split_border_contested(kept, pipes, res.pidx_list, sel_r, cfg,
                                       ignore_pidx=ignore_pidx)
        assigned = assign_remaining_by_buffer(split, pipes, res.pidx_list, cfg,
                                              ignore_pidx=ignore_pidx)
        for_boundary = assigned[assigned["cp_asgn"] != "exclude"]
    else:
        for_boundary = pop.served

    served_union = (None if for_boundary.empty
                    else for_boundary.geometry.union_all())
    geom = build_boundary(for_boundary, method, served_union=served_union,
                          close_ft=close_ft, delaunay_max_edge_ft=max_edge_ft)
    if geom is not None and fill_uncov:
        in_pipes = pipes.iloc[sorted(set(res.pidx_list))]
        geom = fill_uncovered_trace(geom, in_pipes, fill_uncov_buf)

    return {"status": "ok", "geom": geom, "flags": flags, "res": res, "pop": pop}


# ---------------------------------------------------------------------------
# Cross-site pass + output
# ---------------------------------------------------------------------------

def _clear_delineation_layers(gpkg_path: Path) -> None:
    """Drop stale delin_* layers from a prior run so multi-site runs don't leave
    a previous site's flags behind (roadmap P2-6). Network net_* layers written
    by run_qa are preserved.

    GeoPackage backends (pyogrio/fiona) expose no portable per-layer delete, so
    this reads the net_* layers to keep, rewrites the file without them, then the
    caller appends fresh delin_* layers. Degrades to a no-op (leaving a plain
    append) if the file can't be introspected."""
    if not gpkg_path.exists():
        return
    try:
        import pyogrio
        layers = [str(row[0]) for row in pyogrio.list_layers(str(gpkg_path))]
    except Exception:
        return
    if not any(l.startswith("delin_") for l in layers):
        return                                    # nothing stale to clear
    keep = {}
    for lyr in layers:
        if lyr.startswith("net_"):
            try:
                keep[lyr] = gpd.read_file(gpkg_path, layer=lyr)
            except Exception:
                pass
    gpkg_path.unlink()
    for lyr, gdf in keep.items():
        gdf.to_file(gpkg_path, layer=lyr, driver="GPKG")


def main():
    ap = argparse.ArgumentParser(description="Delineate one or many sewershed sites.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--sites", help="comma-separated manhole FACILITYIDs")
    ap.add_argument("--sites-file", help="CSV of sites (x/y or an id column)")
    ap.add_argument("--qa-only", action="store_true",
                    help="run network QA only (delegates to run_qa.py) and stop")
    ap.add_argument("--out", help="output boundary GeoPackage "
                    "(default output/sewershed_final.gpkg)")
    args = ap.parse_args()

    if args.qa_only:
        # Single source of QA logic — hand off to the standalone QA runner.
        import run_qa
        sys.argv = ["run_qa.py", "--config", args.config]
        return run_qa.main()

    cfg = load_config(args.config)
    params = cfg["parameters"]
    crs = params["crs"]
    base = Path(cfg["_base_dir"])

    sites = _site_list(cfg, args)
    print(f"Loading graph : {cfg['inputs']['gravity_main_shapefile']}")
    G, pipes = load_graph_from_config(cfg)
    index = build_node_index(G)
    parcels = load_units(cfg, "parcels")
    ignore_pidx = small_dangling_pidx(
        G, params.get("competing_ignore_dangling_max_pipes", 3))

    # ---- per-site delineation --------------------------------------------
    bounds, summary_rows, all_flags, statuses = {}, [], [], []
    print(f"\n{'label':<16}{'target':<22}{'pipes':>7}{'served':>8}{'acres':>10}  status")
    for s in sites:
        label = s["label"]
        r = delineate_site(G, pipes, index, parcels, cfg, ignore_pidx, s["target"])
        all_flags.extend(r["flags"])     # each flag carries `manhole` = its site
        res, pop = r["res"], r["pop"]
        n_pipes = res.n_edges if res is not None else 0
        n_served = pop.n_served if pop is not None else 0
        tgt = (str(s["target"][1]) if s["target"][0] == "manhole_id"
               else f"({s['target'][1][0]:.0f},{s['target'][1][1]:.0f})")
        if r["status"] != "ok" or r["geom"] is None or r["geom"].is_empty:
            statuses.append({"label": label, "status": r["status"]})
            print(f"{label:<16}{tgt:<22}{n_pipes:>7}{n_served:>8}{'-':>10}  {r['status']}")
            continue
        bounds[label] = r["geom"]
        summary_rows.append({"SiteID": label, "tract": s["tract"],
                             "area_acres": round(_acres(r["geom"]), 2),
                             "n_pipes": n_pipes, "max_depth": res.max_depth})
        print(f"{label:<16}{tgt:<22}{n_pipes:>7}{n_served:>8}"
              f"{_acres(r['geom']):>10.1f}  ok")

    if not bounds:
        print("\nNo delineable sites — nothing written.")
        for st in statuses:
            print(f"  [skipped] {st['label']}: {st['status']}")
        return

    # ---- cross-site pass: align shared borders, then bridge parts ---------
    seam_tol = params.get("seam_align_tol_ft", 50.0)
    if len(bounds) > 1:
        bounds = align_seams(bounds, tol_ft=seam_tol)
        if params.get("resolve_overlaps_enabled", False):
            bounds = resolve_overlaps(bounds, step_ft=seam_tol)
    # Bridge LAST (after align/split have finished moving edges), every site.
    bridge_gap = params.get("bridge_parts_max_gap_ft", 0.0)
    if bridge_gap and bridge_gap > 0:
        for k, g in list(bounds.items()):
            stitched = bridge_parts(g, bridge_gap)
            if stitched is not None:
                bounds[k] = stitched

    # ---- write outputs (all sites, single pass) --------------------------
    out_path = Path(args.out) if args.out else base / "output" / "sewershed_final.gpkg"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fin = gpd.GeoDataFrame(
        [{**row, "geometry": bounds[row["SiteID"]]} for row in summary_rows],
        geometry="geometry", crs=crs)
    fin.to_file(out_path, layer="boundary", driver="GPKG")
    print(f"\nWrote {out_path} (layer: boundary, {len(fin)} sites)")

    # load_config already resolved these against the config's directory
    # (config.py), so do NOT prefix `base` again — that double-prefixes to
    # <base>/<base>/output/... for any config outside the repo root. Harmless
    # when base is "." (every run before the toy example), which is why it
    # survived this long. Roadmap P3-10.
    flags_path = Path(cfg["outputs"]["flags_report"])
    flags_path.parent.mkdir(parents=True, exist_ok=True)
    n_flags = write_flags_csv(all_flags, flags_path)
    print(f"Wrote {flags_path} ({n_flags} flags)")

    gpkg_flags = cfg["outputs"].get("qc_flags_gpkg")
    if gpkg_flags:
        gpath = Path(gpkg_flags)
        _clear_delineation_layers(gpath)              # P2-6: no stale delin_ layers
        layers = write_qc_flags_gpkg(all_flags, gpath, crs, replace=False)
        print(f"Wrote {gpath} (+{len(layers)} delineation flag layers)")

    # ---- summary ----------------------------------------------------------
    areas = [row["area_acres"] for row in summary_rows]
    print(f"\nDelineated {len(bounds)} site(s); median area "
          f"{statistics.median(areas):.0f} acres.")
    for st in statuses:
        print(f"  [skipped] {st['label']}: {st['status']}")


if __name__ == "__main__":
    main()
