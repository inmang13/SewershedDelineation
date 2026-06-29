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
    load_population_units, assign_population_units,
)
from polygon_output import (                          # noqa: E402
    compute_flags, build_sewershed_gdf, build_debug_pipes_gdf, write_flags_csv,
)
from pdf_maps import generate_sewershed_map           # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    params = cfg["parameters"]
    outputs = cfg["outputs"]
    out_dir = cfg["_base_dir"] / "output"
    buffer_ft = params["pipe_buffer_distance_ft"]

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
    parcels = load_population_units(cfg)
    pop = assign_population_units(pipes, res.pidx_list, parcels, buffer_ft)
    if res.is_empty:
        print(">> No upstream pipes — target is a headwater.")
    else:
        print(f"Served parcels : {pop.n_served:,}")

    # Phase 6 — flags + outputs.
    flags = compute_flags(res, pop, params)
    n_flags = write_flags_csv(flags, out_dir / Path(outputs["flags_report"]).name)
    print()
    print(f"Flags          : {n_flags}")
    for f in flags:
        print(f"  [{f['severity']}] {f['flag_type']} — {f['description']}")

    sewershed_gdf = build_sewershed_gdf(res, pop, params)
    if sewershed_gdf is not None:
        area_ac = sewershed_gdf["area_acres"].iloc[0]
        print(f"Sewershed area : {area_ac:,.1f} acres")
        sewershed_gdf.to_file(out_dir / Path(outputs["output_polygon"]).name)

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
        )
        print(f"\nWrote: {Path(outputs['output_polygon']).name}, "
              f"{Path(outputs['debug_upstream_pipes']).name}, "
              f"{Path(outputs['flags_report']).name}, "
              f"{Path(outputs['flag_maps_pdf']).name}")
    else:
        print("No sewershed polygon to write (empty trace or no served parcels).")
        print(f"Wrote: {Path(outputs['flags_report']).name} ({n_flags} flag rows)")


if __name__ == "__main__":
    main()
