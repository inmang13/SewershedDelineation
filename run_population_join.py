"""
CLI runner: assign population units (parcels) to the target manhole's sewershed.

Usage:
    python run_population_join.py --config config.yaml

Traces the upstream network (Phase 4), buffers it by pipe_buffer_distance_ft,
selects served parcels (intersect-any rule), and writes a GIS-reviewable
GeoPackage with the served parcels, the dissolved sewershed polygon, and the
buffer. Final shapefile output + delineation flags are Phase 6.
"""

import argparse
import re
import sys
from pathlib import Path

import geopandas as gpd

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                       # noqa: E402
from graph_builder import load_graph_from_config     # noqa: E402
from traversal import trace_manhole, TargetResolutionError  # noqa: E402
from population_join import load_population_units, assign_population_units  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    params = cfg["parameters"]
    buffer_ft = params["pipe_buffer_distance_ft"]

    print(f"Loading pipes : {cfg['inputs']['gravity_main_shapefile']}")
    G, pipes = load_graph_from_config(cfg)

    try:
        res = trace_manhole(G, cfg)
    except TargetResolutionError as e:
        print(f"Target resolution FAILED: {e}")
        sys.exit(1)

    print(f"Target        : {res.source} = {res.source_value} -> node {res.target_node}")
    print(f"Upstream pipes : {res.n_edges:,}")
    if res.is_empty:
        print(">> No upstream pipes — target is a headwater; no sewershed to build.")
        sys.exit(0)

    print(f"Loading parcels: {cfg['inputs']['population_units_shapefile']}")
    parcels = load_population_units(cfg)

    pop = assign_population_units(pipes, res.pidx_list, parcels, buffer_ft)

    print()
    print(f"Buffer distance : {buffer_ft} ft")
    print(f"Served parcels  : {pop.n_served:,}")
    if pop.is_empty:
        print(">> Upstream pipes exist but no parcels intersect the buffer — "
              "nothing to write. Check CRS and parcel coverage near this site.")
        sys.exit(0)

    sewershed = pop.dissolve()
    print(f"Sewershed area  : {sewershed.area / 43560:,.1f} acres")

    # Write a review GeoPackage (final shapefile output is Phase 6).
    out_dir = cfg["_base_dir"] / "output"
    safe_id = re.sub(r"[^0-9A-Za-z._-]", "_", str(res.source_value))
    out = out_dir / f"sewershed_{safe_id}.gpkg"
    pop.served.to_file(out, layer="served_parcels", driver="GPKG")
    gpd.GeoDataFrame({"manhole": [str(res.source_value)]},
                     geometry=[sewershed], crs=params["crs"]).to_file(
        out, layer="sewershed", driver="GPKG")
    gpd.GeoDataFrame({"manhole": [str(res.source_value)]},
                     geometry=[pop.buffer], crs=params["crs"]).to_file(
        out, layer="pipe_buffer", driver="GPKG")
    print(f"\nWrote {out}")
    print("  layers: served_parcels, sewershed, pipe_buffer")


if __name__ == "__main__":
    main()
