# SewershedDelineation

Semi-automated sewershed delineation tool for eDNA sampling research. Takes a manhole coordinate and a city gravity main shapefile, traces the upstream sewer network, and outputs a polygon built from the dissolved union of served population units (parcels or census blocks).

## Purpose
Delineate the contributing area for each eDNA sampling point so we can estimate the race, income, poverty, and dietary demographics of the sampled population.

## Stack
- Python: GeoPandas, NetworkX, Shapely, matplotlib
- No ArcPy, no QGIS
- Config-driven (YAML): all input paths and parameters in `config.yaml`
- Projection: EPSG:2264 (NAD83 / NC State Plane, US survey feet) — default in `config.yaml`, NC data only. All distance parameters (snap tolerances, buffers) are in feet to match.

## Layout
- `src/` — one module per phase (graph_builder, traversal, population_join, polygon_output, flagging, pdf_maps, main)
- `data/` — input shapefiles (not committed)
- `output/` — sewershed polygon, flags CSV, PDF flag maps, debug layers
- `docs/` — roadmap, decision log

## Key design decisions
- Polygon = dissolved union of population units (not pipe buffer) → clean parcel boundaries for demographic joins
- Direction errors are flagged, not auto-corrected
- Each flag gets a zoomed PDF map for human review without opening GIS
- See `docs/decision_log.md` for rationale on specific choices

## Domain rules (don't re-derive these wrong)
- **A lift station at the sampling point is a terminal end of a gravity
  basin** — everything upstream of it is gravity-fed, so it traces like any
  normal point. Pumps only matter if a trace would have to cross a force main
  mid-basin, which the upstream trace never does. Do NOT exclude or
  special-case pumped sites at the target point (corrected 2026-07-02).
