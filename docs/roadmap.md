# Roadmap — SewershedDelineation

## Phased Implementation

### Phase 1 — Config loader `src/config.py` ✓ COMPLETE
Read and validate YAML config. Check input files exist, required fields present, CRS valid.
Exit with clear errors on bad config. No GIS logic.

---

### Phase 2 — Network QA & repair `src/network_qa.py` + `src/pdf_maps.py`

Run before building the graph. Produces a cleaned network for downstream phases and a
flag report useful to the GIS layer maintainer.

**Checks and actions:**

**Phase 2 is flag-only — it does NOT modify pipe direction or geometry.** It adds QA columns and
emits flags/maps. Actual direction-setting happens in Phase 3 (see note there).

| Check | Trigger | Action |
|---|---|---|
| `missing_direction` | Null FROMMH or TOMH | If inverts present → mark `direction_inferrable` (Phase 3 fixes it); else flag for manual attention |
| `invert_conflict` | UPSTREAMIN < DOWNSTREAM (water flowing uphill) | Flag — likely direction flip |
| `negative_slope` | SLOPE < 0 | Flag (cross-checked against inverts) |
| `snap_gap` | Endpoints in band (snap_tol, 2×snap_tol] | Flag — won't auto-merge. Sub-tolerance gaps are merged by the graph builder, not flagged |
| `directed_cycle` | Small strongly-connected component (≤ `max_mappable_cycle_nodes`) | Flag + normal map — local flip |
| `large_cycle` | Large strongly-connected component | Flag + `large_cycle_suspects.csv` (member pipes, invert/slope suspects on top); no map |
| `disconnected_component` | Subgraph not connected to main network | Flag with component size (CSV-only) |
| `isolated_manhole` | Manhole with no connected pipes | Flag (CSV-only) |

**Outputs:**
- `output/network_qa_flags.csv` — all flags (UTF-8): type, pipe/node ID, location, severity, description
- `output/network_qa_maps.pdf` — one zoomed page per direction/connectivity flag (config `pdf_flag_types`)
- `output/large_cycle_suspects.csv` — member pipes of large cyclic tangles, suspects sorted to top
- `output/gravity_mains_repaired.shp` — pipe layer + QA columns: `QA_STATUS` (`original` / `flagged` /
  `direction_inferrable`) and `QA_FLAGS`. NOTE: name is historical — Phase 2 does not alter geometry/direction.

**PDF map contents per page:**
- Flagged pipe or node highlighted (red)
- Surrounding network with flow direction arrows
- Nearby manholes labeled by FACILITYID
- Flag type + description as page title
- Scale bar

Test: introduce known direction error and snap gap in a copy of the data; confirm both flags
fire, PDF pages generate at correct locations, repaired shapefile has correct QA_STATUS values.

---

### Phase 3 — Network graph builder `src/graph_builder.py`
Load `gravity_mains_repaired.shp` → snap endpoints → build directed NetworkX graph.
Nodes keyed by FACILITYID where available (from FROMMH/TOMH); coordinate-derived elsewhere.

**Owns real direction inference** (deferred from Phase 2): for pipes marked
`QA_STATUS == "direction_inferrable"` (null FROMMH/TOMH but usable inverts), assign flow direction
from endpoint geometry + invert elevation (higher invert = upstream end) and write it into the graph
(and optionally back to FROMMH/TOMH). This is where the 71 inferrable pipes actually get oriented.

Test: load network, confirm node count, visualize edge directions in GIS, confirm the inferrable
pipes get a sensible direction (upstream end = higher invert).

---

### Phase 4 — Upstream traversal `src/traversal.py` ✓ COMPLETE
Given target manhole (by FACILITYID or snapped coordinate), traverse graph in reverse.
Return set of upstream edge IDs (by `pidx`) and the traversal depth of each.

Validated against `nx.ancestors` + in-degree edge-count oracle; headwater/off-network/bad-ID cases
handled; false-headwater snap guarded. Runner: `run_traversal.py`. See decision_log 2026-06-28.

Test: pick a known manhole, trace manually on map, confirm algorithm matches.

---

### Phase 5 — Population unit assignment `src/population_join.py` ✓ COMPLETE
Buffer upstream pipes by `pipe_buffer_distance_ft`, select served parcels (intersect-any rule),
dissolve into the sewershed polygon. Runner: `run_population_join.py`.

Inclusion rule chosen empirically: validated against 24 hand-delineated sampling polygons
(SiteID == FACILITYID), intersect-any scored median IoU 0.64 vs ~0.04 for stricter rules — boundary
parcels are kept, not excluded (reverses the original plan; see decision_log 2026-06-28). Buffer held
at 50 ft. Known gap: pumped/lift-station sites (e.g. 30804) can't be reproduced by gravity tracing.

Test: DONE — `output/phase5_validation.gpkg` overlays generated vs truth polygons for all 25 sites.

---

### Phase 6 — Polygon construction + output `src/polygon_output.py` ✓ COMPLETE
Dissolve served population units → write `output/sewershed.shp` (+ summary fields:
manhole, area_acres, n_parcels, n_pipes, max_depth). Write `output/debug_upstream_pipes.shp`
(contributing pipes + traversal depth) for review. Runner: `run_polygon_output.py`.

Flags at this stage (delineation-level, not network-level):
| Flag | Trigger | Severity |
|---|---|---|
| `large_catchment` | Polygon exceeds `large_catchment_threshold_acres` (500) | `warning` |
| `no_upstream_found` | Traversal returns zero upstream edges | `review_required` |
| `low_population_match` | <`low_population_match_min_buffer_coverage` (0.5, untuned) of the pipe buffer overlaps any served parcel | `warning` |

`boundary_parcel` was **dropped** — Phase 5 made edge-straddling parcels the norm (median
~16% inside buffer), so it would fire on ~every parcel. `low_population_match` is
scale-invariant (buffer-coverage fraction, not parcel count) so small catchments don't
false-flag. See decision_log 2026-06-29.

Outputs appended to `output/flags.csv` (always written, header even when clean) and a
one-page overview map `output/flag_maps.pdf` (`generate_sewershed_map` in pdf_maps.py).

Test: DONE — site 17506 → 832 ac, 1,227 parcels, fires `large_catchment` only; headwater
18177 → `no_upstream_found`, no polygon, no crash. Map preview rendered and inspected
(`output/preview_png/sewershed_17506_map.png`).

---

### Phase 7 — Main script integration `run.py`
Wire all phases. Read config → QA/repair → build graph → traverse → assign units → polygon → flags.
CLI: `python run.py --config config.yaml`
Option: `python run.py --config config.yaml --qa-only` to run just the network QA without delineating.

---

### Phase 8 — service-area matching pipeline + QC spatial output + basemaps

**Status (2026-07-01): BUILT & tested — awaiting IoU-floor decision.** All modules written, imported
clean, exercised on real data; full sweep ran (960 rows). **morph_close wins: median IoU 0.79** (up
from 0.64), 20/21 aggregate sites ≥ 0.5, robust across sel_r 50–150 × close 50–150. Census-block
methods lost (overshoot). Code-review gate passed (0 confirmed findings; one latent overlay bug found
in manual pass + fixed). **Next action: set `iou_floor_median` + `iou_floor_n_sites` in config
(recommend 0.75 / 20), then re-run `python run_validation.py --sweep` to auto-pick the winner and write
`validation_overlay.gpkg`.** See `docs/SewershedDelineation_checkin_2026-07-01.html`.

Phases 1–6 dissolve served **parcels** into a polygon (median IoU 0.64 vs the hand-drawn truth set).
Two problems drive this phase, plus two add-ons:

1. **Parcels don't tile the landscape** — `served.union_all()` is full of holes (streets/ROW) and
   doesn't resemble the truth polygons. Automate Grace's manual method (select near sewer → buffer →
   fill holes → dissolve → shrink back) as a morphological *close*, and empirically pick the best
   boundary method against the 25-site validation set.
2. **QC lives only in CSV** — cross-referencing against GIS is painful. Emit a spatial QC layer.
3. Maps need a **basemap** for orientation.
4. **Empirically tune** boundary method + radii against the validation set rather than guessing.

Research verdict (settled): build custom (no portable Python tool does WBE service-area delineation
from pipe network + parcels); borrow `shapely.concave_hull` as a candidate and Hill & Larsen 2023
census-block apportionment for the eventual demographic join.

**Thread B — matching pipeline + sweep (built first; the research contribution):**
- `src/boundary.py` (new) — `build_boundary(served, method, **params)` with four candidates:
  `morph_close` (buffer +r → fill ALL holes → buffer −r, keep ALL parts; the primary method),
  `blocks_dissolve`, `hybrid` (parcels + gap-filling blocks, then close), `concave`
  (`shapely.concave_hull`). Shared `fill_holes` / `keep_all_parts` helpers.
- `src/population_join.py` (modify) — accept parcels **or** census blocks as the unit
  (`load_census_blocks`: reproject EPSG:4269→2264, filter `COUNTYFP20 == '063'`). Split
  `selection_radius_ft` out of `pipe_buffer_distance_ft`; **keep the thin pipe buffer as the QC
  ribbon** so `low_population_match` doesn't misfire when selection radius grows.
- `src/validation.py` (new) — sweep `method × selection_radius × close_radius` against the 25 truth
  polygons (`Sampling_Polygons_05212026.shp` + `Sampling_Locations_05212026.shp` in
  CommunityWastewaterDashboard). Join **truth `SiteID` == location `AssetID_tx`** (fail loud if not
  ~24 matched pairs); snap point geometry → trace → assign → boundary → IoU. Print the full table;
  drop terminal **30804** from the aggregate, tag **03442/02201**. Emit `output/validation_overlay.gpkg`
  (truth + generated_parcels + generated_boundary, per-site IoU). **IoU-floor (median ≥ T1 and
  ≥ N/25 sites ≥ 0.5) is a hard stop: run sweep → print table → get T1/N from Grace → finalize winner.**
- `src/polygon_output.py` (modify) — emit `output/sewershed_parcels.shp` (intermediate) +
  `output/sewershed_boundary.shp` (final) + feed the overlay.

**Thread A — QC → GeoPackage (after B):** write network + delineation flags to `output/qc_flags.gpkg`
(point/line geometry), layered by type/severity, alongside the existing CSVs. Normalize the two flag
schemas (network `pipe_id` vs delineation `manhole`).

**Basemaps:** `src/pdf_maps.py` (modify) — `contextily` basemaps on both map functions; reproject the
plot to EPSG:3857 for tile display only (data stays 2264 on disk). `basemap_style`: `satellite`
(Esri World Imagery) default | `street` (CartoDB Positron). Degrade gracefully offline.

**Config + deps:** `boundary_method`, `selection_radius_ft`, `close_radius_ft`, `basemap_style`,
census-block inputs, validation paths, IoU-floor thresholds (deferred), new output paths.
`pip install contextily`; `shapely>=2.0` confirmed (2.1.2).

Data: census blocks supplied at `data/the city Blocks/tl_2021_37_tabblock20.shp` (statewide NC TIGER
2020, EPSG:4269; sibling `nhgis0002_csv/` is demographics for the deferred join).

---

## Deferred / Future

- **Socioeconomic stats:** Join output polygon to ACS census data (race, income, poverty).
  Library candidates: `censusdatadownloader`, direct Census API.
- **Multiple manholes per run:** Loop over a list of IDs, one output shapefile per manhole.
- **Interactive UI:** Streamlit wrapper. Far future.
- **Multi-state support:** Promote CRS to required config field if project expands beyond NC.

---

## Open Questions

- **Phases 2 & 3 status:** both are built (`network_qa.py`, `graph_builder.py`) but never got
  their ✓ COMPLETE marker. Verify they're actually done — graph_builder owns the direction
  inference Phase 4 depends on — before wiring Phase 7. **This is the next action.**
- **`low_population_match` threshold (0.5) is untuned:** no validation set. Watch whether it
  mis-fires on legitimate catchments once more sites are run; adjust `low_population_match_min_buffer_coverage`.
- **Lift-station / pumped sites:** 3 of 24 validation sites (30804, 03442, 02201) have zero overlap
  with the hand-drawn truth polygons. 30804 (Garrett Rd) is a known lift station — pumped systems can't
  be reproduced by gravity tracing. Need to confirm whether 03442 and 02201 are also pumped, and decide
  how to handle pumped sites (flag and skip? separate method?).
- **Buffer tuning:** 50 ft gives median IoU 0.64 with intersect-any. A buffer sweep (25/50/75/100 ft)
  could raise the fit but hasn't been run — deferred unless accuracy needs to improve.
- **Single residual direction error:** the one 2-node SCC (pipes 08373/08374, one digitized backwards)
  is the entire remaining direction problem; no sample site touches it, so it's low priority for the
  GIS maintainer.
- Does the GIS maintainer want the repaired shapefile in a specific format or with specific field names
  to match their existing schema?
