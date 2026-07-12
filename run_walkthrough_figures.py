"""
CLI runner: de-identified figures for docs/demographics_walkthrough.md.

Renders the real demographic outputs as committed-safe PNGs under docs/img/.
De-identification rules (decision_log 2026-07-12) are enforced here by
construction — the figures carry:
  - NO basemap (no tiles, no streets, no place names)
  - NO Asset/manhole IDs, no site labels of any kind
  - NO coordinates: axes are turned off entirely (projected State Plane
    coordinates would locate the study area as surely as a street map)
Only the catchment shapes and their attribute colors remain.

Usage:  python run_walkthrough_figures.py [--config config.yaml]
Requires a prior run of run.py + run_demographics.py (reads their outputs).
"""

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                     # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Render de-identified walkthrough figures.")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    base = Path(cfg["_base_dir"])
    shp = base / cfg["outputs"]["demographics_shp"]
    if not shp.exists():
        sys.exit(f"Demographics output not found: {shp}\n"
                 "Run run.py and run_demographics.py first.")

    g = gpd.read_file(shp)
    out_dir = base / "docs" / "img"
    out_dir.mkdir(parents=True, exist_ok=True)

    panels = [
        ("med_inc", "Median household income ($)", "viridis"),
        ("pov_rate", "Poverty rate", "magma_r"),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(11, 5.5))
    for ax, (col, title, cmap) in zip(axes, panels):
        g.plot(column=col, ax=ax, cmap=cmap, edgecolor="white", linewidth=0.6,
               legend=True, legend_kwds={"shrink": 0.7},
               missing_kwds={"color": "#d9d9d9", "hatch": "//",
                             "label": "no population"})
        ax.set_title(title, fontsize=11)
        ax.set_axis_off()          # de-identification: no coordinates
    fig.suptitle(f"Per-catchment demographic estimates ({len(g)} sewersheds) — "
                 "de-identified", fontsize=12)
    fig.tight_layout()
    out = out_dir / "demographics_choropleth.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
