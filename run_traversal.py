"""
CLI runner: trace the upstream network for the configured target manhole.

Usage:
    python run_traversal.py --config config.yaml

Resolves inputs.manhole_id (or inputs.manhole_coordinate) to a graph node and
reports the contributing pipe set. This is the Phase 4 hand-off to Phase 5 —
the contributing pipes are addressed by `pidx` (positional index into the pipes
GeoDataFrame).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                       # noqa: E402
from graph_builder import load_graph_from_config     # noqa: E402
from traversal import trace_manhole, TargetResolutionError  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)

    print(f"Loading pipes : {cfg['inputs']['gravity_main_shapefile']}")
    G, pipes = load_graph_from_config(cfg)
    print(f"  Graph       : {G.number_of_nodes():,} nodes / {G.number_of_edges():,} edges")
    print()

    try:
        res = trace_manhole(G, cfg)
    except TargetResolutionError as e:
        print(f"Target resolution FAILED: {e}")
        sys.exit(1)

    print(f"Target        : {res.source} = {res.source_value}")
    print(f"  Resolved to : node {res.target_node}  (snap {res.snap_dist_ft:.1f} ft)")
    print()
    print("Upstream trace")
    print(f"  Contributing pipes : {res.n_edges:,}")
    print(f"  Contributing nodes : {len(res.nodes):,}")
    print(f"  Max depth          : {res.max_depth}")
    if res.is_empty:
        print("  >> No upstream pipes — target is a headwater (no_upstream_found).")


if __name__ == "__main__":
    main()
