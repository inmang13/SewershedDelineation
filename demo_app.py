"""
Portfolio demo — runs entirely on the toy network (examples/toy/), so it works
for anyone who clones the repo with no municipal data of their own.

The map underlay is a REAL street basemap (CARTO Voyager, no API key needed).
Unlike the old version of this demo, the underlying network geometry is ALSO
real: the pipes and manholes below follow actual street centerlines and
intersections in Trinity Park, Durham, NC (see make_toy_data.py for the real
OpenStreetMap-sourced coordinates). The SEWER NETWORK ITSELF IS STILL
INVENTED — no real sewer infrastructure is used or implied, only real street
alignment.

The socioeconomic panel is REAL Census data (2020 decennial + ACS 5-year, see
fetch_real_census.py) for that same real Trinity Park area, apportioned onto
the traced boundary through the exact production dasymetric join
(src/demographics.py) — the same code path used on real municipal data. Only
"Median parcel value" stays synthetic (real assessor parcel values are not
redistributable; see make_toy_data.py's parcel fabric).

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
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).parent / "src"))
from config import load_config                                   # noqa: E402
from graph_builder import load_graph_from_config, build_node_index, small_dangling_pidx  # noqa: E402
from population_join import load_population_units, _explode_to_parts  # noqa: E402
from run import delineate_site, _acres                           # noqa: E402
import demographics as demo                                      # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from run_demographics import (                                   # noqa: E402
    build_spec, load_blocks_with_data, load_block_groups_with_data,
    served_parcels_for,
)

CONFIG_PATH = str(Path(__file__).parent / "examples" / "toy" / "config.yaml")

st.set_page_config(page_title="Sewershed delineation — demo", page_icon=":material/water_drop:",
                   layout="wide")


@st.cache_resource(show_spinner="Loading the toy network (real Trinity Park streets)...")
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


@st.cache_resource(show_spinner="Loading real Census data (Trinity Park, Durham NC)...")
def load_demographics_inputs(_cfg):
    blocks = load_blocks_with_data(_cfg)
    bgs = load_block_groups_with_data(_cfg, blocks)
    parcels_raw = load_population_units(_cfg)
    res = demo.build_residential_mask(parcels_raw, _cfg)
    res_geoms = list(res.geometry.values)
    res_tree = STRtree(res_geoms)
    spec = build_spec(_cfg)
    return blocks, bgs, parcels_raw, res_geoms, res_tree, spec


st.title("Sewershed delineation — live demo")
st.caption(
    "Runs on a toy sewer network with **real street geometry** (15 manholes, "
    "14 pipes, following actual intersections in Trinity Park, Durham NC) and "
    "**real Census demographics** for that same area — no real sewer "
    "infrastructure, so this works for anyone. "
    "[Full README](https://github.com/inmang13/SewershedDelineation) · "
    "validated median IoU **0.875** against 25 expert-delineated real catchments."
)

cfg, G, pipes, index, parcels, ignore_pidx = load_pipeline()
blocks, bgs, parcels_raw, res_geoms, res_tree, spec = load_demographics_inputs(cfg)
blocks_sindex, bgs_sindex, parcels_sindex = blocks.sindex, bgs.sindex, parcels_raw.sindex

with st.form("site"):
    manhole_id = st.selectbox(
        "Manhole ID", [f"MH{n:02d}" for n in range(1, 16)],
        index=0, key="manhole_select",
        help="MH01 = the outlet (whole 14-pipe network). MH08/MH10/MH12/MH13/"
             "MH14/MH15 are headwaters (nothing upstream — a valid, empty "
             "result). Try a few to see how the traced area and its real "
             "demographics change.")
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
                map_style="road"))
            st.caption("Blue = delineated catchment · gray = traced pipes, over a "
                       "**real street basemap** — the streets under the network "
                       "are real (Trinity Park, Durham NC); the sewer network "
                       "itself is invented (see module docstring)")
        served = r["pop"].served if r["pop"] is not None else None
        with right:
            st.metric("Traced pipes", r["res"].n_edges)
            st.metric("Served parcels", len(served) if served is not None else 0)
            st.metric("Area (acres)", round(_acres(geom), 1))
            st.divider()
            st.caption("Real Census demographics for the traced area (2020 "
                       "decennial + ACS 5-year, Trinity Park block groups):")
            b_sub = blocks.iloc[list(blocks_sindex.query(geom, predicate="intersects"))]
            bg_sub = bgs.iloc[list(bgs_sindex.query(geom, predicate="intersects"))]
            served_p, frac = served_parcels_for(geom, parcels_raw, parcels_sindex)
            rec = demo.demographics_for_site(
                geom, b_sub, bg_sub, res_geoms, res_tree, served_p, spec,
                served_area_weight=frac)
            if rec["population"]:
                c1, c2 = st.columns(2)
                c1.metric("Est. population", f"{rec['population']:,.0f}")
                c2.metric("Median HH income",
                          f"${rec['median_income']:,.0f}" if rec["median_income"] else "—")
                c1.metric("Poverty rate",
                          f"{rec['poverty_rate']:.1f}%" if rec["poverty_rate"] is not None else "—")
                c2.metric("% Black", f"{rec['pct_black']:.1f}%")
                c1.metric("% Hispanic/Latino", f"{rec['pct_hispanic']:.1f}%")
                c2.metric("% White", f"{rec['pct_white']:.1f}%")
            else:
                st.caption("No residential Census population in this catchment "
                           "(e.g. a small headwater with no residential parcels).")
            st.divider()
            st.caption("Synthetic (assessor values are not redistributable):")
            if served is not None and not served.empty:
                st.metric("Median parcel value", f"${served['PARVAL'].median():,.0f}")
