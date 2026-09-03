"""
Force-main (pressurized main) ingest and shared vocabulary.

The gravity pipeline stops at a force-main discharge point, so every subbasin
that reaches a node through a lift station is missing from the sewershed.
Closing that gap is a four-phase pipeline, one module per phase:

    force_mains.py          Phase 1 — ingest and filter (this module),
                             plus the flag/verdict vocabulary and defaults
                             every later phase shares.
    force_main_topology.py  Phase 2 — force-main-only topology.
    force_main_classify.py  Phase 3 — classify termini against the directed
                             gravity graph.
    force_main_review.py    Phase 4 — review file and QC geometry.

Nothing here changes topology. A run produces a review file and a QC
GeoPackage, and a human decides what is real before the graph ever sees a
force main. Wiring confirmed connectivity into `graph_builder` is a later
step, in `force_main_wiring.py`.

Why direction has to be inferred
--------------------------------
The layer ships no FROMMH/TOMH, no invert elevations, and its Z values are 0.0
on 6297 of 6303 vertices — unusable. Geometry direction is not a documented
convention for pressurized mains the way it is for the gravity layer, so it
cannot be trusted either.

What *is* available is the already-directed gravity graph. A pump station is
where the gravity network ends (flow enters the wet well and stops), and a
discharge manhole is a gravity node that still has flow leaving it — very often
a `start_only` headwater that appears to start from nowhere precisely because a
force main feeds it. So:

    terminus on a gravity node with out_degree == 0  ->  wet well  (suction)
    terminus on a gravity node with out_degree  > 0  ->  discharge

Measured on the city's layer, this resolves ~78% of the components that touch
gravity at two ends into exactly one wet well and one discharge. The rest are
reported as ambiguous rather than guessed at. See
`docs/force_main_integration_plan.md` for the validation numbers and for the
open request to the city for a pump-station point layer, which would replace
this inference with a direct lookup.

Output contract
---------------
`QC/force_main_review.csv` deliberately uses the same columns as
`QC/qa_review_decisions.csv` (`flag_type, pipe_id, x, y, decision, radius_ft,
target, comment`) plus diagnostic columns. A reviewer sets `decision=snap` on
the rows they accept; those rows then feed `node_layer.snap_endpoints` pass 3
through the existing `qa_review.manual_snaps` route, so force mains join the one
shared topology instead of getting a parallel snap path of their own. Extra
columns are ignored by `qa_review.load_review_decisions`.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely import force_2d


# Review-file flag types. Named so they cannot collide with the gravity flag
# types already in QC/qa_review_decisions.csv.
FLAG_JUNCTION   = "fm_junction_candidate"
FLAG_NO_CONTACT = "fm_no_gravity_contact"
FLAG_AMBIGUOUS  = "fm_direction_ambiguous"

# A "discharge" that is really the far side of a pump station. Some stations
# are drawn as a wet well manhole joined by a few feet of gravity pipe to a
# second manhole that continues downstream — the out-degree rule sees flow
# continuing past that point and calls it a discharge, when the whole thing is
# still inside the station and there is no real second connection. Confirmed
# 4 times in the 2026-08-02/03 map review (East End, Geer St x2, a stub pipe
# by Geer St): every case was a short incident pipe sitting right next to a
# confirmed station. See docs/force_main_ai_review_trends.md.
FLAG_STATION_ADJACENT = "fm_station_adjacent_discharge"

# Component verdicts, defined once. `review_rows` and any future consumer read
# the sets below rather than re-listing strings — the vocabulary drifted between
# the docstring and a hard-coded literal the first time it lived in two places.
VERDICT_RESOLVED = "resolved"

# The component reaches a confirmed treatment plant. Like `resolved` this needs
# no review — the network is supposed to end there (see `terminal_facilities`).
VERDICT_TERMINAL = "terminates_at_facility"
VERDICT_SETTLED = {VERDICT_RESOLVED, VERDICT_TERMINAL}

# Terminus `contact` values that count as a real attachment when rolling up a
# component verdict. "connected" is a within-tolerance snap; "facility" is a
# confirmed treatment plant or pump station, which is stronger evidence, not
# weaker. "candidate" and "none" are neither.
CONFIRMED_CONTACTS = ("connected", "facility")

# Attachment failures: the component cannot be placed on the gravity network at
# all, so direction never comes up. These are junction problems.
VERDICT_UNATTACHED = {
    "isolated",          # no terminus reaches gravity within the review radius
    "unconnected_only",  # contacts exist, but only as unaccepted candidates
}

# Direction failures: the component attaches, but the wet-well/discharge rule
# could not settle it. These are what `fm_direction_ambiguous` marks.
VERDICT_DIRECTION_UNRESOLVED = {
    "no_discharge",      # touches gravity, but every contact is a terminal sink
    "no_wetwell",        # a discharge, but no terminal sink to pump from
    "multi_discharge",   # two or more contacts have downstream gravity
}

# NOT a direction failure: a loop in the pipe geometry.
#
# A pressurized main is modelled as ONE edge, wet well -> discharge; its interior
# path never enters a trace. So flow direction is fixed by the component's ENDS,
# not by the route between them, and going either way round a loop lands in the
# same place. Grace, 2026-08-06: "the lines make a loop, but the arrows do not
# make a loop."
#
# This was a `cyclic` verdict that blocked 7 systems and 19.2 mi - 35% of the
# pressurized network - on a condition with no bearing on the answer. Mapping the
# loops showed all 13 of them were 32-192 ft valve/manifold arrangements inside
# pump-station and plant yards. `n_cycles` is still reported per component,
# because a loop is still worth seeing; it just no longer decides anything.

VERDICT_UNRESOLVED = VERDICT_UNATTACHED | VERDICT_DIRECTION_UNRESOLVED

# Widest gap that gets a pre-filled merge radius in the review file. Past this,
# a pre-filled radius is a footgun, not a convenience — see `review_rows`.
MAX_PREFILLED_RADIUS_GAP_FT = 25.0

# Inclusion defaults, applied when parameters.force_main_include is absent or
# only partially specified. Both repos' config.yaml set these explicitly; these
# values are the fallback so the module is usable without a config, matching how
# `graph_builder` defaults `snap_gap_search_radius_ft`.
DEFAULT_INCLUDE = {
    "lifecycle":       ["Active"],
    "exclude_owner":   ["PVT"],
    "min_diameter_in": 4.0,
    # Case-insensitive substrings matched against COMMENT. A bypass port is a
    # connection point for portable pumps during maintenance — it carries no
    # routine flow, so leaving it in fabricates connectivity that only exists
    # when a crew is on site. Meter bypasses are deliberately NOT excluded: a
    # meter bypass carries real flow when the meter is offline, so it is live
    # conveyance. Each pattern's hit count is reported separately so a pattern
    # that silently matches nothing is visible.
    "exclude_comment": ["bypass port", "by-pass port", "bypass pumping",
                        "by-pass pumping", "bypass pump pipe",
                        "emergency bypass"],
    # Individual FACILITYIDs a reviewer has confirmed are wrong/redundant and
    # should be dropped outright (e.g. a duplicate stub inside a station that
    # the direction rule keeps misreading as a discharge). Distinct from
    # small-stub pruning: this is a per-pipe human call, not a rule.
    "exclude_facilityid": [],
}


def _read_csv_rows(path):
    """
    Read a review CSV, tolerating what Excel does to it on a Windows machine.

    Excel writes cp1252, and a curly apostrophe typed in a comment field then
    makes a utf-8 read die mid-file. A review loop that cannot survive Excel is
    not a review loop, so fall back rather than fail.

    Shared by every later phase that reads a reviewer-edited CSV
    (`force_main_topology.load_manual_joins`,
    `force_main_classify.load_direction_overrides`,
    `force_main_review._load_prior_decisions`) — one Excel-survival trick,
    not three.
    """
    import csv
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            with open(path, newline="", encoding=encoding) as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path}: could not decode as utf-8 or cp1252")


# ---------------------------------------------------------------------------
# Phase 1 — load and filter
# ---------------------------------------------------------------------------

def load_force_mains(cfg: dict) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """
    Read the force-main layer, reproject to the working CRS, drop Z, and apply
    the inclusion filter.

    Returns (kept, drop_report). `drop_report` has one row per exclusion reason
    with a count, so a run can state what it threw away instead of silently
    shrinking the network. A DIAMETER of 0 is missing data, not a small pipe —
    it fails `min_diameter_in` and is reported under its own reason, and each
    `exclude_comment` pattern gets its own reason for the same purpose.

    Raises FileNotFoundError if `inputs.force_main_shapefile` is set but absent.
    Callers handle the "key not set" case themselves (gravity-only mode).
    """
    path = cfg["inputs"].get("force_main_shapefile")
    if not path:
        raise ValueError("inputs.force_main_shapefile is not set")
    if not Path(path).exists():
        raise FileNotFoundError(f"force main layer not found — {path}")

    rules = dict(DEFAULT_INCLUDE)
    rules.update(cfg["parameters"].get("force_main_include") or {})

    fm = gpd.read_file(path).to_crs(cfg["parameters"]["crs"])
    fm["geometry"] = force_2d(fm.geometry)   # Z is all zeros; carrying it breaks KD-trees
    n_total = len(fm)

    reasons = []

    def _drop(predicate, reason: str) -> None:
        """
        Record and apply one exclusion. `predicate` takes the SURVIVING frame and
        returns a boolean mask — a callable rather than a mask because each drop
        rebinds `fm`, so a mask built earlier would be indexed against a frame
        that no longer exists.
        """
        nonlocal fm
        mask = predicate(fm)
        n = int(mask.sum())
        if n:
            reasons.append({"reason": reason, "n": n,
                            "facilityids": ", ".join(
                                sorted(fm.loc[mask, "FACILITYID"].astype(str))[:20])})
        fm = fm.loc[~mask].copy()

    def _diameter(frame):
        return pd.to_numeric(frame["DIAMETER"], errors="coerce")

    lifecycle = rules.get("lifecycle")
    if lifecycle:
        _drop(lambda f: ~f["LIFECYCLES"].isin(lifecycle),
              f"lifecycle not in {lifecycle}")

    exclude_owner = rules.get("exclude_owner")
    if exclude_owner:
        _drop(lambda f: f["OWNER"].isin(exclude_owner),
              f"owner in {exclude_owner}")

    min_dia = rules.get("min_diameter_in")
    if min_dia is not None:
        # DIAMETER = 0 gets its own reason rather than folding into the size cut:
        # a zero is missing data, and reporting it as "too small" would hide a
        # data-quality problem behind a modelling decision.
        _drop(lambda f: _diameter(f).isna(), "DIAMETER missing/non-numeric")
        _drop(lambda f: _diameter(f) == 0,
              "DIAMETER = 0 (missing data, not a small pipe)")
        _drop(lambda f: _diameter(f) < min_dia,
              f"DIAMETER < {min_dia} in (private grinder lateral)")

    # Comment exclusions run per pattern, not as one combined regex, so the drop
    # report attributes each removal to the pattern that caused it — a pattern
    # matching zero rows is then obvious rather than silently inert.
    for pattern in (rules.get("exclude_comment") or []):
        _drop(lambda f, p=pattern: f["COMMENT"].fillna("").str.contains(
                  p, case=False, regex=False),
              f"COMMENT contains '{pattern}'")

    exclude_fids = rules.get("exclude_facilityid")
    if exclude_fids:
        fid_set = {str(f) for f in exclude_fids}
        _drop(lambda f: f["FACILITYID"].astype(str).isin(fid_set),
              f"FACILITYID in {sorted(fid_set)} (reviewer-confirmed drop)")

    _drop(lambda f: f.geometry.is_empty | f.geometry.isna(), "empty geometry")

    fm = fm.reset_index(drop=True)
    report = pd.DataFrame(reasons, columns=["reason", "n", "facilityids"])
    report.attrs["n_total"] = n_total
    report.attrs["n_kept"] = len(fm)
    return fm, report


def to_geopackage(shapefile_path: str, gpkg_path: str, crs: str) -> int:
    """
    Convert the delivered shapefile to a GeoPackage matching the other `data/`
    layers (and leaving the ArcGIS `.sr.lock` / `.shp.xml` clutter behind).
    Returns the feature count written. No filtering happens here — the raw layer
    is preserved so the inclusion rule stays a pipeline decision, not a
    baked-in one.
    """
    gdf = gpd.read_file(shapefile_path).to_crs(crs)
    gdf["geometry"] = force_2d(gdf.geometry)
    Path(gpkg_path).parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(gpkg_path, driver="GPKG")
    return len(gdf)
