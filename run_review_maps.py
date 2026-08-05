"""
Render one map per open force-main review system, for visual review.

The review CSV asks 63 questions, but they are not 63 independent decisions —
they cluster into 32 connected force-main systems, and deciding a system usually
answers every question in it at once. So this renders one image per system, not
one per row.

Each map shows, over satellite imagery:
  - the force mains of that system, in magenta
  - every other force main nearby, in faded magenta (so a system that looks
    isolated but sits beside another is obvious)
  - nearby gravity mains as arrows pointing the way flow runs
  - the open review points as numbered yellow markers
  - any treatment plant or lift station within view

Usage:
    python run_review_maps.py --config config.yaml
    python run_review_maps.py --config config.yaml --systems 9,31   # just these
"""

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                                   # noqa: E402
from force_mains import load_force_mains, build_topology, load_manual_joins  # noqa: E402
from terminal_facilities import load_facilities                  # noqa: E402
from pdf_maps import _add_basemap                                # noqa: E402

FM_COLOR = "#ff00ff"
GRAVITY_COLOR = "#00e5ff"
POINT_COLOR = "#ffe600"
MIN_HALF_WIDTH_FT = 180.0     # never zoom tighter than this; context matters more
PAD_FRACTION = 0.55


def main():
    ap = argparse.ArgumentParser(description="Render maps of open review systems.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--systems", help="comma-separated SYSTEM ids; default all")
    ap.add_argument("--outdir", default="output/review_maps")
    ap.add_argument("--points", action="store_true",
                    help="one tight map per question instead of one per system. "
                         "Needed for long systems, where a whole-system view is "
                         "zoomed so far out the pipes merge into a blob.")
    ap.add_argument("--half-width", type=float, default=200.0,
                    help="half-width of a --points map, in feet (default 200)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    crs = cfg["parameters"]["crs"]
    base = Path(cfg["_base_dir"])

    review = gpd.read_file(base / cfg["outputs"]["force_main_review_shp"])
    if args.systems:
        wanted = {int(s) for s in args.systems.split(",")}
        review = review[review.SYSTEM.isin(wanted)]
    if review.empty:
        sys.exit("No review rows to map.")

    fm, _ = load_force_mains(cfg)
    topo = build_topology(fm, cfg["parameters"].get("force_main_component_tol_ft", 1.0),
                          manual_joins=load_manual_joins(cfg))
    comp_of = {}
    for cid, comp in enumerate(topo.components):
        for _, _, d in topo.graph.subgraph(comp).edges(data=True):
            comp_of[d["pidx"]] = cid
    fm = fm.assign(system=[comp_of.get(i, -1) for i in range(len(fm))])

    gm = gpd.read_file(cfg["inputs"]["gravity_main_shapefile"]).to_crs(crs)
    facilities = load_facilities(cfg)

    outdir = base / args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    written = []
    if args.points:
        for i, r in enumerate(review.itertuples(), start=1):
            fid = str(r.FM_ID).replace("FM:", "").replace(":", "_")
            path = outdir / f"q{i:02d}_sys{int(r.SYSTEM):02d}_{fid}_{r.FM_END}.png"
            _render(path, int(r.SYSTEM), review.iloc[[i - 1]], fm, gm,
                    facilities, crs, fixed_half=args.half_width)
            written.append(path.name)
            print(f"  {path.name}  gap {r.GAP_FT:.1f} ft, reads as {r.READS_AS}")
    else:
        for system, rows in review.groupby("SYSTEM"):
            path = outdir / f"system_{int(system):02d}.png"
            _render(path, int(system), rows, fm, gm, facilities, crs)
            written.append(path.name)
            print(f"  {path.name}  ({len(rows)} question(s), "
                  f"verdict={rows.VERDICT.iloc[0]})")
    print(f"\n{len(written)} map(s) in {outdir}")


def _render(path, system, rows, fm, gm, facilities, crs, fixed_half=None):
    own = fm[fm.system == system]
    # Frame on the system plus its questions, then pad. A system can be long and
    # thin, so the frame is squared up afterwards or the basemap tiles distort.
    focus = list(own.total_bounds) if not own.empty else list(rows.total_bounds)
    rb = rows.total_bounds
    minx, miny = min(focus[0], rb[0]), min(focus[1], rb[1])
    maxx, maxy = max(focus[2], rb[2]), max(focus[3], rb[3])
    if fixed_half is not None:
        # Point mode: frame on the question itself, ignoring how far the rest of
        # the system runs. A 16-mile system framed whole is unreadable.
        half = fixed_half
        cx, cy = float(rows.geometry.iloc[0].x), float(rows.geometry.iloc[0].y)
    else:
        half = max((maxx - minx) / 2, (maxy - miny) / 2, MIN_HALF_WIDTH_FT)
        half *= (1 + PAD_FRACTION)
        cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    win = (cx - half, cy - half, cx + half, cy + half)

    fig, ax = plt.subplots(figsize=(9, 9), dpi=110)
    ax.set_xlim(win[0], win[2])
    ax.set_ylim(win[1], win[3])

    box = gpd.GeoSeries.from_xy([cx], [cy]).buffer(half * 1.5).iloc[0]

    near_gm = gm[gm.intersects(box)]
    for geom in near_gm.geometry:
        xs, ys = geom.xy
        ax.plot(xs, ys, color=GRAVITY_COLOR, lw=1.6, zorder=3, alpha=0.9)
        # One arrow per pipe at its midpoint, pointing downstream. Flow direction
        # is the whole reason these maps exist, so it has to be visible.
        i = len(xs) // 2
        if i >= 1:
            ax.annotate("", xy=(xs[i], ys[i]), xytext=(xs[i - 1], ys[i - 1]),
                        arrowprops=dict(arrowstyle="-|>", color=GRAVITY_COLOR,
                                        lw=1.4, mutation_scale=13), zorder=4)

    other = fm[(fm.system != system) & fm.intersects(box)]
    if not other.empty:
        other.plot(ax=ax, color=FM_COLOR, lw=2.0, alpha=0.32, zorder=5)
    if not own.empty:
        own.plot(ax=ax, color=FM_COLOR, lw=3.4, zorder=6)

    near_fac = facilities[facilities.intersects(box)] if not facilities.empty \
        else facilities
    for f in near_fac.itertuples():
        ax.plot(f.geometry.x, f.geometry.y, marker="s", ms=11, mfc="none",
                mec="#00ff66", mew=2.4, zorder=7)
        ax.annotate(f.name, (f.geometry.x, f.geometry.y),
                    xytext=(9, 9), textcoords="offset points", color="#00ff66",
                    fontsize=8, weight="bold", zorder=8)

    for n, r in enumerate(rows.itertuples(), start=1):
        ax.plot(r.geometry.x, r.geometry.y, marker="o", ms=15, mfc="none",
                mec=POINT_COLOR, mew=3.0, zorder=9)
        ax.annotate(str(n), (r.geometry.x, r.geometry.y), color=POINT_COLOR,
                    fontsize=13, weight="bold", ha="center", va="center", zorder=10)

    _add_basemap(ax, crs, "satellite")

    legend = [
        Line2D([], [], color=FM_COLOR, lw=3.4, label="force main (this system)"),
        Line2D([], [], color=FM_COLOR, lw=2.0, alpha=0.32, label="other force main"),
        Line2D([], [], color=GRAVITY_COLOR, lw=1.6,
               label="gravity main (arrow = flow direction)"),
        Line2D([], [], color=POINT_COLOR, marker="o", ms=10, mfc="none", mew=2.5,
               ls="", label="review question"),
        Line2D([], [], color="#00ff66", marker="s", ms=9, mfc="none", mew=2,
               ls="", label="lift station / plant"),
    ]
    ax.legend(handles=legend, loc="upper left", fontsize=8, framealpha=0.75)

    lines = [f"SYSTEM {system}   verdict: {rows.VERDICT.iloc[0]}"]
    for n, r in enumerate(rows.itertuples(), start=1):
        lines.append(f"{n}. {r.FM_ID} ({r.FM_END} end)  gap {r.GAP_FT:.1f} ft  "
                     f"reads as: {r.READS_AS}")
    ax.set_title("\n".join(lines), fontsize=9, loc="left")
    ax.set_xticks([]); ax.set_yticks([])
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    main()
