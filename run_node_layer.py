"""
CLI runner: generate geometry-derived node layers.

Usage:
    python run_node_layer.py --config config.yaml

Process:
  Pass 1 — merge raw endpoints within node_snap_tolerance_ft
  Pass 2 — repair: merge end-node pairs within snap_gap_search_radius_ft
            (junctions excluded, so short pipe segments are safe)
  QC      — find any residual near-miss end-node pairs after repair

Outputs:
  1. output/pipe_endpoint_nodes.gpkg  — all nodes after repair
  2. output/snap_gap_pairs.gpkg       — residual near-miss pairs (QC)
  3. output/end_nodes.gpkg            — start_only + end_only nodes only

Pauses after writing so you can inspect in GIS before continuing.
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).parent / "src"))
from node_layer import write_node_layer  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    params        = cfg["parameters"]
    snap_tol      = params["node_snap_tolerance_ft"]
    search_radius = params.get("snap_gap_search_radius_ft", 10.0)
    outputs       = cfg["outputs"]

    print(f"Loading pipes : {cfg['inputs']['gravity_main_shapefile']}")
    print(f"Pass-1 snap   : {snap_tol} ft  (coincident endpoints)")
    print(f"Pass-2 repair : {search_radius} ft  (end-node snap gaps only)")
    print()

    nodes, pairs, end_nodes = write_node_layer(cfg)

    # --- Node layer summary ---
    role_counts = nodes["role"].value_counts().to_dict()
    print(f"Node layer  →  {outputs.get('node_layer', 'output/pipe_endpoint_nodes.gpkg')}")
    print(f"  Total    : {len(nodes):,}")
    print(f"  junction : {role_counts.get('junction',   0):,}")
    print(f"  start_only: {role_counts.get('start_only', 0):,}")
    print(f"  end_only  : {role_counts.get('end_only',   0):,}")
    print()

    # --- QC: residual snap gaps after repair ---
    print(f"Snap gap QC →  {outputs.get('snap_gap_pairs', 'output/snap_gap_pairs.gpkg')}")
    if pairs.empty:
        print("  No residual near-miss pairs — all gaps repaired.")
    else:
        d = pairs["dist_ft"].values
        print(f"  Residual pairs : {len(pairs):,}")
        print(f"  Distance range : {d.min():.2f} – {d.max():.2f} ft")
        for pct in [50, 90, 99]:
            print(f"  p{pct:02d}            : {np.percentile(d, pct):.2f} ft")
    print()

    # --- End nodes ---
    end_role = end_nodes["role"].value_counts().to_dict()
    print(f"End nodes   →  {outputs.get('end_nodes', 'output/end_nodes.gpkg')}")
    print(f"  Total      : {len(end_nodes):,}")
    print(f"  start_only : {end_role.get('start_only', 0):,}")
    print(f"  end_only   : {end_role.get('end_only',   0):,}")
    print()

    input("Layers written. Load in GIS and press Enter when ready to continue...")


if __name__ == "__main__":
    main()
