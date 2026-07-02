"""Phase 3 sign-off on the real network: counts reconcile, every edge's
direction matches its geometry, and the only non-trivial SCC is the known
08373/08374 flip.

Run from the project root (needs the real shapefiles + config.yaml):
    python tests/verify_phase3_real_data.py
Takes a few minutes — it checks every one of the ~38k edges.
"""
import math
import sys
sys.path.insert(0, "src")

import numpy as np

from config import load_config
from graph_builder import load_graph_from_config, summarize_graph

cfg = load_config("config.yaml")
G, pipes = load_graph_from_config(cfg)

n_valid = int((pipes.geometry.notna() & ~pipes.geometry.is_empty).sum())
print(f"pipes: {len(pipes)} ({n_valid} with geometry); "
      f"graph: {G.number_of_nodes()} nodes / {G.number_of_edges()} edges")
assert G.number_of_edges() == n_valid, "edge count != valid pipe count"

# Direction check over EVERY edge: start node at geometry's first vertex,
# end node at the last (within the repair radius, since merged node
# centroids shift). A flipped assignment would put u ~ the geometric END.
repair_r = cfg["parameters"]["snap_gap_search_radius_ft"]
bad = 0
for u, v, d in G.edges(data=True):
    geom = pipes.geometry.iloc[d["pidx"]]
    (sx, sy), (ex, ey) = geom.coords[0], geom.coords[-1]
    du = math.hypot(G.nodes[u]["x"] - sx, G.nodes[u]["y"] - sy)
    dv = math.hypot(G.nodes[v]["x"] - ex, G.nodes[v]["y"] - ey)
    if du > 2 * repair_r or dv > 2 * repair_r:
        bad += 1
print(f"edges whose direction disagrees with geometry: {bad}")
assert bad == 0

s = summarize_graph(G)
print(f"non-trivial SCCs: {s['n_nontrivial_sccs']} (sizes {s['scc_sizes']})")
assert s["n_nontrivial_sccs"] == 1 and s["largest_scc"] == 2
scc_fids = sorted({d["facilityid"]
                   for scc in s["scc_node_sets"] for n in scc
                   for *_, d in list(G.in_edges(n, data=True))
                   + list(G.out_edges(n, data=True))})
print(f"SCC member pipes: {scc_fids}")
assert set(scc_fids) == {"08364", "08368", "08373", "08374", "08386"} or \
       {"08373", "08374"} <= set(scc_fids), scc_fids

print(f"sources {s['n_sources']}, sinks {s['n_sinks']}, "
      f"isolated {s['n_isolated']}, weak components {s['weak_components']}, "
      f"self-loops {s['n_self_loops']}, parallel {s['n_parallel']}")
print("\nPHASE 3 REAL-DATA CHECKS: ALL PASS")
