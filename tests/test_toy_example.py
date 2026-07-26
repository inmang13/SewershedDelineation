"""
Toy example (roadmap Phase 9, Track B) — end-to-end smoke tests.

Every other test in this suite builds its geometry inline. This one runs the
real `run.py` against the committed `examples/toy/` data on disk, so it is the
only test that exercises config loading, graph building, traversal, the
population join, the competing-pipe rules and boundary construction as one
pipeline. If the shipped one-command demo breaks, this goes red.

Intent being guarded:

  1. **The advertised demo command actually works.** README tells a stranger to
     run `python run.py --config examples/toy/config.yaml`; a broken toy is a
     broken first impression, and nothing else in CI touches it.
  2. **The traversal returns the hand-verified upstream sets.** The toy network
     is a 12-pipe tree whose answers were counted by hand from the node list in
     make_toy_data.py, so these are true oracles, not a snapshot of whatever the
     code happened to print.
  3. **Output paths resolve exactly once, against the config's own directory.**
     `load_config` resolves `outputs.*` relative to the config file; `run.py`
     used to prefix its base directory a second time, writing to
     `<base>/<base>/output/flags.csv`. That was invisible for every config at
     the repo root (base "."), which is why it survived to 2026-07-26 — only a
     config in a subdirectory exposes it. Roadmap P3-10.

Run:  python -m pytest tests/test_toy_example.py
"""

import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))          # import run (project root)
sys.path.insert(0, str(ROOT / "src"))  # run.py's own imports resolve

import run                                              # noqa: E402
from config import load_config                          # noqa: E402
from graph_builder import load_graph_from_config, build_node_index   # noqa: E402
from traversal import trace_manhole                     # noqa: E402

TOY_CONFIG = ROOT / "examples" / "toy" / "config.yaml"

# Hand-counted from the NODES/PIPES tables in examples/toy/make_toy_data.py.
# MH01 is the outlet (whole tree); MH03 and MH04 are junctions partway up;
# MH08 is a headwater with nothing above it.
UPSTREAM_PIPE_COUNTS = {"MH01": 12, "MH03": 10, "MH04": 6, "MH08": 0}


@pytest.fixture(scope="module")
def toy_graph():
    """Load the toy network once for all the traversal oracles."""
    cfg = load_config(str(TOY_CONFIG))
    G, pipes = load_graph_from_config(cfg)
    return cfg, G, pipes, build_node_index(G)


# --- the network itself ---------------------------------------------------

TOY_LAYERS = ("gravity_mains.gpkg", "manholes.gpkg", "parcels.gpkg")


def test_toy_data_is_tracked_by_git():
    """The demo is worthless if the data isn't in the repo — a stranger cloning
    the project must not have to run the generator first.

    This asks git, not the filesystem. `.gitignore` has a blanket `data/` rule
    with a negation re-including `examples/toy/data/`; if that negation broke,
    the files would still be sitting on a developer's disk and `Path.exists()`
    would stay green while a fresh clone came up empty. Only `git ls-files`
    distinguishes the two.
    """
    out = subprocess.run(["git", "ls-files", "examples/toy/data"],
                         cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:                    # not a git checkout (e.g. sdist)
        pytest.skip("not a git working tree")
    tracked = {Path(line).name for line in out.stdout.split()}
    missing = [n for n in TOY_LAYERS if n not in tracked]
    assert not missing, f"toy layers not tracked by git: {missing}"


def test_committed_data_matches_the_generator(tmp_path):
    """The committed GeoPackages must be what make_toy_data.py produces.

    Shipping both the data and its generator is only honest if they agree, and
    the GeoPackages are binary, so nobody can eyeball a diff. A byte hash will
    not do the job: GDAL stamps a write timestamp into gpkg_contents.last_change,
    so regenerating changes every hash while the features stay identical. Compare
    content instead — that is the property actually worth guarding.
    """
    sys.path.insert(0, str(TOY_CONFIG.parent))
    import make_toy_data

    make_toy_data.main(out_dir=tmp_path, quiet=True)
    for name in TOY_LAYERS:
        committed = gpd.read_file(TOY_CONFIG.parent / "data" / name)
        regenerated = gpd.read_file(tmp_path / name)
        assert len(committed) == len(regenerated), f"{name}: feature count differs"
        assert list(committed.columns) == list(regenerated.columns), f"{name}: schema differs"
        assert committed.crs == regenerated.crs, f"{name}: CRS differs"
        for col in committed.columns.drop("geometry"):
            assert committed[col].tolist() == regenerated[col].tolist(), \
                f"{name}: column {col} differs"
        assert committed.geometry.geom_equals_exact(
            regenerated.geometry, tolerance=1e-9).all(), f"{name}: geometry differs"


def test_toy_graph_has_one_edge_per_pipe(toy_graph):
    _, G, pipes, _ = toy_graph
    assert len(pipes) == 12
    assert G.number_of_edges() == 12      # no pipe dropped, none duplicated
    assert G.number_of_nodes() == 13      # every endpoint pair merged to one node


def test_toy_network_has_no_midspan_junctions(toy_graph):
    """Every toy pipe is a single grid step, so no manhole sits on another
    pipe's interior. If this fires, the generator has grown accidental
    collinear geometry and the splitter is silently reshaping the network."""
    from pipe_splits import apply_midspan_splits
    cfg, _, pipes, _ = toy_graph
    _, split_log = apply_midspan_splits(pipes, cfg)
    assert split_log.empty


# --- traversal oracles ----------------------------------------------------

@pytest.mark.parametrize("manhole,expected", sorted(UPSTREAM_PIPE_COUNTS.items()))
def test_upstream_trace_matches_hand_count(toy_graph, manhole, expected):
    cfg, G, _, index = toy_graph
    res = trace_manhole(G, cfg, index=index, target=("manhole_id", manhole))
    assert res.n_edges == expected


def test_headwater_traces_empty_rather_than_failing(toy_graph):
    # A headwater is a valid answer, not an error — Phase 6 reports it as
    # no_upstream_found. MH08 has nothing upstream of it.
    cfg, G, _, index = toy_graph
    res = trace_manhole(G, cfg, index=index, target=("manhole_id", "MH08"))
    assert res.is_empty
    assert res.n_edges == 0


# --- end to end through run.py -------------------------------------------

SANDBOX = "toyrun"      # subdirectory holding the copied config, relative to cwd


def _toy_config_in(tmp_path: Path, monkeypatch) -> str:
    """Stage the toy config in tmp_path and return a RELATIVE path to it.

    Inputs are rewritten absolute so they still find the committed toy data;
    outputs stay relative, so load_config resolves them against the config's own
    directory and the run writes into tmp_path instead of the repo.

    The relative path is load-bearing, not incidental. `Path(a) / b` discards `a`
    whenever `b` is absolute, so an absolute config path makes the double-prefix
    bug vanish on its own — the test would pass against the broken code and
    guard nothing. Only a config reached by a relative path (the way the README
    invokes it: `--config examples/toy/config.yaml`) reproduces it. Hence the
    chdir plus the `SANDBOX/config.yaml` return value.
    """
    cfg = yaml.safe_load(TOY_CONFIG.read_text())
    toy_dir = TOY_CONFIG.parent
    for key in ("gravity_main_shapefile", "manholes_shapefile",
                "population_units_shapefile"):
        cfg["inputs"][key] = str((toy_dir / cfg["inputs"][key]).resolve())
    sandbox = tmp_path / SANDBOX
    sandbox.mkdir()
    (sandbox / "config.yaml").write_text(yaml.safe_dump(cfg))
    monkeypatch.chdir(tmp_path)
    return f"{SANDBOX}/config.yaml"


def test_run_py_delineates_the_toy_outlet(tmp_path, monkeypatch):
    cfg_path = _toy_config_in(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["run.py", "--config", cfg_path])
    run.main()

    out = tmp_path / SANDBOX / "output" / "sewershed_final.gpkg"
    assert out.exists(), "run.py wrote no boundary for the toy outlet"
    gdf = gpd.read_file(out, layer="boundary")
    assert len(gdf) == 1
    assert gdf.iloc[0]["SiteID"] == "MH01"
    assert gdf.iloc[0]["n_pipes"] == 12
    assert gdf.geometry.iloc[0].is_valid and not gdf.geometry.iloc[0].is_empty


def test_outputs_resolve_once_against_the_config_directory(tmp_path, monkeypatch):
    """Regression for the double-prefix bug (roadmap P3-10).

    RED before the fix: flags.csv landed at toyrun/toyrun/output/flags.csv,
    because run.py prefixed its base directory onto a path load_config had
    already resolved. See _toy_config_in on why the config path must be
    relative for this to reproduce.
    """
    cfg_path = _toy_config_in(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["run.py", "--config", cfg_path])
    run.main()

    assert (tmp_path / SANDBOX / "output" / "flags.csv").exists()
    # No output may sit at a path that repeats the sandbox directory inside
    # itself — that is the double-prefix signature.
    doubled = [p for p in tmp_path.rglob("*")
               if list(p.relative_to(tmp_path).parts).count(SANDBOX) > 1]
    assert not doubled, f"outputs written to a double-prefixed path: {doubled}"


def test_boundary_follows_the_network_instead_of_blobbing(tmp_path, monkeypatch):
    """Guards the toy's geometry against the boundary step over-bridging.

    The served set is whole parcels, not the pipe buffer: each pipe runs along a
    parcel-grid boundary, so the 50 ft selection radius catches the parcel on
    each side and the served corridor is two parcels (400 ft) wide. Twelve
    1000 ft pipes therefore cover about 110 acres.

    If Delaunay bridged between branches instead of following them, the polygon
    would balloon toward the convex hull of the tree — the network spans
    4000 x 4000 ft, about 367 acres, so the failure mode is far outside the band
    below rather than a near miss. The toy's 1000 ft node spacing exists to keep
    branch gaps above delaunay_max_edge_ft (500 ft); this test fails if someone
    tightens the spacing or loosens the threshold.
    """
    cfg_path = _toy_config_in(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["run.py", "--config", cfg_path])
    run.main()

    gdf = gpd.read_file(tmp_path / SANDBOX / "output" / "sewershed_final.gpkg",
                        layer="boundary")
    parcel_ft = 200.0                       # make_toy_data.PARCEL_SIZE
    corridor_acres = (12 * 1000.0 * 2 * parcel_ft) / 43560.0     # ~110 acres
    area = gdf.iloc[0]["area_acres"]
    assert 0.8 * corridor_acres < area < 1.5 * corridor_acres, (
        f"toy boundary {area:.1f} acres is outside the expected corridor band "
        f"around {corridor_acres:.1f} acres")


def test_headwater_site_is_skipped_without_crashing(tmp_path, monkeypatch):
    # Mixed list: two delineable sites plus a headwater. The headwater must be
    # reported and skipped, not abort the run or emit an empty polygon.
    cfg_path = _toy_config_in(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv",
                        ["run.py", "--config", cfg_path,
                         "--sites", "MH01,MH04,MH08"])
    run.main()

    gdf = gpd.read_file(tmp_path / SANDBOX / "output" / "sewershed_final.gpkg",
                        layer="boundary")
    assert sorted(gdf["SiteID"]) == ["MH01", "MH04"]
    assert "MH08" not in set(gdf["SiteID"])
