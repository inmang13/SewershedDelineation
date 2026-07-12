"""
CLI runner: align shared borders across the final sewershed polygons.

Usage:
    python run_seam_align.py [--config config.yaml] [--tol 50]
                             [--gpkg QC/competing_pipe_review.gpkg]

Reads the full-rules per-site boundaries (the `boundary` layer written by
run_competing_review.py — exclude/split/buffer-assign applied), then:

  1. align_seams       snap each polygon to its already-processed neighbours so
                       shared borders coincide exactly (tol ft; boundary.py)
  2. resolve_overlaps  equidistant midline split of overlapping basins — OPTIONAL,
                       off unless parameters.resolve_overlaps_enabled
  3. bridge_parts      stitch a site's disjoint parts across a wide gap (runs
                       last so earlier passes can't re-fragment merged parts)

Verifies the pass is cosmetic: per-site IoU vs truth before/after (deltas
should be ~0), and reports the coincident-seam length per adjacent pair before
vs after (should jump from ~0 to the seam length). Writes
output/sewershed_final.gpkg (layers: boundary, truth).
"""

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                       # noqa: E402
from boundary import (align_seams, resolve_overlaps, bridge_parts,  # noqa: E402
                      SQFT_PER_ACRE)


def _total_overlap_ac(bounds: dict) -> float:
    """Total pairwise overlap area (acres) across all site polygons."""
    tot = 0.0
    ids = sorted(bounds)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            ga, gb = bounds[a], bounds[b]
            if ga is None or gb is None or ga.is_empty or gb.is_empty:
                continue
            tot += ga.intersection(gb).area
    return tot / SQFT_PER_ACRE
from validation import iou                           # noqa: E402


def coincident_seams(bounds: dict, near_ft: float) -> pd.DataFrame:
    """
    For every pair of polygons whose outlines come within near_ft, measure the
    length of exactly-shared border (intersection of the two boundary lines).
    Before alignment this is ~0 even where catchments abut; after, it is the
    seam length.
    """
    rows = []
    ids = sorted(bounds)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            ga, gb = bounds[a], bounds[b]
            if ga is None or gb is None or ga.is_empty or gb.is_empty:
                continue
            la, lb = ga.boundary, gb.boundary
            if la.distance(lb) > near_ft:
                continue
            rows.append({"a": a, "b": b,
                         "shared_ft": round(la.intersection(lb).length, 1)})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser(description="Seam alignment post-pass.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--gpkg", default="QC/competing_pipe_review.gpkg",
                    help="GeoPackage holding the full-rules `boundary` + `truth` layers")
    ap.add_argument("--tol", type=float, default=50.0,
                    help="seam snap tolerance (ft)")
    ap.add_argument("--bridge", type=float, default=None,
                    help="max inter-part gap to bridge per site (ft); "
                         "default from config parameters.bridge_parts_max_gap_ft")
    ap.add_argument("--split-step", type=float, default=50.0,
                    help="densify step (ft) for the equidistant overlap split")
    args = ap.parse_args()

    cfg = load_config(args.config)
    crs = cfg["parameters"]["crs"]
    gpkg = Path(cfg["_base_dir"]) / args.gpkg

    bnd = gpd.read_file(gpkg, layer="boundary").to_crs(crs)
    tru = gpd.read_file(gpkg, layer="truth").to_crs(crs)
    print(f"Loaded {len(bnd)} boundaries from {gpkg}")

    bounds = {r["SiteID"]: r.geometry for _, r in bnd.iterrows()}
    truth = {r["SiteID"]: r.geometry for _, r in tru.iterrows()}
    tract = {r["SiteID"]: r.get("tract", "") for _, r in bnd.iterrows()}

    seams_before = coincident_seams(bounds, args.tol)

    aligned = align_seams(bounds, tol_ft=args.tol)

    # Optional equidistant midline split of overlapping basins (Grace 2026-07-08).
    # OFF by default: the only large overlaps are nested sub-basins (intentional)
    # or the 3.02/1.02 interceptor question (a source-data issue, not geometry),
    # and the sequential Voronoi split fragments large polygons. Kept behind a
    # flag for when a genuine peer-overlap case appears.
    if cfg["parameters"].get("resolve_overlaps_enabled", False):
        ov_before = _total_overlap_ac(aligned)
        aligned = resolve_overlaps(aligned, step_ft=args.split_step)
        ov_after = _total_overlap_ac(aligned)
        print(f"resolve_overlaps: pairwise overlap {ov_before:.1f} -> "
              f"{ov_after:.1f} ac")

    # Bridge LAST: seal a site's disjoint parts across a wide gap, after
    # align/split have finished moving edges (running it earlier lets the later
    # passes re-fragment what it merged).
    bridge_gap = (args.bridge if args.bridge is not None
                  else cfg["parameters"].get("bridge_parts_max_gap_ft", 0.0))
    if bridge_gap and bridge_gap > 0:
        nb = 0
        for sid, g in aligned.items():
            stitched = bridge_parts(g, bridge_gap)
            if stitched is not g and stitched is not None:
                before = len(getattr(g, "geoms", [g]))
                after = len(getattr(stitched, "geoms", [stitched]))
                if after < before:
                    nb += 1
                aligned[sid] = stitched
        print(f"bridge_parts (gap <= {bridge_gap:g} ft): {nb} site(s) merged parts")

    seams_after = coincident_seams(aligned, args.tol)

    # --- verify cosmetic: IoU before/after -------------------------------
    rows = []
    for sid in sorted(bounds):
        t = truth.get(sid)
        i0 = iou(bounds[sid], t) if t is not None else float("nan")
        i1 = iou(aligned[sid], t) if t is not None else float("nan")
        rows.append({"site": sid, "tract": tract.get(sid, ""),
                     "iou_before": round(i0, 4), "iou_after": round(i1, 4),
                     "delta": round(i1 - i0, 4)})
    df = pd.DataFrame(rows)
    print("\nPer-site IoU (before -> after seam align + overlap split):")
    print(df.to_string(index=False))
    print(f"\nMedian IoU: {df['iou_before'].median():.4f} -> "
          f"{df['iou_after'].median():.4f}   "
          f"max |delta| {df['delta'].abs().max():.4f}")

    # --- seam coincidence report -----------------------------------------
    merged = seams_before.merge(seams_after, on=["a", "b"], how="outer",
                                suffixes=("_before", "_after")).fillna(0)
    merged = merged.sort_values("shared_ft_after", ascending=False)
    print(f"\nCoincident shared border per adjacent pair (ft), tol {args.tol:g}:")
    print(merged.to_string(index=False))

    # --- largest remaining overlaps (should be ~0 after the split) -------
    resid = []
    ids = sorted(aligned)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            ga, gb = aligned[a], aligned[b]
            if ga is None or gb is None or ga.is_empty or gb.is_empty:
                continue
            ov = ga.intersection(gb).area / SQFT_PER_ACRE
            if ov > 0.01:
                resid.append((tract.get(a, a), tract.get(b, b), round(ov, 2)))
    resid.sort(key=lambda r: -r[2])
    print(f"\nResidual overlaps > 0.01 ac ({len(resid)} pairs):")
    for a, b, ov in resid[:10]:
        print(f"  {a} / {b}: {ov} ac")

    # --- write final output ----------------------------------------------
    out = Path(cfg["_base_dir"]) / "output" / "sewershed_final.gpkg"
    fin = gpd.GeoDataFrame(
        [{"SiteID": sid, "tract": tract.get(sid, ""),
          "iou": r["iou_after"]}
         for sid, r in zip(df["site"], df.to_dict("records"))],
        geometry=[aligned[sid] for sid in df["site"]], crs=crs)
    fin.to_file(out, layer="boundary", driver="GPKG")
    tru.to_file(out, layer="truth", driver="GPKG")
    print(f"\nWrote {out} (layers: boundary, truth)")


if __name__ == "__main__":
    main()
