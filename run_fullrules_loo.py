"""
Leave-one-out validation of the FULL-RULES pipeline (delaunay boundary +
nick gate + competing-pipe exclude/split/buffer-assign + fill_uncovered_trace).

Sweeps selection_radius_ft x delaunay_max_edge_ft by driving the real shipped
run_competing_review.py once per combo (config variants writing to scratch, so
the production QC gpkg is untouched), reads each run's per-site IoU from the
`boundary` layer, then reuses validation.loo_reaggregate for the fold logic.

SCOPE / HONEST CAVEAT (printed in the report): this LOO cross-validates ONLY
the sel_r x edge grid pick. The nick-gate thresholds, boundary-method choice,
fill buffer and bridge gap were hand-fit to these 24 sites and stay frozen
across folds — they are invisible to this LOO. A small optimism gap therefore
means the grid pick is stable, NOT that the pipeline generalizes to a new city.
seam-align is excluded (IoU-neutral, max |delta| ~0.01), so the boundary-layer
IoU here is the fair per-site score.

Usage: python run_fullrules_loo.py [--sel 50,100] [--edge 500,750,1000]
"""

import argparse
import subprocess
import sys
import copy
from pathlib import Path

import geopandas as gpd
import pandas as pd
import yaml

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE / "src"))
from validation import loo_reaggregate, report_loo, summarize   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Full-rules LOO sweep.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--sel", default="50,100")
    ap.add_argument("--edge", default="500,750,1000")
    args = ap.parse_args()

    sels = [float(x) for x in args.sel.split(",") if x.strip()]
    edges = [float(x) for x in args.edge.split(",") if x.strip()]

    with open(BASE / args.config) as f:
        base_cfg = yaml.safe_load(f)

    scratch = BASE / "output" / "loo_scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    rows = []
    for sel_r in sels:
        for edge in edges:
            tag = f"sel{sel_r:g}_edge{edge:g}"
            cfg = copy.deepcopy(base_cfg)
            cfg["parameters"]["selection_radius_ft"] = sel_r
            cfg["parameters"]["delaunay_max_edge_ft"] = edge
            # redirect outputs to scratch so the production QC gpkg is untouched
            gpkg_rel = f"output/loo_scratch/{tag}.gpkg"
            csv_rel = f"output/loo_scratch/{tag}.csv"
            cfg["outputs"]["competing_review_gpkg"] = gpkg_rel
            cfg["outputs"]["competing_review_csv"] = csv_rel
            # config MUST live in the project root: load_config resolves input
            # paths relative to the config file's directory.
            cfg_path = BASE / f"config_loo_{tag}.yaml"
            with open(cfg_path, "w") as f:
                yaml.safe_dump(cfg, f)

            print(f"\n=== running full rules: {tag} ===", flush=True)
            r = subprocess.run(
                [sys.executable, str(BASE / "run_competing_review.py"),
                 "--config", str(cfg_path)],
                capture_output=True, text=True)
            med_line = [ln for ln in r.stdout.splitlines() if "Median IoU" in ln]
            print("  " + (med_line[-1] if med_line else "(no median line)"))
            if r.returncode != 0:
                print(r.stderr[-1500:])
                sys.exit(f"run failed for {tag}")

            gpkg = BASE / gpkg_rel
            bnd = gpd.read_file(gpkg, layer="boundary")
            for _, row in bnd.iterrows():
                rows.append({"site": str(row["SiteID"]), "tag": "",
                             "method": "delaunay", "sel_r": sel_r,
                             "param2": edge, "iou": float(row["iou"])})

    for stale in BASE.glob("config_loo_*.yaml"):
        stale.unlink()   # tidy the temp configs

    df = pd.DataFrame(rows)
    out_csv = BASE / "output" / "fullrules_sweep.csv"
    df.to_csv(out_csv, index=False)
    print(f"\nWrote {out_csv} ({len(df)} rows, {df['site'].nunique()} sites, "
          f"{len(sels) * len(edges)} combos)")

    print("\n===== FULL-RULES SWEEP SUMMARY (in-sample) =====")
    print(summarize(df.to_dict("records")).to_string(index=False))

    print("\n===== FULL-RULES LEAVE-ONE-OUT =====")
    report_loo(df)

    print("\nCAVEAT: LOO cross-validates the sel_r x edge grid pick only. "
          "Nick-gate thresholds, method choice, fill buffer and bridge gap were "
          "fit to these 24 sites and are frozen across folds — a small gap means "
          "a stable pick, not out-of-sample generalization (that needs a held-out "
          "city; multi-city = future work).")


if __name__ == "__main__":
    main()
