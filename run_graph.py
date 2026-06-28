"""
CLI runner: build and validate the Phase 3 directed network graph.

Usage:
    python run_graph.py --config config.yaml

Geometry-first: flow direction comes from line geometry (start = upstream,
end = downstream). The headline check is the strongly-connected-component
count — a correctly-directed gravity network is a forest with zero directed
cycles. Any non-trivial SCC is a direction error to resolve before Phase 4.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config  # noqa: E402
from graph_builder import (  # noqa: E402
    load_graph_from_config, summarize_graph, invert_direction_conflicts,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    # load_config validates required keys and resolves input/output paths
    # relative to the config file, so the runner works from any directory.
    cfg = load_config(args.config)
    params = cfg["parameters"]

    print(f"Loading pipes : {cfg['inputs']['gravity_main_shapefile']}")
    G, pipes = load_graph_from_config(cfg)
    print(f"  Pipes       : {len(pipes):,}")
    print(f"Snap tol      : {params['node_snap_tolerance_ft']} ft   |  "
          f"end-node repair: {params.get('snap_gap_search_radius_ft', 10.0)} ft")
    print()

    s = summarize_graph(G)

    print("Graph")
    print(f"  Nodes          : {s['n_nodes']:,}")
    print(f"  Edges          : {s['n_edges']:,}")
    print(f"  Self-loops     : {s['n_self_loops']:,}")
    print(f"  Parallel edges : {s['n_parallel']:,}")
    print(f"  Sources (head) : {s['n_sources']:,}")
    print(f"  Sinks (outlet) : {s['n_sinks']:,}")
    print(f"  Isolated nodes : {s['n_isolated']:,}")
    print(f"  Weak components: {s['weak_components']:,}")
    print()

    print("Directed-cycle check (should be zero for a clean gravity network)")
    print(f"  Non-trivial SCCs : {s['n_nontrivial_sccs']:,}")
    if s["n_nontrivial_sccs"]:
        print(f"  Largest SCC      : {s['largest_scc']:,} nodes")
        print(f"  SCC sizes (top)  : {s['scc_sizes'][:10]}")
        print("  >> Direction errors remain — investigate before Phase 4.")
    else:
        print("  >> No directed cycles. Network is a forest. Geometry-first holds.")
    print()

    conflicts = invert_direction_conflicts(G)
    print("Invert cross-check (QC only — geometry is authoritative)")
    print(f"  Edges where inverts disagree with geometry: {len(conflicts):,}")
    if not conflicts.empty:
        worst = conflicts.head(5)
        for _, r in worst.iterrows():
            print(f"    {r['facilityid']}: rises {r['rise_ft']} ft "
                  f"(up {r['up_invert']} -> dn {r['dn_invert']})")


if __name__ == "__main__":
    main()
