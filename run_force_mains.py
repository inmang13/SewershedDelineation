"""
Force-main review runner - steps 1 and 2 of the force-main integration.

Reads the pressurized-main layer, applies the inclusion filter, builds the
force-main-only topology, and classifies every component terminus against the
directed gravity graph. Writes a review CSV and a QC GeoPackage.

**This runner changes no topology.** It answers "where would force mains attach,
and where is that unclear?" so the junctions can be reviewed before the graph
ever sees a force main. Wiring into `graph_builder` is a later step.

Usage:
    python run_force_mains.py --config config.yaml
    python run_force_mains.py --config config.yaml --convert data/snForceMain/Publicworks_PUBLICWORKS_snForceMain.shp

`--convert` writes `inputs.force_main_shapefile` from the delivered shapefile
(GeoPackage, working CRS, Z stripped) and exits - that is step 1, run once.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                                  # noqa: E402
from graph_builder import load_graph_from_config, build_node_index  # noqa: E402
from force_mains import (                                       # noqa: E402
    load_force_mains, to_geopackage, build_topology, classify_termini,
    component_verdicts, review_rows, write_qc_gpkg, load_manual_joins,
    prune_small_stubs, settle_reviewed_rows, write_review_shapefile,
    flag_station_adjacent_discharges, VERDICT_SETTLED,
    load_direction_overrides, apply_direction_overrides,
)
from terminal_facilities import (                                # noqa: E402
    load_facilities, match_facilities, apply_to_termini, facility_flags,
)
from force_main_wiring import build_force_main_edges              # noqa: E402


def main():
    ap = argparse.ArgumentParser(
        description="Review force-main attachment to the gravity network.")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--convert", metavar="SHAPEFILE",
                    help="convert a delivered shapefile to "
                         "inputs.force_main_shapefile (GeoPackage) and exit")
    args = ap.parse_args()

    cfg = load_config(args.config)
    params = cfg["parameters"]
    crs = params["crs"]

    fm_path = cfg["inputs"].get("force_main_shapefile")
    if not fm_path:
        # Not an error: gravity-only is the documented default and how the
        # public repo runs. Exit 0 so a CI step or script doesn't read it as a
        # failure.
        print("inputs.force_main_shapefile is not set - this config is "
              "gravity-only. Nothing to review.")
        return

    # --- Step 1: one-time conversion -------------------------------------
    if args.convert:
        n = to_geopackage(args.convert, fm_path, crs)
        print(f"Converted {args.convert}\n       -> {fm_path}  ({n} features, {crs})")
        return

    # --- Load and filter --------------------------------------------------
    fm, dropped = load_force_mains(cfg)
    n_total = dropped.attrs["n_total"]
    print(f"\nForce mains: {n_total} read, {len(fm)} kept "
          f"({fm.geometry.length.sum() / 5280:.1f} mi)")
    for r in dropped.itertuples(index=False):
        print(f"  dropped {r.n:>4}  {r.reason}")
    if fm.empty:
        sys.exit("Inclusion filter removed every force main - check "
                 "parameters.force_main_include.")

    # --- Gravity graph (unchanged; force mains are not added) -------------
    # wire_force_mains=False on purpose: this runner GENERATES the edge list, so
    # reading last run's copy back in would classify termini against a graph
    # that already contains the answer.
    print("\nBuilding gravity graph...")
    G, _pipes = load_graph_from_config(cfg, wire_force_mains=False)
    index = build_node_index(G)
    print(f"  {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    # --- Topology + classification ----------------------------------------
    comp_tol   = params.get("force_main_component_tol_ft", 1.0)
    snap_tol   = params.get("force_main_snap_tolerance_ft", 10.0)
    review_rad = params.get("force_main_review_radius_ft", 500.0)

    joins = load_manual_joins(cfg)
    if joins:
        print(f"Force-main joins: {len(joins)} human-confirmed intra-network "
              "repair(s) applied before clustering")
    topo = build_topology(fm, comp_tol, manual_joins=joins)

    # Small city-owned mains are kept unless they dangle (Grace, 2026-08-02).
    # Needs the topology, so it runs here rather than in the inclusion filter,
    # and the topology is rebuilt from what survives.
    rules = params.get("force_main_include") or {}
    small_dia = rules.get("small_diameter_in")
    if small_dia:
        fm, stubs = prune_small_stubs(
            fm, topo, index, small_dia,
            rules.get("small_stub_max_component_ft", 25.0),
            params.get("force_main_snap_tolerance_ft", 10.0))
        if not stubs.empty:
            print(f"\nSmall-main stubs dropped: {len(stubs)} component(s), "
                  f"{int(stubs.n_pipes.sum())} pipe(s)")
            for r in stubs.itertuples(index=False):
                print(f"    {r.facilityids} - {r.length_ft:.0f} ft long, "
                      f"{r.min_gap_ft:.0f} ft from the nearest gravity main")
            topo = build_topology(fm, comp_tol, manual_joins=joins)

    termini = classify_termini(topo, G, index, snap_tol, review_rad)

    # Confirmed facilities outrank inference: a plant ends the network, a lift
    # station pins the wet well. Applied before verdicts so the roll-up sees them.
    facilities = load_facilities(cfg)
    matches = match_facilities(
        facilities, termini,
        params.get("terminal_facility_plant_tol_ft", 1000.0),
        params.get("terminal_facility_station_tol_ft", 200.0))
    if not facilities.empty:
        roles = facilities.role.value_counts().to_dict()
        print(f"\nTerminal facilities: {len(facilities)} read ({roles})")
        n_ok = int((matches.status == "matched").sum())
        print(f"  matched to a force-main terminus: {n_ok} of {len(matches)}")
        termini = apply_to_termini(termini, matches)

    # A reviewer's ruling outranks both the rule and the facility match - it is
    # the only thing that can close out a facility_direction_conflict, where the
    # evidence genuinely points both ways. Applied after the facility match so it
    # overrides that too, and before the verdicts so the roll-up sees it.
    termini, overridden = apply_direction_overrides(
        termini, load_direction_overrides(cfg),
        params.get("force_main_direction_override_tol_ft", 25.0))
    if not overridden.empty:
        print(f"\nReviewer direction rulings applied: {len(overridden)}")
        for r in overridden.itertuples(index=False):
            print(f"    ({r.x:.0f}, {r.y:.0f}) {r.was} -> {r.now}"
                  + (f"  [{r.comment}]" if r.comment else ""))

    # Reported AFTER the rulings, so the conflicts listed are the ones still
    # open. Printing them first would name a conflict and then resolve it a line
    # later, which reads as though the ruling did not take.
    if not facilities.empty:
        for f in facility_flags(matches, termini):
            print(f"  [{f['flag_type']}] {f['name']} ({f['dist_ft']:.0f} ft)")

    # A short stub next to a confirmed station reads as a real discharge by the
    # out-degree rule but usually isn't one - see FLAG_STATION_ADJACENT's
    # docstring. Downgrades qualifying rows out of CONFIRMED_CONTACTS so the
    # component verdict below doesn't silently mark them resolved.
    termini, station_adjacent = flag_station_adjacent_discharges(
        termini, topo, fm, facilities,
        params.get("force_main_station_adjacent_max_pipe_ft", 25.0),
        params.get("terminal_facility_station_tol_ft", 200.0))
    if not station_adjacent.empty:
        print(f"\nStation-adjacent discharge misreads caught: "
              f"{len(station_adjacent)} (short stub next to a confirmed "
              "station, flagged for review instead of auto-resolving)")
        for r in station_adjacent.itertuples(index=False):
            print(f"    {r.pipe_id} - {r.pipe_len_ft:.0f} ft pipe, "
                  f"{r.dist_ft:.0f} ft from the station")

    verdicts = component_verdicts(topo, termini, fm)

    print(f"\nTopology (endpoints clustered at {comp_tol} ft): "
          f"{len(topo.components)} components, {len(termini)} termini")
    print(f"Classification (snap {snap_tol} ft, review radius {review_rad} ft):")
    for cls, n in termini.classification.value_counts().items():
        print(f"  {cls:<20} {n:>4}")

    # Report mileage, not just counts. Resolved components skew small and
    # simple; the big pumped systems - the ones that actually move a sewershed -
    # concentrate in the unresolved buckets, so a component count alone
    # overstates how much of the network is settled.
    total_mi = verdicts.length_ft.sum() / 5280
    print("\nComponent verdicts:")
    for v, n in verdicts.verdict.value_counts().items():
        miles = verdicts.loc[verdicts.verdict == v, "length_ft"].sum() / 5280
        share = 100 * miles / total_mi if total_mi else 0.0
        print(f"  {v:<20} {n:>4}  ({miles:5.1f} mi, {share:4.1f}% of length)")
    settled = verdicts.verdict.isin(VERDICT_SETTLED)
    n_resolved = int(settled.sum())
    mi_resolved = verdicts.loc[settled, "length_ft"].sum() / 5280
    print(f"\n  {n_resolved} of {len(verdicts)} components need no review "
          f"(direction rule resolved them, or they end at a facility) -")
    print(f"  but only {mi_resolved:.1f} of {total_mi:.1f} mi "
          f"({100 * mi_resolved / total_mi if total_mi else 0:.0f}% of length). "
          "Review the longest unresolved components first:")
    worst = (verdicts[~settled]
             .nlargest(5, "length_ft")[["comp_id", "verdict", "n_pipes",
                                        "n_cycles", "length_ft"]])
    for r in worst.itertuples(index=False):
        print(f"    comp {r.comp_id:<4} {r.verdict:<18} {r.n_pipes:>3} pipes, "
              f"{r.n_cycles:>2} cycles, {r.length_ft / 5280:5.2f} mi")

    # --- Outputs ----------------------------------------------------------
    rows = review_rows(termini, verdicts, fm, topo,
                       params.get("force_main_max_prefilled_radius_gap_ft", 25.0))
    n_before = len(rows)
    rows, settled = settle_reviewed_rows(
        rows, cfg, params.get("force_main_auto_accept_ft", 0.1))
    if not settled.empty:
        counts = settled.settled_as.value_counts().to_dict()
        print(f"\nSettled without review: {n_before - len(rows)} of {n_before} "
              f"rows ({counts})")
        settled_path = Path(cfg["_base_dir"]) / cfg["outputs"]["force_main_settled"]
        try:
            settled.to_csv(settled_path, index=False, encoding="utf-8-sig")
            print(f"  written to {settled_path} - nothing is dropped silently")
        except PermissionError:
            print(f"  COULD NOT WRITE {settled_path} - close it and re-run.")
    review_path = Path(cfg["_base_dir"]) / cfg["outputs"]["force_main_review"]
    review_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # utf-8-sig, not plain utf-8: Excel on Windows renders a BOM-less
        # utf-8 CSV as mojibake, and this file exists to be opened in Excel.
        rows.to_csv(review_path, index=False, encoding="utf-8-sig")
        print(f"\nReview file:  {review_path}  ({len(rows)} rows, all decisions blank)")
        for ft, n in rows.flag_type.value_counts().items():
            print(f"    {ft:<26} {n:>4}")
    except PermissionError:
        print(f"\nCOULD NOT WRITE {review_path} - close it in Excel and re-run.")

    # --- The wiring artifact ---------------------------------------------
    # Written here, consumed by graph_builder.load_graph_from_config. Only
    # settled components appear; everything still in review is listed in
    # `skipped` with the reason, so the gap between "network" and "wired" is
    # explicit rather than inferred from a count.
    fm_edges, fm_skipped = build_force_main_edges(termini, verdicts, fm, topo)
    edges_path = Path(cfg["_base_dir"]) / cfg["outputs"]["force_main_edges"]
    try:
        fm_edges.to_csv(edges_path, index=False, encoding="utf-8-sig")
        # Per COMPONENT, not per edge: a system with two lift stations on one
        # discharge gets two edges, and summing the column would count its
        # mileage twice.
        wired_mi = (fm_edges.drop_duplicates("comp_id").length_ft.sum() / 5280
                    if not fm_edges.empty else 0.0)
        print(f"\nForce-main edges: {edges_path}  ({len(fm_edges)} edge(s), "
              f"{fm_edges.comp_id.nunique() if not fm_edges.empty else 0} "
              f"system(s), {wired_mi:.1f} mi of pressurized main)")
        print("  Set inputs.force_main_edges to this path to trace through them.")
    except PermissionError:
        print(f"\nCOULD NOT WRITE {edges_path} - close it and re-run.")
    if not fm_skipped.empty:
        held_mi = fm_skipped.length_ft.sum() / 5280
        print(f"  not wired: {len(fm_skipped)} system(s), {held_mi:.1f} mi - "
              "these stay invisible to a trace until reviewed:")
        for reason, grp in fm_skipped.groupby("reason"):
            print(f"    {len(grp):>3}  {reason}")

    shp_path = Path(cfg["_base_dir"]) / cfg["outputs"]["force_main_review_shp"]
    try:
        n_shp = write_review_shapefile(shp_path, crs, rows)
        print(f"Review shapefile: {shp_path}  ({n_shp} points still open)")
    except PermissionError:
        print(f"COULD NOT WRITE {shp_path} - close it in GIS and re-run.")

    gpkg_path = Path(cfg["_base_dir"]) / cfg["outputs"]["force_main_junctions"]
    try:
        layers = write_qc_gpkg(gpkg_path, crs, fm, topo, termini, verdicts)
        print(f"QC GeoPackage: {gpkg_path}  (layers: {', '.join(layers)})")
    except PermissionError:
        print(f"COULD NOT WRITE {gpkg_path} - close it in QGIS and re-run.")

    print("\nNo topology was changed. Review fm_junctions in QGIS, set "
          "decision=snap on the rows you accept, then run the graph-wiring step.")


if __name__ == "__main__":
    main()
