# Roadmap — SewershedDelineation

## Phased Implementation

### Phase 1 — Config loader `src/config.py` ✓ COMPLETE
Read and validate YAML config. Check input files exist, required fields present, CRS valid.
Exit with clear errors on bad config. No GIS logic.

---

### Phase 2 — Network QA & repair `src/network_qa.py` + `src/pdf_maps.py` ✓ COMPLETE

**Signed off 2026-07-02** via `tests/test_phase2_phase3.py`: synthetic network with one planted
defect per check — every flag type fires at the planted location with the right severity,
`QA_STATUS` values are correct, and geometry/attributes are provably unmodified (flag-only
contract). This fulfills the test criterion below (direction error + snap gap in a copy of the
data). PDF page generation was verified separately on the real network (QC round 1 review).

**Status 2026-07-02: reconciled onto the shared graph** (`graph_builder.build_graph()`/`pidx`) —
QA flags now describe exactly the topology the tool traces. Semantics updated from the table below
(which is kept for history): `invert_conflict` is severity *warning* (geometry-first — attributes
are cross-checks); `snap_gap` is two-tier (auto-repaired ≤ 10 ft = warning / genuinely unconnected
≤ 20 ft incl. end↔junction = review_required); `disconnected_component` flags only fragments
≤ `disconnected_component_max_nodes` (50) — larger components are separate real basins;
`negative_slope` is emitted as `attribute_slope_error`. Current run: 1,066 flags. See
decision_log 2026-07-02.

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

### Phase 3 — Network graph builder `src/graph_builder.py` ✓ COMPLETE

**Signed off 2026-07-02**: synthetic tests (`tests/test_phase2_phase3.py`) confirm one edge per
pipe, every edge oriented start→end of its geometry, tier-1 gaps merged / tier-2 not, and the
planted flip surfacing as the lone SCC. Real-network verification (full graph via
`load_graph_from_config`): 38,359 edges = 38,359 valid pipes, **0** edges whose direction
disagrees with geometry, exactly one non-trivial SCC (size 2 — the known 08373/08374 flip).
Note: invert-based direction inference described below was superseded by the 2026-06-24
geometry-first redesign (line start = upstream); inverts are cross-checks only.

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

**Status (2026-07-02): COMPLETE & validated.** Floor set (0.75 / 20); winner **morph_close
sel_r=100 close=150, median IoU 0.7948, 20/21 aggregate sites ≥ 0.5**; `validation_overlay.gpkg`
written. **Leave-one-out validated: LOO median 0.7898 (optimism gap +0.005), fold-stable** — quote
"median IoU 0.79 (LOO 0.79), 21 gravity-tractable sites." Census-block methods lost (overshoot).
See decision_log 2026-07-02 and `docs/SewershedDelineation_checkin_2026-07-01.html`.

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

## Action items from end-to-end review (2026-07-01)

Ranked by importance. Check off / annotate as resolved; move anything that becomes a
real decision into `decision_log.md`.

**P1 — blocks the research deliverable or its credibility**
1. [x] **Set the IoU floor and finalize the winner.** DONE 2026-07-02: floor 0.75 / 20 set;
   winner **morph_close sel_r=100 close=150, median IoU 0.7948, 20/21 sites ≥ 0.5**;
   `validation_overlay.gpkg` written.
2. [x] **Leave-one-out validation of the sweep winner.** DONE 2026-07-02: `--loo` mode in
   `src/validation.py`. **LOO median 0.7898 vs in-sample 0.7948 (optimism gap +0.005)**;
   fold-stable (morph_close close=150 won all 21 folds). Quotable: "median IoU 0.79
   (LOO 0.79), 21 gravity-tractable sites." See decision_log 2026-07-02.
3. [x] **Reconcile `network_qa.py`** — DONE 2026-07-02: ported onto `build_graph()`/`pidx`;
   all five listed defects fixed (invert check delegates to
   `graph_builder.invert_direction_conflicts`, emits `invert_conflict`/warning; snap_gap
   two-tier with corrected wording; flags keyed by pidx; single-pass component membership).
   Plus: `disconnected_component` reframed to fragments ≤ 50 nodes (the city is multiple
   real basins — largest component is only ~27% of nodes). QA rerun: 1,066 flags, all
   count changes vs baseline reconciled. See decision_log 2026-07-02.

**P2 — correctness/integrity fixes before multi-site use**
4. [ ] **Unify target resolution.** `validation.trace_sites` (`validation.py:149`) uses
   bare `nearest_node` without the false-headwater guard in `resolve_target_node`
   (`traversal.py:139`) — sweep and production resolve targets differently. Extract one
   shared resolution function.
5. [ ] **Redefine Phase 7 as the multi-site runner** (not a single-manhole `run.py`).
   Loop over a site list → one boundary + flag set per site. ~80% extractable from
   `validation.py`'s batch trace (and now `run_competing_review.py`'s loop). This IS
   the actual use case (25 sites). Include: let `trace_manhole` take an explicit
   target instead of batch callers mutating `cfg["inputs"]` per site
   (run_competing_review.py does this today — works, but fragile).
6. [ ] **Fix `qc_flags.gpkg` layer clobbering.** Re-running `run_polygon_output.py` for a
   second manhole overwrites the prior site's `delin_*` layers; a clean run leaves stale
   flags in place (`run_polygon_output.py:85`). Harmless single-site, data-integrity bug
   once multi-site.
7. [ ] **Pumped-site guard in the production path.** Nothing detects a pumped/lift-station
   target (e.g. 30804) — it produces a confidently wrong polygon. Add a known-pumped-sites
   list in config that hard-flags (`review_required`) or refuses.

**P3 — reproducibility & maintainability**
8. [ ] **`requirements.txt`** (geopandas, networkx, shapely>=2, scipy, matplotlib,
   contextily, pyyaml) and a **README** with run-order commands.
9. [ ] **Regression tests.** Pytest on synthetic 5-pipe networks: snap edge cases, false
   headwater, self-loop, 2-node SCC, empty trace, boundary methods on toy geometry.
   Freezes the correctness already proven by the one-shot oracle validations.
10. [ ] **Path-resolution consistency.** `config.load_config` misses `node_layer` /
    `snap_gap_pairs` / `end_nodes` in output resolution (`config.py:73-76`);
    `run_node_layer.py` bypasses `load_config` entirely; `run_polygon_output.py` flattens
    configured paths via `Path(...).name`; `run_qa.py:59` derives paths by string
    `.replace()`. Pick one convention (load_config resolves everything; runners use it).
11. [ ] **`data/README.md` provenance:** source, download date, expected CRS/fields for
    each input (the city GIS layers, parcels, TIGER blocks, NHGIS extract).

**P4 — hygiene & polish**
12. [ ] Repo cleanup: delete/gitignore stray `wb.html`; add `*.lock` to `.gitignore`
    (ArcGIS `.sr.lock` files in data/ and output/); consider renaming
    `check_sample_sites.py` to match `run_*.py` convention.
13. [ ] Doc bookkeeping: update `CLAUDE.md` one-liner (output is the morph-closed
    boundary, not the raw dissolved union); mark Phases 2–3 status in this roadmap;
    state the honest denominator ("20 of 21 gravity-tractable sites") wherever the
    IoU result is quoted.
14. [ ] Minor code polish: remove blocking `input()` in `run_node_layer.py:81`; scale
    bar hardcodes 500/1000 ft regardless of extent (`pdf_maps.py`); flag-threshold
    defaults duplicated between `config.yaml` and `polygon_output.py:43-44`.

---

## QC review round 1 (2026-07-02) — remaining work

Grace's review lives in `QC/network_qa_flags_reviewed_20260702.csv` (flag
comments) and `QC/QC_Review_v1.xlsx` (9 unique polygon/service-area comments).
Item 1 (confirmed snap + review-decisions feedback loop) is DONE — see
decision_log 2026-07-02. Remaining, in planned order:

1. [x] **Downstream leakage — DIAGNOSED (2026-07-02), not a tracing bug:** the
   truth shapefile's SiteID labels are cyclically rotated among the three 20.x
   sites (20.20/20.23/20.29 ↔ 30804/03442/02201); our traces land inside the
   correctly-matched polygons. See decision_log 2026-07-02. **Source shapefile
   fixed and validation re-swept 2026-07-02** (backup at the dashboard project,
   `backup_pre_siteid_fix_20260702/`): median IoU 0.64 → 0.79 (24-site
   aggregate 0.7917, LOO 0.7846), config now sel_r=50 / close_radius=150.
   All site exclusions removed — per Grace, a lift station at the sampling
   point is a terminal end of a gravity basin and traces normally. Site 29962
   (Tract 1.02, IoU 0.37) is the one weak delineation — ties into the
   1.02/13.01 cosmetic item below.
2. [~] **Competing-pipe check (biggest item — 5 of 9 comments): BUILT +
   BATCH GENERATED (2026-07-03), awaiting Grace's review.**
   `population_join.competing_pipe_check` annotates each served parcel with
   distance to nearest in-trace vs foreign main; review_required when foreign
   crosses/is closer, warning when merely within selection radius. Flag-only.
   Verified against the QC parcels: catches 6 of 7 still-served named parcels
   (216813 escapes — its foreign main is beyond the 50 ft radius; decision
   deferred). 164674/165265, 116993/235214/156798 no longer served at all
   (sel_r 100→50 drop). `run_competing_review.py` ran all 24 sites →
   `QC/competing_pipe_review.csv` (1,325 contested parcels: 613
   review_required / 712 warning; blank decision/comment columns) +
   `.gpkg` (contested_parcels / boundary / truth). **Remaining:** (a) Grace
   fills decision column (triage: review_required first; Tract 22 is the
   outlier at 167/399 = 42% contested; start with 18.08/14/5 to calibrate);
   (b) build the consuming pass (exclude | keep | reassign before dissolve);
   (c) decide 216813 (widen foreign-search radius vs leave).
   **Mirror case DIAGNOSED — data, not code:** the three 18.06 parcels
   (169852/54/65) sit on fragment 64951–64955 (`component_141`), which
   touches the traced network at 0 ft but shares no node (endpoint lands on
   a main's midspan — needs a pipe split at the source; manual-snap pass 3
   can't fix endpoint-to-interior). Tract 14's 235214 sits on fragment
   64930–64960 (`component_140`), 712 ft from any other pipe. Both already
   flagged disconnected_component (unreviewed); highest FACILITYIDs in the
   layer = newest construction. → GIS-maintainer list.
   **RESOLVED IN-TOOL (2026-07-03): midspan-junction splits** (see
   decision_log) — component_141 and 25 other midspan tees now connect via
   in-memory pipe splits; the source-layer fix stays on the maintainer list
   as 26 `midspan_junction` flags. component_140 (712 ft away) remains
   genuinely unmapped. **Review CSV regenerated (1,296 contested) — fill in
   THAT version, not any older copy.** New open review items from the
   splits: duplicate main 62451/62452 (directed_cycle, one copy backwards).
3. [ ] **Pairwise overlap QC:** automated overlap check across all output
   polygons (Tracts 5 and 7 may overlap — invisible to eyeball review in GIS).
   Doubles as the acceptance test for the competing-pipe check. Blocked on
   item 2's decisions landing (overlap should be measured post-exclusion).
4. [ ] **Polygon cosmetics (after membership logic is right):** Tract 5-23 is
   two polygons, should be one (bridging the road is acceptable); 1.02/13.01
   highway-ramp gap (cosmetic, Grace says not critical); align polygon edges
   to census blocks / roads / parcels after gap filling.
5. [x] **Site-label → manhole mapping — RESOLVED (2026-07-02):** join
   `Sampling_Locations_05212026.shp` field `Tract` to `AssetID_tx`. Known
   pairs: 5→27508, 7→09289, 14→11069, 18.06→17863, 18.08→30976,
   20.20→03442, 20.23→02201, 20.29→30804, 1.02→29962, 13.01→28175, 23→28080.

---

## Deferred / Future

- **Socioeconomic stats:** Join output polygon to ACS census data (race, income, poverty).
  Library candidates: `censusdatadownloader`, direct Census API.
- **Multiple manholes per run:** Loop over a list of IDs, one output shapefile per manhole.
- **Interactive UI:** Streamlit wrapper. Far future.
- **Multi-state support:** Promote CRS to required config field if project expands beyond NC.

---

## Open Questions

- **Phases 2 & 3 status — RESOLVED (2026-07-02):** signed off and marked ✓ COMPLETE. Evidence:
  `tests/test_phase2_phase3.py` (synthetic planted-defect network, every check fires, flag-only
  contract holds; graph direction follows geometry) + real-network verification (38,359 edges,
  0 direction disagreements, one non-trivial SCC = known 08373/08374 flip). See decision_log.
- **`low_population_match` threshold (0.5) is untuned:** no validation set. Watch whether it
  mis-fires on legitimate catchments once more sites are run; adjust `low_population_match_min_buffer_coverage`.
- **Lift-station / pumped sites — RESOLVED (2026-07-02):** the zero IoU at 30804/03442/02201 was a
  SiteID label rotation in the truth shapefile, not a pumped-tracing failure (decision_log 2026-07-02).
  30804 is still genuinely a lift station, but its gravity trace lands in the correct basin. Remaining
  action: fix the labels (or the join key) and rerun validation to get these sites' real IoU.
- **Buffer tuning — superseded 2026-07-02:** against the corrected truth set the full sweep gives
  median IoU 0.79 (LOO 0.79) at sel_r=50 / close=150 (morph_close), fold-stable in 22/23 LOO folds.
  See decision_log 2026-07-02.
- **Single residual direction error:** the one 2-node SCC (pipes 08373/08374, one digitized backwards)
  is the entire remaining direction problem; no sample site touches it, so it's low priority for the
  GIS maintainer.
- Does the GIS maintainer want the repaired shapefile in a specific format or with specific field names
  to match their existing schema?
