"""
Terminal-facility tests — from the stated intent:

"A component that ends at a treatment plant isn't broken, it's finished. And a
lift station tells you directly which end is the wet well, instead of guessing
from the gravity direction. But if the station and the guess disagree, tell me —
don't just overwrite it."

Toy geometry only — no data/ or config.yaml. Runs on a clean clone.

Run:  python -m pytest tests/test_terminal_facilities.py
"""

import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from terminal_facilities import (                                # noqa: E402
    load_facilities, match_facilities, apply_to_termini, facility_flags,
    PLANT, STATION, IGNORED, FLAG_NO_FORCE_MAIN, FLAG_DIRECTION_CONFLICT,
)

CRS = "EPSG:2264"


def _cfg(path=None):
    return {"inputs": {"terminal_facilities": str(path) if path else None},
            "parameters": {"crs": CRS}}


def _sheet(tmp_path, rows, name="fac.xlsx"):
    """Name + 'lat, lon' Coords — the shape you get pasting from Google Maps."""
    path = tmp_path / name
    pd.DataFrame({"Name": [r[0] for r in rows],
                  "Coords": [f"{r[1]}, {r[2]}" for r in rows]}).to_excel(
        path, index=False)
    return path


def _termini(rows):
    """Minimal terminus table: (comp_id, cluster, x, y, classification, contact)."""
    return pd.DataFrame(
        [{"comp_id": r[0], "cluster": r[1], "x": r[2], "y": r[3],
          "classification": r[4], "contact": r[5], "dist_ft": 0.0} for r in rows])


# ---------------------------------------------------------------------------
# Loading and role assignment
# ---------------------------------------------------------------------------

def test_roles_are_assigned_from_the_facility_name(tmp_path):
    path = _sheet(tmp_path, [
        ("South the city Water Reclamation Facility", 35.904, -78.978),
        ("Cedar Creek Lift Station", 36.042, -78.985),
        ("East the city Water Tank", 35.983, -78.888),
    ])
    f = load_facilities(_cfg(path))
    assert dict(zip(f.name, f.role)) == {
        "South the city Water Reclamation Facility": PLANT,
        "Cedar Creek Lift Station": STATION,
        "East the city Water Tank": IGNORED,
    }


def test_a_water_asset_is_not_a_sewer_terminus(tmp_path):
    """
    Grace's compiled layer includes a water tank. Left in, it reads as a lift
    station 7789 ft from any force main — a fabricated data gap.
    """
    path = _sheet(tmp_path, [("East the city Water Tank", 35.983, -78.888)])
    f = load_facilities(_cfg(path))
    t = _termini([(0, 1, 0.0, 0.0, "wetwell?", "candidate")])
    assert match_facilities(f, t, 1000.0, 200.0).empty


def test_transposed_coordinates_fail_loudly(tmp_path):
    """
    'lon, lat' instead of 'lat, lon' puts every facility in the Indian Ocean and
    matches nothing. Silence here would look like "no facilities nearby".
    """
    path = _sheet(tmp_path, [("Some Lift Station", -78.888, 35.983)])
    with pytest.raises(ValueError, match="looks like 'lon, lat'"):
        load_facilities(_cfg(path))


def test_unparseable_coords_name_the_offending_value(tmp_path):
    path = tmp_path / "bad.xlsx"
    pd.DataFrame({"Name": ["A"], "Coords": ["36.0 / -78.9"]}).to_excel(path, index=False)
    with pytest.raises(ValueError, match="unparseable coordinates"):
        load_facilities(_cfg(path))


def test_separate_lat_and_lon_columns_are_accepted(tmp_path):
    """
    Both shapes are things a person actually produces: one 'lat, lon' cell
    pasted from Google Maps, or two columns after splitting it in Excel.
    """
    path = tmp_path / "split.xlsx"
    pd.DataFrame({"Name": ["Cedar Creek Lift Station"],
                  "lat": [36.043048], "lon": [-78.987224]}).to_excel(path, index=False)
    f = load_facilities(_cfg(path))
    assert len(f) == 1 and f.role.iloc[0] == STATION


def test_a_sheet_with_no_usable_coordinate_columns_says_so(tmp_path):
    path = tmp_path / "nocoords.xlsx"
    pd.DataFrame({"Name": ["A"], "Notes": ["somewhere"]}).to_excel(path, index=False)
    with pytest.raises(ValueError, match="need either a 'Coords' column"):
        load_facilities(_cfg(path))


def test_no_facility_layer_is_not_an_error():
    assert load_facilities(_cfg()).empty
    assert load_facilities(_cfg("nope.xlsx")).empty


# ---------------------------------------------------------------------------
# Matching and its effect
# ---------------------------------------------------------------------------

def _fac(rows):
    return gpd.GeoDataFrame(
        {"name": [r[0] for r in rows], "role": [r[1] for r in rows]},
        geometry=[Point(r[2], r[3]) for r in rows], crs=CRS)


def test_a_plant_makes_termination_a_result_not_a_defect():
    f = _fac([("Big WRF", PLANT, 0.0, 800.0)])       # 800 ft from the terminus
    t = _termini([(0, 1, 0.0, 0.0, "wetwell?", "candidate")])
    m = match_facilities(f, t, plant_tol_ft=1000.0, station_tol_ft=200.0)
    assert m.status.iloc[0] == "matched"
    out = apply_to_termini(t, m)
    assert out.classification.iloc[0] == "terminal"
    assert out.contact.iloc[0] == "facility"


def test_plant_and_station_get_different_tolerances():
    """
    A plant is a campus (South the city WRF measured 842 ft from its terminus); a
    station is a small structure (median 34 ft). One tolerance can't serve both.
    """
    at_800 = [("X", 0.0, 800.0)]
    t = _termini([(0, 1, 0.0, 0.0, "wetwell?", "candidate")])
    plant = match_facilities(_fac([(n, PLANT, x, y) for n, x, y in at_800]),
                             t, 1000.0, 200.0)
    station = match_facilities(_fac([(n, STATION, x, y) for n, x, y in at_800]),
                               t, 1000.0, 200.0)
    assert plant.status.iloc[0] == "matched"
    assert station.status.iloc[0] == "no_force_main"


def test_a_lift_station_confirms_the_inferred_wet_well():
    f = _fac([("Pumpy", STATION, 0.0, 30.0)])
    t = _termini([(0, 1, 0.0, 0.0, "wetwell?", "candidate")])
    out = apply_to_termini(t, match_facilities(f, t, 1000.0, 200.0))
    assert out.classification.iloc[0] == "wetwell"
    assert out.facility_name.iloc[0] == "Pumpy"
    assert not out.facility_conflict.iloc[0]


def test_a_disagreement_is_reported_not_silently_overwritten():
    """
    A station on a terminus the out-degree rule called a discharge means one of
    the two is wrong. Flipping it would destroy the only signal that says so.
    """
    f = _fac([("Pumpy", STATION, 0.0, 30.0)])
    t = _termini([(0, 1, 0.0, 0.0, "discharge", "connected")])
    out = apply_to_termini(t, match_facilities(f, t, 1000.0, 200.0))
    assert out.classification.iloc[0] == "discharge"      # unchanged
    assert out.facility_conflict.iloc[0]
    assert facility_flags(pd.DataFrame(columns=["status"]), out)[0]["flag_type"] \
        == FLAG_DIRECTION_CONFLICT


def test_a_pump_station_with_no_force_main_is_a_finding():
    """A pump station with no force main is a contradiction in terms."""
    f = _fac([("Lonely LS", STATION, 0.0, 5000.0)])
    t = _termini([(0, 1, 0.0, 0.0, "wetwell?", "candidate")])
    m = match_facilities(f, t, 1000.0, 200.0)
    assert m.status.iloc[0] == "no_force_main"
    flags = facility_flags(m, apply_to_termini(t, m))
    assert flags[0]["flag_type"] == FLAG_NO_FORCE_MAIN
    assert "Lonely LS" in flags[0]["description"]


def test_facility_contact_counts_toward_a_component_verdict():
    """
    Regression (2026-08-02): a confirmed wet well got contact="facility", which
    the verdict roll-up did not count as attached, turning confirmed wet wells
    into `no_wetwell`. A confirmed facility is stronger evidence than a snap,
    not weaker.
    """
    from force_mains import CONFIRMED_CONTACTS
    f = _fac([("Pumpy", STATION, 0.0, 30.0)])
    t = _termini([(0, 1, 0.0, 0.0, "wetwell?", "candidate")])
    out = apply_to_termini(t, match_facilities(f, t, 1000.0, 200.0))
    assert out.contact.iloc[0] in CONFIRMED_CONTACTS
