"""
Portfolio demo — runs entirely on the synthetic toy network (examples/toy/),
so it works for anyone who clones the repo with no municipal data of their own.

Force-main wiring is on by default (wire_force_mains=True) — no toggle, since
the toy network doesn't ship a pumped basin to demonstrate it on. See the
README's Validation section for the real-network force-main results; a lift
station in the toy example is on the roadmap (examples/toy/README.md).

Run:  streamlit run demo_app.py
"""

import sys
from pathlib import Path

import geopandas as gpd
import pydeck as pdk
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                                   # noqa: E402
from graph_builder import load_graph_from_config, build_node_index, small_dangling_pidx  # noqa: E402
from population_join import load_population_units, _explode_to_parts  # noqa: E402
from run import delineate_site, _acres                           # noqa: E402

CONFIG_PATH = str(Path(__file__).parent / "examples" / "toy" / "config.yaml")

st.set_page_config(page_title="Sewershed delineation — demo", page_icon=":material/water_drop:",
                   layout="wide")


@st.cache_resource(show_spinner="Loading the synthetic toy network...")
def load_pipeline():
    cfg = load_config(CONFIG_PATH)
    G, pipes = load_graph_from_config(cfg, wire_force_mains=True)
    index = build_node_index(G)
    parcels_raw = load_population_units(cfg)
    parcels = (_explode_to_parts(parcels_raw)
               if cfg["parameters"].get("explode_multipart_units", True)
               else parcels_raw)
    ignore_pidx = small_dangling_pidx(
        G, cfg["parameters"].get("competing_ignore_dangling_max_pipes", 3))
    return cfg, G, pipes, index, parcels, ignore_pidx


st.title("Sewershed delineation — live demo")
st.caption(
    "Runs on a **synthetic toy network** (13 manholes, 12 pipes, 484 parcels) — "
    "no real infrastructure data, so this works for anyone. "
    "[Full README](https://github.com/inmang13/SewershedDelineation) · "
    "validated median IoU **0.875** against 25 expert-delineated real catchments."
)

cfg, G, pipes, index, parcels, ignore_pidx = load_pipeline()

with st.form("site"):
    manhole_id = st.selectbox(
        "Manhole ID", ["MH01", "MH02", "MH03", "MH04", "MH05", "MH08", "MH11"],
        index=0,
        help="MH01 = the outlet (whole 12-pipe network). MH08/MH11 = headwaters "
             "(nothing upstream — a valid, empty result). Try a few to see how "
             "the traced area changes.")
    submitted = st.form_submit_button("Delineate", type="primary")

if submitted:
    r = delineate_site(G, pipes, index, parcels, cfg, ignore_pidx,
                       ("manhole_id", manhole_id))
    if r["status"] != "ok" or r["geom"] is None or r["geom"].is_empty:
        st.warning(f"**{manhole_id}** — {r['status']} (a headwater has nothing "
                   "upstream; that's a valid result, not an error).")
    else:
        geom = r["geom"]
        gdf = gpd.GeoDataFrame({"SiteID": [manhole_id]}, geometry=[geom],
                               crs=cfg["parameters"]["crs"])
        trace = pipes.iloc[sorted(set(r["res"].pidx_list))]

        left, right = st.columns([3, 1])
        with left:
            wgs = gdf.to_crs("EPSG:4326")
            b = wgs.total_bounds
            boundary_layer = pdk.Layer(
                "GeoJsonLayer", data=wgs.__geo_interface__,
                get_fill_color=[30, 120, 200, 60],
                get_line_color=[20, 80, 160, 255], line_width_min_pixels=2)
            trace_layer = pdk.Layer(
                "GeoJsonLayer", data=trace.to_crs("EPSG:4326").__geo_interface__,
                get_line_color=[60, 60, 60, 200], line_width_min_pixels=2)
            st.pydeck_chart(pdk.Deck(
                layers=[boundary_layer, trace_layer],
                initial_view_state=pdk.ViewState(
                    latitude=(b[1] + b[3]) / 2, longitude=(b[0] + b[2]) / 2, zoom=14),
                map_style="light"))
            st.caption("Blue = delineated catchment · gray = traced pipes "
                       "(synthetic network — not a real place)")
        with right:
            st.metric("Traced pipes", r["res"].n_edges)
            st.metric("Served parcels",
                      len(r["pop"].served) if r["pop"] is not None else 0)
            st.metric("Area (acres)", round(_acres(geom), 1))
