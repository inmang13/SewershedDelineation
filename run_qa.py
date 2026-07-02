"""
Standalone QA runner — use this to audit a network before delineating.
Usage: python run_qa.py --config config.yaml
"""

import argparse
import csv
from pathlib import Path

from src.config import load_config
from src.network_qa import run_qa
from src.pdf_maps import generate_qa_maps
from src.qc_output import write_qc_flags_gpkg


def main():
    parser = argparse.ArgumentParser(description="Run network QA on gravity mains.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Cap the number of PDF map pages, sampled across flag types "
             "(default: 10). Pass 0 for all configured flags.",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    print("Config loaded.")

    print("Running network QA...")
    pipes_repaired, manholes, flags = run_qa(cfg)

    # --- Summary ---
    from collections import Counter
    type_counts = Counter(f["flag_type"] for f in flags)
    sev_counts = Counter(f["severity"] for f in flags)
    status_counts = pipes_repaired["QA_STATUS"].value_counts().to_dict()

    print(f"\n{'='*50}")
    print(f"QA SUMMARY — {len(pipes_repaired)} total pipes")
    print(f"{'='*50}")
    for status, count in status_counts.items():
        print(f"  {status:<20} {count:>6}")
    print(f"\n  Total flags: {len(flags)}")
    for ft, count in type_counts.most_common():
        print(f"    {ft:<30} {count:>4}")
    n_resolved = sum(1 for f in flags if f.get("review_status") == "resolved")
    n_rr_open = sum(1 for f in flags if f["severity"] == "review_required"
                    and f.get("review_status") != "resolved")
    print(f"\n  review_required: {sev_counts.get('review_required', 0)} "
          f"({n_rr_open} open)")
    print(f"  warning:         {sev_counts.get('warning', 0)}")
    if n_resolved:
        print(f"  resolved by review file: {n_resolved}")
    print(f"{'='*50}\n")

    # Windows locks files that are open in Excel/QGIS/a PDF viewer. Each output
    # is written independently so one locked file doesn't kill the rest of the
    # run; locked paths are reported together at the end.
    locked = []

    # --- Write repaired shapefile ---
    repaired_path = str(
        Path(cfg["outputs"]["output_polygon"]).parent / "gravity_mains_repaired.shp"
    )
    try:
        pipes_repaired.to_file(repaired_path)
        print(f"QA-tagged shapefile: {repaired_path}")
    except PermissionError:
        locked.append(repaired_path)

    # --- Write flags CSV ---
    flags_path = cfg["outputs"]["flags_report"].replace(
        "flags.csv", "network_qa_flags.csv"
    )
    try:
        _write_flags_csv(flags, flags_path)
        print(f"Flags report:       {flags_path}")
    except PermissionError:
        locked.append(flags_path)

    # --- Write flags as spatial layers (GeoPackage) for GIS review ---
    # replace=True: this is a fresh QA run, so wipe stale network layers first.
    # Delineation flags append to the same file later (run_polygon_output).
    gpkg_path = cfg["outputs"].get("qc_flags_gpkg")
    if gpkg_path:
        try:
            layers = write_qc_flags_gpkg(flags, gpkg_path,
                                         cfg["parameters"]["crs"], replace=True)
            print(f"QC flags GPKG:      {gpkg_path} ({len(layers)} layers)")
        except PermissionError:
            locked.append(gpkg_path)

    # --- Write large-cycle suspect CSV (member pipes of big tangles) ---
    suspect_rows = [m for f in flags if f.get("member_pipes") for m in f["member_pipes"]]
    if suspect_rows:
        suspects_path = cfg["outputs"]["large_cycle_suspects"]
        try:
            _write_suspects_csv(suspect_rows, suspects_path)
            n_suspect = sum(1 for r in suspect_rows if r["is_suspect"])
            print(
                f"Large-cycle suspects: {suspects_path} "
                f"({len(suspect_rows)} member pipes, {n_suspect} prime suspects)"
            )
        except PermissionError:
            locked.append(suspects_path)

    # --- Write PDF maps (only for configured flag types) ---
    pdf_path = cfg["outputs"]["flag_maps_pdf"].replace(
        "flag_maps.pdf", "network_qa_maps.pdf"
    )
    pdf_types = set(cfg["parameters"]["pdf_flag_types"])
    # Resolved flags stay in the CSV/GPKG for the record but need no map page —
    # the maps exist for human review, and these are already reviewed.
    in_pdf_types = [f for f in flags if f["flag_type"] in pdf_types]
    mapped_flags = [f for f in in_pdf_types
                    if f.get("review_status") != "resolved"]
    n_bulk = len(flags) - len(in_pdf_types)
    n_resolved_skipped = len(in_pdf_types) - len(mapped_flags)

    if args.max_pages and len(mapped_flags) > args.max_pages:
        # Preview mode: spread the cap across flag types so the sample is
        # representative rather than all of whichever type sorts first.
        n_eligible = len(mapped_flags)
        mapped_flags = _sample_across_types(mapped_flags, args.max_pages)
        print(
            f"PREVIEW: generating {len(mapped_flags)} of {n_eligible} "
            f"map-eligible flags (sampled across types)..."
        )
    else:
        print(
            f"Generating {len(mapped_flags)} map pages "
            f"({n_bulk} bulk flags are CSV-only, "
            f"{n_resolved_skipped} resolved flags skipped)..."
        )
    try:
        generate_qa_maps(
            pipes_repaired,
            manholes,
            mapped_flags,
            pdf_path,
            cfg["parameters"]["flag_map_context_buffer_ft"],
            cfg["parameters"].get("basemap_style"),
        )
    except PermissionError:
        # Windows locks a PDF while it is open in a viewer.
        locked.append(pdf_path)
    else:
        print(f"Flag maps PDF:      {pdf_path}")

    if locked:
        print("\nERROR: could not write these outputs — each file is open in "
              "another program (Excel / QGIS / PDF viewer). Close them and "
              "re-run; everything else above was written successfully.")
        for p in locked:
            print(f"  LOCKED: {p}")
        raise SystemExit(1)


def _write_suspects_csv(rows: list[dict], path: str) -> None:
    """Member pipes of large cyclic tangles, with invert/slope suspects marked."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["scc_id", "pipe_id", "is_suspect", "suspect_reason", "x", "y"],
        )
        writer.writeheader()
        # Suspects first within each SCC, so the actionable pipes are at the top.
        for r in sorted(rows, key=lambda r: (r["scc_id"], not r["is_suspect"])):
            writer.writerow(r)


def _sample_across_types(flags: list[dict], max_pages: int) -> list[dict]:
    """Round-robin a sample across flag types so a preview shows variety."""
    from collections import defaultdict, OrderedDict

    by_type = OrderedDict()
    for f in flags:
        by_type.setdefault(f["flag_type"], []).append(f)

    sampled = []
    # Round-robin: take one from each type in turn until we hit the cap.
    while len(sampled) < max_pages and any(by_type.values()):
        for ft in list(by_type.keys()):
            if by_type[ft]:
                sampled.append(by_type[ft].pop(0))
                if len(sampled) >= max_pages:
                    break
    return sampled


def _write_flags_csv(flags: list[dict], path: str) -> None:
    if not flags:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["flag_type", "severity", "pipe_id", "x", "y",
                        "description", "review_status", "review_comment"],
        )
        writer.writeheader()
        for flag in flags:
            geom = flag["geometry"]
            writer.writerow({
                "flag_type": flag["flag_type"],
                "severity": flag["severity"],
                "pipe_id": flag["pipe_id"],
                "x": round(geom.x, 2),
                "y": round(geom.y, 2),
                "description": flag["description"],
                "review_status": flag.get("review_status", ""),
                "review_comment": flag.get("review_comment", ""),
            })


if __name__ == "__main__":
    main()
