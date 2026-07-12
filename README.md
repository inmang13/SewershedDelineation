# SewershedDelineation

Semi-automated **sewershed delineation** for wastewater-based epidemiology (WBE) /
eDNA sampling. Given a sampling manhole and a city's gravity-main network, it traces
the upstream sewer network, selects the served population units (parcels), and builds
a clean catchment polygon — then optionally attaches Census demographics to each
catchment.

Built for estimating the race, income, poverty, and diet-proxy demographics of the
population contributing to each eDNA sample.

- **Pure Python** — GeoPandas, NetworkX, Shapely. No ArcPy, no QGIS.
- **Config-driven** — all inputs and parameters live in `config.yaml`.
- **Validated** — median IoU **0.875** in-sample / **0.863** leave-one-out against
  24 expert hand-delineated catchments (see [Validation](#validation)).

---

## What it produces

| Output | What it is |
|---|---|
| `output/sewershed_final.gpkg` (`boundary` layer) | one polygon per site — the final catchment |
| `output/flags.csv` | per-site + per-parcel review flags (large catchment, competing pipe, …) |
| `output/qc_flags.gpkg` | flags as spatial layers for GIS review |
| `output/network_qa_*.{csv,pdf,shp}` | network QA report (direction errors, snap gaps, cycles) |
| `output/demographics/…` | per-site Census demographics (optional phase) |

The catchment polygon is the dissolved union of served parcels, cleaned into a
seamless boundary (Delaunay gap-fill + shared-border alignment) — not a raw pipe
buffer — so it has clean parcel edges for demographic joins.

---

## Install

The geospatial stack is easiest via **conda-forge**:

```bash
conda create -n sewershed -c conda-forge python=3.13 \
    geopandas shapely pyogrio networkx numpy scipy pandas matplotlib pyyaml requests contextily
conda activate sewershed
```

Or with pip (needs a working GDAL/GEOS toolchain):

```bash
pip install -r requirements.txt
```

`contextily` is optional — maps still render without it, just with no basemap.

---

## Data inputs (you supply these)

The the city data used for development is **not committed** (city GIS layers are not
redistributable). Point `config.yaml` at your own layers:

| `config.yaml` key | Layer |
|---|---|
| `inputs.gravity_main_shapefile` | gravity sewer mains (lines) |
| `inputs.manholes_shapefile` | manholes (points, `FACILITYID`) |
| `inputs.population_units_shapefile` | parcels (polygons) |
| `inputs.census_blocks_shapefile` | TIGER blocks (optional; alternate unit) |

**Projection:** everything runs in **EPSG:2264** (NC State Plane, US survey feet) by
default — all distance parameters (snap tolerances, buffers) are in feet. Change
`parameters.crs` and the distances together if you use a different projected CRS.

> A committed, fully-runnable **synthetic toy network** (no data dependency) is on the
> roadmap (Phase 9, Track B) so the pipeline can be demo'd end to end without city data.

---

## Quickstart (run order)

Everything runs through **`run.py`** — one command, one or many sites.

```bash
# 1. (optional) Audit the network first — direction errors, snap gaps, cycles.
python run.py --config config.yaml --qa-only

# 2a. Delineate a single site (target from config: inputs.manhole_id / manhole_coordinate)
python run.py --config config.yaml

# 2b. …or several sites by FACILITYID
python run.py --config config.yaml --sites 17506,09289,27508

# 2c. …or a whole list from a CSV (columns: an id column, OR x,y coords; optional SiteID/tract)
python run.py --config config.yaml --sites-file sites.csv

# 3. (optional) Attach Census demographics to the delineated catchments.
#    Needs a free Census API key — see below.
python run_demographics.py --config config.yaml
```

`run.py` runs the full delineation per site — trace → parcel membership →
competing-pipe resolution → boundary construction — then a cross-site pass that
aligns shared borders between neighbouring catchments, and writes everything in one
pass.

### Census API key (demographics phase only)

Get a free key at <https://api.census.gov/data/key_signup.html>, then expose it any
one of these ways (checked in order):

1. env var `CENSUS_API_KEY`
2. a file `.census_api_key` in the project root (one line — **gitignored**, never commit it)
3. `inputs.census_api_key` in `config.yaml`

---

## Validation

The 24 catchments were scored against expert hand-delineated truth polygons using
**IoU** (Intersection-over-Union — overlap area ÷ union area; 1.0 = perfect match):

- **Median IoU 0.875** in-sample, **0.863** leave-one-out cross-validation
  (optimism gap +0.012, fold-stable).
- **24 / 24 sites ≥ 0.5.**

**Two honest caveats:**

1. **Truth = agreement with expert manual delineation, not ground-truth accuracy.**
   The reference polygons were drawn by hand by a domain expert; IoU measures how
   well the tool reproduces that expert judgement, not physical correctness.
2. **Gravity-only.** The tracer follows gravity mains. A subbasin fed *through* an
   upstream lift station / force main is not reached by the gravity trace and is
   undercounted. (A lift station *at* the sampling point is fine — it's the terminal
   end of a gravity basin and traces normally.)

---

## Repository layout

```
src/            one module per phase
  config.py           load + validate config.yaml
  network_qa.py       network QA / flagging (direction, snap gaps, cycles)
  graph_builder.py    shapefile → directed NetworkX graph (+ in-memory repairs)
  traversal.py        upstream trace from a target manhole
  population_join.py   served-parcel selection + competing-pipe resolution
  boundary.py         served units → seamless boundary (Delaunay, seam-align, bridge)
  polygon_output.py   delineation flags + output shapefiles
  qc_output.py        flags → GeoPackage layers
  pdf_maps.py         zoomed PDF flag maps (+ optional basemaps)
  census_api.py       Census API client (demographic phase)
  census_data.py      Census pull / cache / freshness
  demographics.py     dasymetric apportionment into catchments
  validation.py       IoU sweep + leave-one-out cross-validation

run.py          production runner (single or multi-site)  ← start here
run_qa.py       standalone network QA
run_demographics.py   demographic-join phase
run_*.py        focused per-phase runners (debugging)
experiments/    one-off boundary-method tuning scripts (not part of the pipeline)

config.yaml     all input paths + parameters
docs/           roadmap, decision log, dated check-in reports
data/           input shapefiles (not committed)
output/         generated artifacts (not committed)
```

---

## License

MIT (intended — LICENSE file pending, Phase 9 Track E).

## Citing

A JOSS software note + archived Zenodo DOI are in progress (Phase 9). Until then, cite
this repository directly.
