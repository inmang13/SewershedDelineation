"""
Terminal facilities — treatment plants and pump/lift stations as ground truth.

Two problems in the force-main pipeline are really one problem: not knowing where
the network is *supposed* to end.

  1. A component whose flow terminates at a treatment plant gets the verdict
     `no_discharge` — reported as "we can't find where this goes" when the
     correct answer is "it goes to the plant, and that is the end." On the city's
     network this mislabels the second-largest item on the review worklist.
  2. Which end of a force main is the wet well is *inferred* from gravity
     out-degree (see `force_mains`), because a force main starts at a pump
     discharge flange — a structure that appears in neither the gravity-main nor
     the manhole layer. The inference resolves ~78% of components and fails
     loudly on the rest.

A facility point layer answers both directly. A plant point makes termination a
result rather than a defect; a lift-station point *pins* the suction end instead
of inferring it.

Validated against Grace's hand-compiled the city layer (34 points read off
satellite imagery, 2026-08-02): of the 26 lift stations with a force-main
terminus within 200 ft, **24 sit on a terminus the out-degree rule had already
called a wet well.** That converts the inference from self-consistent to
grounded. The 2 disagreements are worth a look, not a silent override — so a
lift station sitting on a terminus the rule called a *discharge* is reported as
`facility_direction_conflict` rather than quietly flipped.

Why two tolerances
------------------
A facility point dropped from satellite imagery marks the middle of the site,
not the pipe end. A lift station is a small structure — its point lands within
tens of feet of the wet well (median 34 ft, measured). A treatment plant is a
campus: South the city WRF's point sits 842 ft from the nearest force-main
terminus, and North the city's 242 ft. One tolerance cannot serve both, so plants
and stations get their own.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import geopandas as gpd
from scipy.spatial import cKDTree

# Facility roles. A plant ends the network; a station is a wet well mid-network.
PLANT = "plant"
STATION = "station"
IGNORED = "ignored"

# Default name patterns (case-insensitive substrings). Config-driven because
# every utility names its assets differently — this is the city's vocabulary, not
# a universal one.
DEFAULT_PATTERNS = {
    PLANT:   ["reclamation", "wastewater", "treatment", "wwtp", "wrf"],
    STATION: ["lift station", "pump station", "lift sta", "pump sta", " ps"],
    # A drinking-water asset is not a sewer terminus. Grace's compiled layer
    # includes a water tank; without this it reads as a lift station 7789 ft
    # from any force main, i.e. a fake data gap.
    IGNORED: ["water tank", "water tower", "reservoir"],
}

FLAG_NO_FORCE_MAIN = "facility_no_force_main"
FLAG_DIRECTION_CONFLICT = "facility_direction_conflict"


def load_facilities(cfg: dict) -> gpd.GeoDataFrame:
    """
    Read the terminal-facility layer and classify each point's role.

    Accepts `.xlsx`/`.xls` (a `Name` column plus a `Coords` column holding
    "lat, lon" — the shape you get from pasting Google Maps coordinates into a
    spreadsheet), `.csv` with the same columns, or any geospatial format
    GeoPandas reads. Returns a GeoDataFrame in the working CRS with columns
    `name`, `role`, `geometry`.

    Missing/unset path returns an empty frame — the facility layer is optional,
    and everything downstream degrades to the inferred rule without it.

    Role is assigned by name pattern, longest match wins, so
    "North the city Wastewater Division" is a plant even though a shorter station
    pattern might also appear in some other name. An unmatched name is a
    station: a facility a human bothered to record, whose name we don't
    recognise, is far more likely to be a pump station than anything else, and
    the station tolerance is the conservative one.
    """
    path = cfg["inputs"].get("terminal_facilities")
    crs = cfg["parameters"]["crs"]
    if not path or not Path(path).exists():
        return gpd.GeoDataFrame({"name": [], "role": []},
                                geometry=[], crs=crs)

    patterns = {**DEFAULT_PATTERNS,
                **(cfg["parameters"].get("terminal_facility_patterns") or {})}
    suffix = Path(path).suffix.lower()

    if suffix in {".xlsx", ".xls", ".csv"}:
        raw = (pd.read_excel(path) if suffix != ".csv"
               else pd.read_csv(path, encoding="utf-8-sig"))
        gdf = _from_name_coords(raw, path).to_crs(crs)
    else:
        gdf = gpd.read_file(path).to_crs(crs)
        if "name" not in gdf.columns:
            name_col = next((c for c in gdf.columns if c.lower() == "name"), None)
            gdf["name"] = gdf[name_col] if name_col else ""

    gdf["role"] = [_role(n, patterns) for n in gdf["name"].fillna("")]
    return gdf[["name", "role", "geometry"]].reset_index(drop=True)


def _from_name_coords(raw: pd.DataFrame, path) -> gpd.GeoDataFrame:
    """
    Build points from a `Name` + `Coords` sheet, where Coords is "lat, lon".

    Latitude first, because that is the order Google Maps shows and therefore
    the order a human pasting from it will produce. Getting this backwards puts
    every facility in the Indian Ocean, so the parsed values are range-checked
    rather than trusted: a "latitude" outside ±90 is a transposed pair, and
    saying so beats silently producing an empty match.
    """
    cols = {c.lower(): c for c in raw.columns}
    if "name" not in cols:
        raise ValueError(
            f"{path}: expected a 'Name' column, got {list(raw.columns)}")

    # Two shapes, because both are things a person actually produces: a single
    # `Coords` cell holding "lat, lon" (pasted straight from Google Maps), or
    # separate `lat`/`lon` columns (what you get after splitting them in Excel).
    if "lat" in cols and ("lon" in cols or "long" in cols):
        lon_col = cols.get("lon") or cols["long"]
        lat = pd.to_numeric(raw[cols["lat"]], errors="coerce")
        lon = pd.to_numeric(raw[lon_col], errors="coerce")
        source = "lat/lon columns"
    elif "coords" in cols:
        parsed = (raw[cols["coords"]].astype(str)
                  .str.extract(r"^\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*$"))
        lat = pd.to_numeric(parsed[0], errors="coerce")
        lon = pd.to_numeric(parsed[1], errors="coerce")
        source = "Coords column"
    else:
        raise ValueError(
            f"{path}: need either a 'Coords' column holding 'lat, lon', or "
            f"separate 'lat' and 'lon' columns. Got {list(raw.columns)}")

    bad = lat.isna() | lon.isna()
    if bad.any():
        raise ValueError(
            f"{path}: {int(bad.sum())} row(s) have unparseable coordinates in the "
            f"{source}. First offending row: "
            f"{raw.loc[bad].iloc[0].to_dict()!r}")
    if (lat.abs() > 90).any():
        raise ValueError(
            f"{path}: latitude outside +/-90 — Coords looks like 'lon, lat'. "
            f"Offending value: {lat[lat.abs() > 90].iloc[0]}")

    # The +/-90 check only catches a transposition when the longitude happens to
    # exceed 90. the city's is -78.9, so it would sail through and put every
    # facility off the coast of Africa, matching nothing — which reads
    # identically to "no facilities near the network."
    #
    # Heuristic, and its assumption stated: across the continental US longitude
    # magnitude (66-125) always exceeds latitude magnitude (24-49). Requiring
    # this to hold for EVERY row before complaining makes a false alarm very
    # unlikely. It does NOT hold everywhere — northern Europe has |lat| > |lon|
    # legitimately — so the message says what to do rather than just refusing.
    if len(lat) and (lat.abs() > lon.abs()).all():
        raise ValueError(
            f"{path}: every row has |latitude| > |longitude|, so Coords looks "
            f"like 'lon, lat' rather than 'lat, lon' (first row: {lat.iloc[0]}, "
            f"{lon.iloc[0]}). This check assumes continental-US coordinates; if "
            "your study area genuinely has |lat| > |lon|, swap the columns in "
            "the sheet or supply the layer as a geospatial file instead.")

    return gpd.GeoDataFrame({"name": raw[cols["name"]].astype(str)},
                            geometry=gpd.points_from_xy(lon, lat),
                            crs="EPSG:4326")


def _role(name: str, patterns: dict) -> str:
    """Longest matching pattern wins, so a specific name beats a generic one."""
    low = str(name).lower()
    best, best_len = STATION, 0
    for role, pats in patterns.items():
        for p in pats:
            if p.lower() in low and len(p) > best_len:
                best, best_len = role, len(p)
    return best


def match_facilities(facilities: gpd.GeoDataFrame, termini: pd.DataFrame,
                     plant_tol_ft: float, station_tol_ft: float) -> pd.DataFrame:
    """
    Match each facility to its nearest force-main terminus.

    Returns one row per non-ignored facility: name, role, matched terminus
    (comp_id, cluster), distance, the terminus classification the inference
    produced, and `status`:

      matched              within the role's tolerance
      no_force_main        nothing within tolerance — either the force main is
                           missing from the layer or the point is misplaced.
                           A pump station with no force main is a contradiction
                           in terms, so this is a real data-quality finding.

    Facilities are matched to termini, not termini to facilities, because a
    facility is the thing a human verified. An unmatched facility is a question
    about the *network*; an unmatched terminus is just a terminus.
    """
    cols = ["name", "role", "comp_id", "cluster", "dist_ft",
            "inferred_class", "status"]
    live = facilities[facilities.role != IGNORED]
    if live.empty or termini.empty:
        return pd.DataFrame(columns=cols)

    tree = cKDTree(np.c_[termini.x.to_numpy(), termini.y.to_numpy()])
    dist, j = tree.query(np.c_[live.geometry.x, live.geometry.y])

    rows = []
    for (_, fac), d, jj in zip(live.iterrows(), dist, j):
        t = termini.iloc[int(jj)]
        tol = plant_tol_ft if fac.role == PLANT else station_tol_ft
        rows.append({
            "name": fac["name"],
            "role": fac["role"],
            "comp_id": int(t.comp_id),
            "cluster": int(t.cluster),
            "dist_ft": round(float(d), 1),
            "inferred_class": t.classification,
            "status": "matched" if d <= tol else "no_force_main",
        })
    return pd.DataFrame(rows, columns=cols)


def apply_to_termini(termini: pd.DataFrame, matches: pd.DataFrame) -> pd.DataFrame:
    """
    Stamp facility knowledge onto the terminus table.

    Adds `facility_name`, `facility_role`, and rewrites `classification` for
    matched termini:

      plant   -> "terminal"   the network legitimately ends here
      station -> "wetwell"    confirmed, replacing an inferred "wetwell?"

    A station matched to a terminus the inference called a *discharge* is NOT
    flipped. Both readings cannot be right, and a silent override would destroy
    the only signal that something is wrong — `facility_conflict` marks it for
    review instead. Measured rate on the city: 1 of 26.
    """
    out = termini.copy()
    out["facility_name"] = ""
    out["facility_role"] = ""
    out["facility_conflict"] = False
    if matches.empty:
        return out

    by_cluster = {int(m.cluster): m for m in
                  matches[matches.status == "matched"].itertuples(index=False)}
    for i, row in out.iterrows():
        m = by_cluster.get(int(row.cluster))
        if m is None:
            continue
        out.at[i, "facility_name"] = m.name
        out.at[i, "facility_role"] = m.role
        if m.role == PLANT:
            out.at[i, "classification"] = "terminal"
            out.at[i, "contact"] = "facility"
        elif str(row.classification).startswith("discharge"):
            out.at[i, "facility_conflict"] = True
        else:
            out.at[i, "classification"] = "wetwell"
            out.at[i, "contact"] = "facility"
    return out


def facility_flags(matches: pd.DataFrame, termini: pd.DataFrame) -> list[dict]:
    """
    QC findings the facility layer exposes, as flag dicts for the review file.

    `facility_no_force_main` — a pump station or plant with no force-main
    terminus within tolerance. Either the force main is missing from the layer,
    or the facility point is wrong. Both are worth knowing and neither is
    visible without the facility layer.

    `facility_direction_conflict` — a confirmed pump station sitting on a
    terminus the out-degree rule called a discharge. One of the two is wrong.
    """
    flags = []
    for m in matches[matches.status == "no_force_main"].itertuples(index=False):
        flags.append({
            "flag_type": FLAG_NO_FORCE_MAIN,
            "name": m.name,
            "role": m.role,
            "dist_ft": m.dist_ft,
            "description": (
                f"{m.role} '{m.name}' has no force-main terminus within "
                f"tolerance (nearest is {m.dist_ft:.0f} ft). A pump station with "
                "no force main is a contradiction — check for a missing "
                "pressurized main or a misplaced facility point."),
        })
    if "facility_conflict" in termini.columns:
        for t in termini[termini.facility_conflict].itertuples(index=False):
            flags.append({
                "flag_type": FLAG_DIRECTION_CONFLICT,
                "name": t.facility_name,
                "role": STATION,
                "dist_ft": t.dist_ft,
                "description": (
                    f"pump station '{t.facility_name}' sits on a terminus the "
                    f"out-degree rule called '{t.classification}'. A station is a "
                    "wet well, so either the gravity direction is wrong here or "
                    "the facility point belongs to a different structure."),
            })
    return flags
