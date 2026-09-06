"""
Functional tests for demo_app.py via streamlit.testing.v1.AppTest.

Runs entirely on the committed toy network (examples/toy/) — no skip
condition needed, unlike RDII's dashboard tests, since the data this app
needs ships with the repo.

Pins:
  - the app loads and renders without exception;
  - MH01 (the outlet) delineates successfully with the exact numbers
    examples/toy/README.md documents (14 pipes traced, 66 served parcels,
    76.7 acres) — a silent regression in the boundary pipeline would show
    up here as a changed number, not just a crash;
  - MH01's real-Census demographics panel renders a nonzero population;
  - a headwater (MH08) reports its status as a warning, not a crash or a
    silently-empty success.
"""

from pathlib import Path

import pytest

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest

APP = Path(__file__).parent.parent / "demo_app.py"


def _run():
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    return at


def test_app_loads_without_exception():
    at = _run()
    assert not at.exception


def test_mh01_delineates_with_documented_numbers():
    at = _run()
    at.selectbox(key="manhole_select").set_value("MH01").run()
    at.button[0].click().run()
    assert not at.exception

    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Traced pipes"] == "14"
    assert metrics["Served parcels"] == "66"
    assert metrics["Area (acres)"] == "76.7"
    assert metrics["Est. population"] == "488"


def test_headwater_reports_a_warning_not_a_crash():
    at = _run()
    at.selectbox(key="manhole_select").set_value("MH08").run()
    at.button[0].click().run()
    assert not at.exception
    assert len(at.warning) == 1
    assert "MH08" in at.warning[0].value
