"""
CLI runner: Phase 6 — build the sewershed polygon, debug pipe layer, delineation
flags, and overview map for the configured target manhole.

Usage:
    python run_polygon_output.py --config config.yaml

Runs Phases 4 (trace) -> 5 (population join) -> 6 (output), then writes:
    output/sewershed.shp              dissolved sewershed polygon (+ summary fields)
    output/debug_upstream_pipes.shp   contributing pipes, with traversal depth
    output/flags.csv                  delineation flags (always written, header even if clean)
    output/flag_maps.pdf              one-page sewershed overview map

Unlike run_population_join (which exits early on a headwater), this runner always
writes flags.csv so an empty trace surfaces as a no_upstream_found row.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                       # noqa: E402
from graph_builder import load_graph_from_config     # noqa: E402
from traversal import trace_manhole, TargetResolutionError  # noqa: E402
from population_join import (                         # noqa: E402
    load_units, assign_population_units,
)
from polygon_output import (                          # noqa: E402
    compute_flags, build_sewershed_gdf, build_boundary_gdf,
    build_debug_pipes_gdf, write_flags_csv,
)
from pdf_maps import generate_sewershed_map           # noqa: E402
from qc_output import write_qc_flags_gpkg              # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    params = cfg["parameters"]
    outputs = cfg["outputs"]
    out_dir = cfg["_base_dir"] / "output"
    selection_radius = params["selection_radius_ft"]
    qc_buffer_ft = params["pipe_buffer_distance_ft"]
    method = params["boundary_method"]

    print(f"Loading pipes  : {cfg['inputs']['gravity_main_shapefile']}")
    G, pipes = load_graph_from_config(cfg)

    try:
        res = trace_manhole(G, cfg)
    except TargetResolutionError as e:
        print(f"Target resolution FAILED: {e}")
        sys.exit(1)

    print(f"Target         : {res.source} = {res.source_value} -> node {res.target_node}")
    print(f"Upstream pipes : {res.n_edges:,}")

    # Phase 5 — population join. A headwater (empty trace) is valid: an empty
    # pidx_list makes assign_population_units return the canonical empty result, so
    # Phase 6 can emit no_upstream_found rather than exit early.
    print(f"Loading parcels: {cfg['inputs']['population_units_shapefile']}")
    parcels = load_units(cfg, "parcels")
    pop = assign_population_units(pipes, res.pidx_list, parcels,
                                  selection_radius, qc_buffer_ft)
    if res.is_empty:
        print(">> No upstream pipes — target is a headwater.")
    else:
        print(f"Served parcels : {pop.n_served:,}  (selection radius {selection_radius:.0f} ft)")

    # Phase 6 — flags + outputs.
    flags = compute_flags(res, pop, params)
    n_flags = write_flags_csv(flags, out_dir / Path(outputs["flags_report"]).name)
    print()
    print(f"Flags          : {n_flags}")
    for f in flags:
        print(f"  [{f['severity']}] {f['flag_type']} — {f['description']}")

    # Append delineation flags to the QC GeoPackage (network layers, if any, were
    # written by run_qa with replace=True; delin_ layer prefix avoids collision).
    gpkg_path = outputs.get("qc_flags_gpkg")
    if gpkg_path and flags:
        layers = write_qc_flags_gpkg(flags, out_dir / Path(gpkg_path).name,
                                     params["crs"], replace=False)
        print(f"QC flags GPKG  : {Path(gpkg_path).name} (+{len(layers)} delineation layers)")

    # Intermediate — the raw dissolved served-parcel union (gappy, inspectable).
    parcels_gdf = build_sewershed_gdf(res, pop, params)
    if parcels_gdf is not None:
        area_ac = parcels_gdf["area_acres"].iloc[0]
        print(f"Served area    : {area_ac:,.1f} acres (raw parcel dissolve)")
        parcels_gdf.to_file(out_dir / Path(outputs["sewershed_parcels"]).name)

        # Final — the seamless boundary from the configured method. Block-based
        # methods select census blocks; hybrid needs the county blocks to fill gaps.
        served_for_boundary, blocks_gdf, served_union = pop.served, None, pop.dissolve()
        if method == "blocks_dissolve":
            blocks = load_units(cfg, "blocks")
            blk_pop = assign_population_units(pipes, res.pidx_list, blocks, selection_radius)
            served_for_boundary, served_union = blk_pop.served, None
        elif method == "hybrid":
            blocks_gdf = load_units(cfg, "blocks")

        boundary_gdf = build_boundary_gdf(res, params, served_for_boundary,
                                          served_union=served_union, blocks_gdf=blocks_gdf)
        boundary_note = ""
        if boundary_gdf is not None:
            b_area = boundary_gdf["area_acres"].iloc[0]
            print(f"Boundary area  : {b_area:,.1f} acres (method: {method})")
            boundary_gdf.to_file(out_dir / Path(outputs["sewershed_boundary"]).name)
            boundary_note = f", {Path(outputs['sewershed_boundary']).name}"

        debug_gdf = build_debug_pipes_gdf(pipes, res)
        debug_gdf.to_file(out_dir / Path(outputs["debug_upstream_pipes"]).name)

        generate_sewershed_map(
            served=pop.served,
            buffer_geom=pop.buffer,
            pipes_sub=debug_gdf,
            target_xy=res.target_xy,
            flags=flags,
            manhole_id=str(res.source_value),
            area_acres=area_ac,
            output_path=str(out_dir / Path(outputs["flag_maps_pdf"]).name),
            basemap_style=params.get("basemap_style"),
        )
        print(f"\nWrote: {Path(outputs['sewershed_parcels']).name}{boundary_note}, "
              f"{Path(outputs['debug_upstream_pipes']).name}, "
              f"{Path(outputs['flags_report']).name}, "
              f"{Path(outputs['flag_maps_pdf']).name}")
    else:
        print("No sewershed polygon to write (empty trace or no served parcels).")
        print(f"Wrote: {Path(outputs['flags_report']).name} ({n_flags} flag rows)")


if __name__ == "__main__":
    main()
