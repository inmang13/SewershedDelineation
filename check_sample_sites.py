"""
Phase 4 pre-flight: validate graph QA flags only where they matter.

Snaps each sampling-location point to its nearest graph node and flags any site
that lands in a problem zone — a non-trivial strongly-connected component (a
direction-error loop), a self-loop node, or a small disconnected component. A
graph flag only corrupts a result if a sample manhole sits in or upstream of it,
so this targets the cases that can silently produce a wrong catchment instead of
reviewing every flag.

Re-run whenever the gravity-main network or the sampling-location layer changes.

Usage:
    python check_sample_sites.py --config config.yaml \
        --samples ../CommunityWastewaterDashboard/data/raw/shapefiles/Sampling_Locations_05212026.shp
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
import geopandas as gpd
import networkx as nx

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config        # noqa: E402
from graph_builder import (           # noqa: E402
    load_graph_from_config, summarize_graph, build_node_index, nearest_node,
)

# Sites snapping into a component smaller than this can't trace a real catchment.
SMALL_COMPONENT_NODES = 50
# Snap distances beyond this suggest the point isn't on the modeled network.
FAR_SNAP_FT = 50.0
# Field naming in the sampling layer (override via CLI if the schema changes).
DEFAULT_SAMPLE_ID_FIELD = "AssetID_tx"
DEFAULT_SAMPLE_LABEL_FIELD = "Tract"


def check_sample_sites(G, samples, id_field, label_field):
    """Return a DataFrame: one row per sample point with its snapped node + flags."""
    s = summarize_graph(G)
    scc_nodes = set().union(*s["scc_node_sets"]) if s["scc_node_sets"] else set()
    self_loop_nodes = {u for u, v in G.edges() if u == v}

    # Weak-component size per node (direction ignored).
    comp_size = {}
    for comp in nx.connected_components(G.to_undirected()):
        for n in comp:
            comp_size[n] = len(comp)

    index = build_node_index(G)

    rows = []
    for _, r in samples.iterrows():
        geom = r.geometry
        if geom is None or geom.is_empty:
            continue
        node, dist = nearest_node(index, geom.x, geom.y)
        cs = comp_size.get(node, 0)

        flags = []
        if node in scc_nodes:
            flags.append("IN_NONTRIVIAL_SCC")
        if node in self_loop_nodes:
            flags.append("SELF_LOOP")
        if cs < SMALL_COMPONENT_NODES:
            flags.append(f"SMALL_COMPONENT({cs})")
        if dist > FAR_SNAP_FT:
            flags.append(f"FAR_SNAP({dist:.0f}ft)")
        if G.in_degree(node) == 0:
            flags.append("NO_UPSTREAM")

        rows.append({
            "label":     r.get(label_field, "?"),
            "asset_id":  r.get(id_field, "?"),
            "snap_ft":   round(float(dist), 1),
            "node":      node,
            "in_deg":    G.in_degree(node),
            "out_deg":   G.out_degree(node),
            "comp_size": cs,
            "flags":     ";".join(flags) if flags else "OK",
        })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--samples", required=True,
                        help="Path to the sampling-location point shapefile")
    parser.add_argument("--id-field", default=DEFAULT_SAMPLE_ID_FIELD)
    parser.add_argument("--label-field", default=DEFAULT_SAMPLE_LABEL_FIELD)
    args = parser.parse_args()

    cfg = load_config(args.config)
    p = cfg["parameters"]

    G, _ = load_graph_from_config(cfg)

    samples = gpd.read_file(args.samples).to_crs(p["crs"])
    df = check_sample_sites(G, samples, args.id_field, args.label_field)

    pd.set_option("display.max_rows", None, "display.width", 200)
    print(df.to_string(index=False))
    print()
    problems = df[df["flags"] != "OK"]
    print(f"Problem sites: {len(problems)} of {len(df)}")
    if not problems.empty:
        print("  Review:", ", ".join(f"{r.label} ({r.asset_id})" for r in problems.itertuples()))


if __name__ == "__main__":
    main()
