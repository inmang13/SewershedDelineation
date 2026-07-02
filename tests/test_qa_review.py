"""
Smoke test for the QA review feedback loop (src/qa_review.py + snap pass 3).

Written from the stated intent (2026-07-02): a reviewer records decisions in a
CSV; on the next run a "snap" decision connects the flagged gap in the graph,
"resolved" flags stop counting as open, "keep" flags stay open with the comment
attached, typos fail loudly, and decisions still match flags whose ids shifted.

Run:  python tests/test_qa_review.py     (from the project root)
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import geopandas as gpd
from shapely.geometry import LineString, Point

from graph_builder import build_graph
from qa_review import (DEFAULT_SNAP_RADIUS_FT, apply_review,
                       load_review_decisions, manual_snaps)

CRS = "EPSG:2264"


def _pipes(*lines):
    return gpd.GeoDataFrame(
        {"FACILITYID": [f"P{i}" for i in range(len(lines))],
         "SLOPE": [1.0] * len(lines),
         "UPSTREAMIN": [10.0] * len(lines),
         "DOWNSTREAM": [5.0] * len(lines)},
        geometry=[LineString(l) for l in lines], crs=CRS)


def _write_csv(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                    encoding="utf-8", newline="")
    f.write(text)
    f.close()
    return f.name


HEADER = "flag_type,pipe_id,x,y,decision,radius_ft,comment\n"


def test_snap_connects_end_to_junction_gap():
    # P0 flows into P1 at a junction (100, 0); P2's end dangles 3 ft short of
    # it — the exact end-beside-junction case the automatic repair skips.
    pipes = _pipes([(0, 0), (100, 0)], [(100, 0), (200, 0)],
                   [(100, 200), (100, 3)])
    before = build_graph(pipes, snap_tol_ft=1.0, snap_gap_search_radius_ft=10.0)
    assert before.number_of_nodes() == 5, before.number_of_nodes()

    # Reviewer records YES SNAP at the gap midpoint (100, 1.5).
    snaps = manual_snaps(load_review_decisions(_write_csv(
        HEADER + "snap_gap,P0 / P1 / P2,100,1.5,snap,,YES SNAP\n")))
    after = build_graph(pipes, 1.0, 10.0, manual_snaps=snaps)
    assert after.number_of_nodes() == 4, after.number_of_nodes()
    assert after.number_of_edges() == 3          # no pipe lost or duplicated

    # The merged node must carry all three pipes: P2 now drains into the run.
    merged = [n for n, d in after.nodes(data=True)
              if abs(d["x"] - 100) < 5 and abs(d["y"]) < 5]
    assert len(merged) == 1
    fids = {d["facilityid"]
            for *_, d in after.in_edges(merged[0], keys=True, data=True)} | \
           {d["facilityid"]
            for *_, d in after.out_edges(merged[0], keys=True, data=True)}
    assert fids == {"P0", "P1", "P2"}, fids


def test_snap_that_reaches_nothing_fails_loudly():
    pipes = _pipes([(0, 0), (100, 0)])
    snaps = [{"x": 9999.0, "y": 9999.0, "radius_ft": DEFAULT_SNAP_RADIUS_FT}]
    try:
        build_graph(pipes, 1.0, 10.0, manual_snaps=snaps)
    except ValueError:
        pass
    else:
        raise AssertionError("a snap that merges nothing must raise")


def test_decisions_annotate_flags():
    path = _write_csv(
        HEADER
        + "snap_gap,A / B,10,10,resolved,,checked in GIS - laterals\n"
        + "snap_gap,C / D,20,20,keep,,not sure yet\n"
        # id drifted (component renumbering) — must still match by location
        + "disconnected_component,component_7,500,500,resolved,,ends at PS\n")
    decisions = load_review_decisions(path)
    flags = [
        {"flag_type": "snap_gap", "pipe_id": "A / B",
         "severity": "review_required", "geometry": Point(10, 10)},
        {"flag_type": "snap_gap", "pipe_id": "C / D",
         "severity": "review_required", "geometry": Point(20, 20)},
        {"flag_type": "disconnected_component", "pipe_id": "component_9",
         "severity": "warning", "geometry": Point(510, 500)},
        {"flag_type": "snap_gap", "pipe_id": "E / F",
         "severity": "review_required", "geometry": Point(30, 30)},
    ]
    apply_review(flags, decisions, match_radius_ft=50.0)

    assert flags[0]["review_status"] == "resolved"
    assert flags[1]["review_status"] == "open"
    assert flags[1]["review_comment"] == "not sure yet"
    assert flags[2]["review_status"] == "resolved"      # proximity fallback
    assert flags[3]["review_status"] == ""              # never reviewed
    n_open = sum(1 for f in flags if f["severity"] == "review_required"
                 and f["review_status"] != "resolved")
    assert n_open == 2   # the kept flag + the unreviewed one


def test_loader_rejects_typos_and_bad_coords():
    for bad_row, why in [
        ("snap_gap,A,10,10,reslved,,typo\n", "unknown decision"),
        ("snap_gap,A,,10,resolved,,blank x\n", "blank coordinate"),
    ]:
        try:
            load_review_decisions(_write_csv(HEADER + bad_row))
        except ValueError:
            pass
        else:
            raise AssertionError(f"loader must reject: {why}")
    # Blank decision = comment-only row: allowed, skipped.
    assert load_review_decisions(_write_csv(
        HEADER + "snap_gap,A,10,10,,,just a note\n")) == []


def test_missing_file_is_not_an_error():
    assert load_review_decisions(None) == []
    assert load_review_decisions("nope/does_not_exist.csv") == []


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS  {t.__name__}")
    print(f"\n{len(tests)} tests passed.")
