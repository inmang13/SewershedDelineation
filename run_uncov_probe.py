"""
Test Grace's fill method (2026-07-08): isolate in-trace pipe NOT covered by the
polygon, buffer it, union into the polygon, fill the newly closed holes.

The delaunay boundary is built from served PARCELS; where the trace runs through
an area with no selected parcels (a big unsewered void the pipes still pass
through), the boundary leaves a gap. Buffering the uncovered in-trace pipe and
adding it closes that gap; foreign areas get nothing added, so they're untouched.

Usage: python run_uncov_probe.py --site 17506 [--buffer 100]
Renders output/preview_png/uncov_<site>.png (original vs added vs filled).
"""

import argparse
import sys
from pathlib import Path

import geopandas as gpd

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                       # noqa: E402
from graph_builder import load_graph_from_config, build_node_index  # noqa: E402
from boundary import (fill_holes, keep_all_parts, _polygon_parts,  # noqa: E402
                      SQFT_PER_ACRE)
from validation import load_truth, trace_sites, iou  # noqa: E402
from shapely.ops import unary_union                  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Uncovered-trace fill probe.")
    ap.add_argument("--site", required=True)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--buffer", type=float, default=100.0,
                    help="buffer (ft) around uncovered in-trace pipe")
    args = ap.parse_args()

    cfg = load_config(args.config)
    p = cfg["parameters"]
    crs = p["crs"]

    print(f"Loading graph : {cfg['inputs']['gravity_main_shapefile']}")
    G, pipes = load_graph_from_config(cfg)
    index = build_node_index(G)
    sites, points_xy = load_truth(cfg)
    traces = trace_sites(cfg, G, index, {args.site: points_xy[args.site]})
    pidx, _ = traces[args.site]
    in_set = set(pidx)
    in_pipes = pipes.iloc[sorted(in_set)]
    in_union = in_pipes.geometry.union_all()
    foreign = pipes.iloc[[i for i in range(len(pipes)) if i not in in_set]]

    fpath = Path(cfg["_base_dir"]) / "output" / "sewershed_final.gpkg"
    fb = gpd.read_file(fpath, layer="boundary").to_crs(crs)
    boundary = fb[fb["SiteID"].astype(str) == args.site].geometry.iloc[0]
    truth = sites[args.site]

    # 1. in-trace pipe NOT covered by the polygon
    uncovered = in_union.difference(boundary)
    # 2. buffer + union in  3. fill the holes it closes
    added = uncovered.buffer(args.buffer)
    grown = unary_union([boundary, added])
    filled = keep_all_parts(fill_holes(grown))

    print(f"\nSite {args.site}  buffer {args.buffer:g} ft")
    print(f"  boundary        {boundary.area/ SQFT_PER_ACRE:8.1f} ac  IoU {iou(boundary,truth):.3f}")
    print(f"  uncovered pipe  {uncovered.length:8.0f} ft")
    print(f"  after fill      {filled.area/ SQFT_PER_ACRE:8.1f} ac  IoU {iou(filled,truth):.3f}")
    print(f"  net added       {(filled.area-boundary.area)/ SQFT_PER_ACRE:8.1f} ac  "
          f"(truth {truth.area/ SQFT_PER_ACRE:.1f} ac)")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(14, 14))
    # filled polygon (light), then original on top, then the delta highlighted
    delta = filled.difference(boundary)
    gpd.GeoSeries([boundary], crs=crs).plot(ax=ax, facecolor="tab:blue",
                                            alpha=0.20, edgecolor="navy")
    for piece in _polygon_parts(delta):
        gpd.GeoSeries([piece], crs=crs).plot(ax=ax, facecolor="limegreen",
                                             alpha=0.6, edgecolor="green")
    bx = filled.buffer(300).bounds
    in_pipes.cx[bx[0]:bx[2], bx[1]:bx[3]].plot(ax=ax, color="navy", linewidth=1.0)
    foreign.cx[bx[0]:bx[2], bx[1]:bx[3]].plot(ax=ax, color="red", linewidth=0.7)
    gpd.GeoSeries([truth], crs=crs).plot(ax=ax, facecolor="none",
                                         edgecolor="black", linewidth=1.5,
                                         linestyle="--")
    x, y = points_xy[args.site]
    ax.plot(x, y, "k^", ms=13)
    ax.set_title(f"Site {args.site}: fill uncovered in-trace pipe (buffer "
                 f"{args.buffer:g}ft). green=added, navy=in-trace, red=foreign, "
                 f"dashed=truth")
    ax.set_aspect("equal")
    ax.set_axis_off()
    out = Path(cfg["_base_dir"]) / "output" / "preview_png" / f"uncov_{args.site}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
