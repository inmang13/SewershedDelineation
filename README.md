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
- **Handles pumped basins** — optional force-main wiring lets a trace cross a lift
  station and pick up the pumped basin upstream of it, with direction (wet well vs.
  discharge) inferred from graph topology alone, since pressurized-main geometry
  ships with no reliable direction of its own (see [Validation](#validation)).
- **Validated** — median IoU **0.875** in-sample / **0.863** leave-one-out against
  24 expert hand-delineated catchments, confirmed on a second, independent 14-site
  ground truth (see [Validation](#validation)).

---

## What it produces

| Output | What it is |
|---|---|
| `output/sewershed_final.gpkg` (`boundary` layer) | one polygon per site — the final catchment |
| `output/flags.csv` | per-site + per-parcel review flags (large catchment, competing pipe, …) |
| `output/qc_flags.gpkg` | flags as spatial layers for GIS review |
| `output/network_qa_*.{csv,pdf,shp}` | network QA report (direction errors, snap gaps, cycles) |
| `output/demographics/…` | per-site Census demographics (optional phase) |
| `QC/force_main_review.csv` | force-main junctions/directions still needing a human decision (optional phase) |
| `QC/force_main_edges.csv` | confirmed force-main connectivity — the file that makes a trace cross a lift station |
| `output/force_main_junctions.gpkg` | force-main pipes, termini, and proposed junctions, for GIS review |

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

The data used for development is **not committed** (municipal GIS layers are not
redistributable). Point `config.yaml` at your own layers:

| `config.yaml` key | Layer | Required? |
|---|---|---|
| `inputs.gravity_main_shapefile` | gravity sewer mains (lines) | **required** — the tool needs this to run at all |
| `inputs.manholes_shapefile` | manholes (points, `FACILITYID`) | **required** |
| `inputs.population_units_shapefile` | parcels (polygons) | **required** |
| `inputs.census_blocks_shapefile` | TIGER blocks | optional — alternate population unit |
| `inputs.force_main_shapefile` | pressurized force mains (lines) | optional — lets a trace cross a lift station; `null` (unset) runs gravity-only |

**Projection:** everything runs in **EPSG:2264** (NC State Plane, US survey feet) by
default — all distance parameters (snap tolerances, buffers) are in feet. Change
`parameters.crs` and the distances together if you use a different projected CRS.

> A committed, fully-runnable **synthetic toy network** (no data dependency) is on the
> roadmap (Phase 9, Track B) so the pipeline can be demo'd end to end without city data.

---

## Try it now — no data needed

```bash
streamlit run demo_app.py
```

Runs on a toy network with **real street geometry** (Trinity Park, Durham NC)
and **real Census demographics** for that area — no municipal data, no
config to edit. Pick a manhole, see the traced pipes, delineated catchment,
and socioeconomic profile on a map.

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

# 4. (optional) Review force-main attachment before wiring pumped basins into
#    the trace. Writes QC/force_main_review.csv + a QC GeoPackage; changes no
#    topology until a human sets decision=snap on the rows they accept.
python run_force_mains.py --config config.yaml
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

## Network QA — scope and limitations

`--qa-only` audits the network with eleven checks (direction conflicts, snap gaps,
cycles, disconnected fragments, isolated manholes, …). Know what you're getting:

- **Flags are conservative, fixed-threshold heuristics.** Expect volume: several flag
  types (`isolated_manhole`, `missing_direction`, `invert_conflict`) are
  *annotation-only* by design — they document data-quality issues the tracer already
  handles geometrically and rarely need action. They are excluded from the PDF maps
  for exactly that reason but still dominate the CSV.
- **Severity (`warning` / `review_required`) is a fixed label per check type**, not a
  confidence score — it does not reliably rank which flags deserve your time. On the
  development network, most `review_required` snap gaps turned out to be odd-but-real
  pipe layouts, not errors.
- **Human review is a file-based loop.** Decisions go in
  `QC/qa_review_decisions.csv` (verbs: `snap`, `flip`, `delete`, `extend`,
  `resolved`, `keep`, plus comments). Confirmed repairs are re-applied **in memory on
  every run** — delineation and QA both read this file; the source shapefiles are
  never modified.

Smarter flag triage is on the roadmap as post-release work (see
`docs/roadmap.md`).

---

## Validation

The 24 catchments were scored against expert hand-delineated truth polygons using
**IoU** (Intersection-over-Union — overlap area ÷ union area; 1.0 = perfect match):

- **Median IoU 0.875** in-sample, **0.863** leave-one-out cross-validation
  (optimism gap +0.012, fold-stable).
- **24 / 24 sites ≥ 0.5.**

**Three honest caveats:**

1. **Truth = agreement with expert manual delineation, not ground-truth accuracy.**
   The reference polygons were drawn by hand by a domain expert; IoU measures how
   well the tool reproduces that expert judgement, not physical correctness.
2. **Gravity-only by default.** The tracer follows gravity mains; force-main wiring
   (below) is optional and off here. A subbasin fed *through* an upstream lift
   station is not reached by a gravity-only trace and is undercounted. (A lift
   station *at* the sampling point is fine — it's the terminal end of a gravity
   basin and traces normally.) See [Force mains: a real accuracy
   tradeoff](#force-mains-a-real-accuracy-tradeoff) — wiring them in is not a strict
   improvement.
3. **Demographic outputs are unvalidated estimates.** The catchment polygons are
   validated (above); the demographic numbers apportioned into them are not — no
   ground truth exists for the demographics of a sewershed's contributing
   population. The method is standard dasymetric census apportionment (residential
   parcels as the population surface, after Hill & Larsen 2023), with Census-rule
   margins of error where propagation is valid. Treat the outputs as estimates
   with stated uncertainty, not measurements.

### Independent second check: 14 flow-meter sites

The 25-site truth set above is one ground truth. A second, independent one exists
in a sibling project: the city's official monitoring-basin polygons, hand-corrected
against imagery, at the 14 sites the city's permanent flow meters sit on. Using
a second truth set matters because it tests whether the 0.875 IoU number
generalizes, rather than measuring how well the tool fits one dataset — and it
does: **median IoU 0.897** gravity-only on this independent set, in the same
range as the 25-site number. (4 of 14 sites didn't trace at all — their
surveyed coordinate sits more than the 50 ft snap tolerance from any network
node, a coordinate-precision gap in the source data, not a tool failure.)


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

  force_mains.py          force-main ingest/filter + shared flag/verdict vocabulary
  force_main_topology.py  force-main-only topology (endpoint clustering, stub pruning)
  force_main_classify.py  classify each terminus as wet well / discharge, roll up to a
                          per-system verdict
  force_main_review.py    the human review file + QC GeoPackage
  force_main_wiring.py    wires confirmed force mains into the traversal graph
  terminal_facilities.py  treatment-plant / lift-station point matching

run.py          production runner (single or multi-site)  ← start here
run_qa.py       standalone network QA
run_demographics.py   demographic-join phase
run_force_mains.py     force-main review pipeline (optional phase — ingest through
                       QC file, no topology change until reviewed)
run_*.py        focused per-phase runners (debugging)
experiments/    one-off boundary-method tuning scripts (not part of the pipeline)

config.yaml     all input paths + parameters
docs/           roadmap, decision log
data/           input shapefiles (not committed)
output/         generated artifacts (not committed)
```

---

## Contributing / AI agents

Picking this up as an AI coding agent (or a new human contributor)? Read
[`AGENTS.md`](AGENTS.md) first — it covers the hard rules (never modify a
source shapefile, one module per phase, fail loud on ambiguous data) and
where to find the *why* behind non-obvious design choices.

## License

[MIT](LICENSE). Every dependency (GeoPandas, Shapely, NetworkX, pandas, NumPy,
SciPy, PyYAML, requests) ships under a compatible permissive license (BSD/MIT/
Apache-2.0) — nothing copyleft in the stack.

## Citing

A JOSS software note + archived Zenodo DOI are in progress (Phase 9). Until then, cite
this repository directly.
