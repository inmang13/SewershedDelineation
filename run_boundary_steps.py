"""
CLI runner: dump every boundary-construction step as its own GIS layer.

Usage:
    python run_boundary_steps.py --site 27508 [--config config.yaml]
                                 [--edge round|mitre] [--trim]

Writes output/boundary_steps_<site>.gpkg with one layer per step, in pipeline
order, so a wrong-looking final polygon can be walked back to the step that
introduced the problem:

    01_upstream_pipes    traced in-trace pipes
    02_selection_buffer  pipes buffered by selection_radius_ft
    03_served_parcels    intersect-any served parcels (pre membership rules)
    04_served_union      dissolved served parcels
    05_dilated           buffer +close_radius_ft
    06_dilated_filled    interior rings removed
    07_closed            buffer -close_radius_ft (morph close complete)
    08_closed_final      fill holes + keep all parts
    09_parcels_touching  all parcels intersecting the closed polygon (trim ref)
    10_street_fill       closed minus all parcels (what trim keeps off-parcel)
    11_trimmed           served_union + street_fill        (only with --trim)
    12_final             fill holes + keep all parts of 11 (or of 08)
    99_truth             truth polygon for the site

Each polygonal layer also gets a companion `<name>_lines` boundary-only layer
so internal edges/slivers are visible regardless of fill symbology.
"""

import argparse
import sys
from pathlib import Path

import geopandas as gpd

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                       # noqa: E402
from graph_builder import load_graph_from_config, build_node_index  # noqa: E402
from boundary import morph_close, fill_holes, keep_all_parts  # noqa: E402
from population_join import load_units, assign_population_units, \
    buffer_upstream_pipes                             # noqa: E402
from validation import load_truth, trace_sites       # noqa: E402
from shapely.ops import unary_union                  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Boundary step-by-step debug layers.")
    ap.add_argument("--site", required=True)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--edge", default="round", choices=["round", "mitre"])
    ap.add_argument("--trim", action="store_true",
                    help="include the trim_to_parcels steps (09-11)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    params = cfg["parameters"]
    sel_r = params["selection_radius_ft"]
    close_ft = params["close_radius_ft"]
    crs = params["crs"]

    print(f"Loading graph : {cfg['inputs']['gravity_main_shapefile']}")
    G, pipes = load_graph_from_config(cfg)
    index = build_node_index(G)
    sites, points_xy = load_truth(cfg)
    if args.site not in sites:
        sys.exit(f"site {args.site} not in truth set; have {sorted(sites)}")
    traces = trace_sites(cfg, G, index, {args.site: points_xy[args.site]})
    pidx, _snap = traces[args.site]
    parcels = load_units(cfg, "parcels")

    out = Path(cfg["_base_dir"]) / "output" / f"boundary_steps_{args.site}.gpkg"

    def write(name, geom, **attrs):
        """Write one geometry as a polygon layer + a boundary-lines layer."""
        if geom is None or geom.is_empty:
            print(f"  {name}: EMPTY — skipped")
            return
        g = gpd.GeoDataFrame([{"step": name, **attrs}],
                             geometry=[geom], crs=crs)
        g.to_file(out, layer=name, driver="GPKG")
        if geom.geom_type not in ("LineString", "MultiLineString"):
            lines = geom.boundary
            gpd.GeoDataFrame([{"step": name}], geometry=[lines], crs=crs) \
                .to_file(out, layer=f"{name}_lines", driver="GPKG")
        nparts = len(getattr(geom, "geoms", [geom]))
        print(f"  {name}: {geom.geom_type}, {nparts} part(s), "
              f"area {geom.area / 43560:.1f} ac" if geom.area else f"  {name}: lines")

    # 01 pipes
    up = pipes.iloc[sorted(set(pidx))]
    up.to_file(out, layer="01_upstream_pipes", driver="GPKG")
    print(f"  01_upstream_pipes: {len(up)} pipes")

    # 02 buffer + 03 served
    buf = buffer_upstream_pipes(pipes, pidx, sel_r)
    write("02_selection_buffer", buf)
    pr = assign_population_units(pipes, pidx, parcels, sel_r)
    pr.served.to_file(out, layer="03_served_parcels", driver="GPKG")
    print(f"  03_served_parcels: {len(pr.served)} parcels")

    # 04 union
    served_union = None if pr.served.empty else pr.served.geometry.union_all()
    write("04_served_union", served_union)

    # 05-08 morph close, decomposed
    kw = {"join_style": args.edge}
    if args.edge == "mitre":
        kw["mitre_limit"] = 2.0
    dilated = served_union.buffer(close_ft, **kw)
    write("05_dilated", dilated)
    dilated_filled = fill_holes(dilated)
    write("06_dilated_filled", dilated_filled)
    closed = dilated_filled.buffer(-close_ft, **kw)
    write("07_closed", closed)
    closed_final = keep_all_parts(fill_holes(closed))
    write("08_closed_final", closed_final)

    final_src = closed_final
    if args.trim:
        idx = parcels.sindex.query(closed_final, predicate="intersects")
        touching = parcels.iloc[sorted(idx)]
        touching.to_file(out, layer="09_parcels_touching", driver="GPKG")
        print(f"  09_parcels_touching: {len(touching)} parcels")
        all_parcels = touching.geometry.union_all()
        street_fill = closed_final.difference(all_parcels)
        write("10_street_fill", street_fill)
        trimmed = unary_union([served_union, street_fill])
        write("11_trimmed", trimmed)
        final_src = trimmed

    final = keep_all_parts(fill_holes(final_src))
    write("12_final", final)
    write("99_truth", sites[args.site])

    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
