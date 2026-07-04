"""
Line-trace export for every validation site.

For each of the 24 validation sites, resolve the sampling point to a graph node
(geometry-first snap, guarded production resolver) and export the upstream pipe
trace as line geometry. One GeoPackage, one row per traced pipe, tagged by
tract / manhole / traversal depth so a reviewer can filter and shade in GIS.

This is trace lines only — no buffers, parcels, or boundaries. It uses the same
shared topology as delineation (midspan splits + manual snaps applied via
load_graph_from_config), so the traces match what the tool actually delineates.

Run:  python run_validation_traces.py --config config.yaml
"""

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                                 # noqa: E402
from graph_builder import load_graph_from_config, build_node_index  # noqa: E402
from traversal import trace_manhole, TargetResolutionError     # noqa: E402
from validation import load_truth                              # noqa: E402


def load_tract_labels(cfg) -> dict:
    """AssetID_tx -> Tract label from the sampling-points layer, for readability."""
    pts = gpd.read_file(cfg["inputs"]["validation_points"])
    return {str(r["AssetID_tx"]).strip(): str(r["Tract"]).strip()
            for _, r in pts.iterrows()}


def main():
    ap = argparse.ArgumentParser(description="Export the upstream line trace for every validation site.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="QC/validation_traces.gpkg",
                    help="output GeoPackage path (default: QC/validation_traces.gpkg)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    out_path = cfg["_base_dir"] / args.out
    out_path.parent.mkdir(exist_ok=True)

    print(f"Loading graph : {cfg['inputs']['gravity_main_shapefile']}")
    G, pipes = load_graph_from_config(cfg)
    index = build_node_index(G)

    sites, points_xy = load_truth(cfg)
    tract_of = load_tract_labels(cfg)

    rows, statuses = [], []
    print(f"\n{'tract':<16}{'manhole':<9}{'pipes':>7}{'depth':>7}")
    for sid in sorted(sites):
        tract = tract_of.get(sid, "")
        x, y = points_xy[sid]
        # Guarded production resolver, driven by the sampling-point geometry.
        cfg["inputs"]["manhole_id"] = None
        cfg["inputs"]["manhole_coordinate"] = [x, y]
        try:
            res = trace_manhole(G, cfg, index=index)
        except TargetResolutionError as e:
            statuses.append((tract, sid, f"target resolution failed: {e}"))
            print(f"{tract:<16}{sid:<9}  TARGET RESOLUTION FAILED: {e}")
            continue
        if res.is_empty:
            statuses.append((tract, sid, "headwater — no upstream pipes"))
            print(f"{tract:<16}{sid:<9}  headwater — no upstream pipes")
            continue

        for e in res.edges:
            geom = pipes.geometry.iloc[e["pidx"]]
            if geom is None or geom.is_empty:
                continue
            rows.append({
                "tract": tract,
                "manhole": sid,
                "pidx": e["pidx"],
                "facilityid": e["facilityid"],
                "depth": e["depth"],
                "geometry": geom,
            })
        print(f"{tract:<16}{sid:<9}{len(res.edges):>7}{res.max_depth:>7}")

    if not rows:
        print("\nNo traces produced — nothing written.")
        return

    gdf = gpd.GeoDataFrame(rows, crs=cfg["parameters"]["crs"])
    if out_path.exists():
        out_path.unlink()
    gdf.to_file(out_path, layer="traces", driver="GPKG")

    n_sites = gdf["manhole"].nunique()
    print(f"\nWrote {len(gdf)} pipe lines across {n_sites} sites to {out_path}")
    print("  layer 'traces' — columns: tract, manhole, pidx, facilityid, depth")
    for tract, sid, msg in statuses:
        print(f"  [no trace] {tract} {sid}: {msg}")


if __name__ == "__main__":
    main()
