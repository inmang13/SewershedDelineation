"""
run.py (production spine) unit tests — from intent, guarding the two pieces that
were unverified or buggy when run.py landed:

  1. Site-list parsing must NOT corrupt zero-padded FACILITYIDs. IDs like
     "02201" are strings; pandas' default type inference reads them as numbers
     (2201.0), silently mis-identifying the site. This is a regression test for
     the exact bug the 24-site equivalence gate caught (decision_log 2026-07-12).
  2. Multi-site QC-flag writing must not leave a previous run's flags behind
     (roadmap P2-6): `_clear_delineation_layers` drops stale `delin_*` layers but
     must preserve the network-QA `net_*` layers written by run_qa. It has to be
     correct against BOTH naive versions — wiping everything, or clearing nothing.

Toy inputs only — no data/ or config.yaml. Runs on a clean clone.

Run:  python -m pytest tests/test_run_pipeline.py
"""

import sys
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))          # import run (project root)
sys.path.insert(0, str(ROOT / "src"))  # run.py's own imports resolve

import run                              # noqa: E402
from qc_output import write_qc_flags_gpkg   # noqa: E402

CRS = "EPSG:2264"


# --- site-list parsing ----------------------------------------------------

def test_sites_from_csv_preserves_zero_padded_ids(tmp_path):
    csv = tmp_path / "sites.csv"
    csv.write_text("FACILITYID\n02201\n09289\n", encoding="utf-8")
    sites = run._sites_from_csv(csv)
    ids = [s["target"][1] for s in sites]
    # Intent: the id survives as the exact zero-padded string. RED without
    # dtype=str (pandas would give 2201 / "2201.0").
    assert ids == ["02201", "09289"]
    assert all(s["target"][0] == "manhole_id" for s in sites)
    assert sites[0]["label"] == "02201"


def test_sites_from_csv_coordinate_mode(tmp_path):
    csv = tmp_path / "sites.csv"
    csv.write_text("SiteID,x,y\nS1,1000.5,2000.25\n", encoding="utf-8")
    sites = run._sites_from_csv(csv)
    assert len(sites) == 1
    kind, value = sites[0]["target"]
    # Intent: x/y columns => coordinate target, as floats.
    assert kind == "coordinate"
    assert value == [1000.5, 2000.25]
    assert sites[0]["label"] == "S1"


def test_sites_from_csv_id_mode_without_coords(tmp_path):
    csv = tmp_path / "sites.csv"
    csv.write_text("FACILITYID,tract\n17506,18.02\n", encoding="utf-8")
    sites = run._sites_from_csv(csv)
    assert sites[0]["target"] == ("manhole_id", "17506")
    assert sites[0]["tract"] == "18.02"


# --- P2-6: stale-layer clearing without wiping QA layers ------------------

def _seed_gpkg(path):
    """A gpkg as it looks after run_qa (net_ layer) + a prior delineation
    (a stale delin_ layer)."""
    gpd.GeoDataFrame({"feat_id": ["P1"], "descr": ["net flag"]},
                     geometry=[Point(0, 0)], crs=CRS).to_file(
        path, layer="net_invert_conflict_point", driver="GPKG")
    gpd.GeoDataFrame({"feat_id": ["OLD"], "descr": ["stale"]},
                     geometry=[Point(1, 1)], crs=CRS).to_file(
        path, layer="delin_large_catchment_point", driver="GPKG")


def _layers(path):
    import pyogrio
    return sorted(str(r[0]) for r in pyogrio.list_layers(str(path)))


def test_clear_delineation_layers_preserves_net_drops_stale(tmp_path):
    gpkg = tmp_path / "qc.gpkg"
    _seed_gpkg(gpkg)

    run._clear_delineation_layers(gpkg)

    layers = _layers(gpkg)
    # Intent: QA network layers survive; the stale delineation layer is gone.
    # RED against replace=True (wipes net_) AND against not clearing (stale stays).
    assert "net_invert_conflict_point" in layers
    assert not any(l.startswith("delin_") for l in layers)
    # The preserved layer's data must survive the unlink+rewrite, not just its name.
    kept = gpd.read_file(gpkg, layer="net_invert_conflict_point")
    assert kept["feat_id"].iloc[0] == "P1"

    # A fresh delineation write then appends without disturbing net_.
    fresh = [{"flag_type": "competing_pipe", "severity": "warning",
              "manhole": "17506", "parcel_id": "9", "description": "fresh",
              "geometry": Point(2, 2)}]
    write_qc_flags_gpkg(fresh, gpkg, CRS, replace=False)
    final = _layers(gpkg)
    assert "net_invert_conflict_point" in final
    assert any("competing_pipe" in l for l in final)
    assert not any("large_catchment" in l for l in final)


def test_clear_delineation_layers_noop_on_missing_file(tmp_path):
    # Intent: first-ever run (no gpkg yet) must not raise.
    run._clear_delineation_layers(tmp_path / "does_not_exist.gpkg")
