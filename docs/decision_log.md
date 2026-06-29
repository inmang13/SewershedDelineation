# Decision Log — SewershedDelineation

## 2026-06-23 — Initial architecture decisions

**Decision:** Polygon built from dissolved population unit union, not pipe buffer.
**Rationale:** Downstream goal is demographic analysis (race, income, poverty). Clean parcel/block boundaries make the spatial join to census data straightforward. A pipe buffer produces irregular geometry that clips parcels and complicates attribute joins.

**Decision:** Direction errors flagged, not auto-corrected.
**Rationale:** Direction is a data quality problem that requires local knowledge. Auto-correction risks silently producing wrong catchments. A human reviewer with GIS context is better positioned to fix source data.

**Decision:** EPSG:2264 (NAD83 / NC State Plane, US survey feet) as default projection — not 32119.
**Rationale:** All three source layers (gravity mains, manholes, parcels) are in EPSG:2264. Parcels are missing CRS metadata in the file; assigned on load. All distance parameters (buffer, snap tolerance) are in feet to match.

**Decision:** Direction encoded via FROMMH → TOMH fields (FACILITYID references), not a separate direction attribute.
**Rationale:** That's how the city's data is structured. Graph edges are added from FROMMH to TOMH. SLOPE and invert elevation fields (UPSTREAMIN, DOWNSTREAM) are used as cross-checks for direction flags. 76 pipes have null FROMMH and 59 have null TOMH — these are flagged as missing_direction.

## 2026-06-23 — Phase 2 is QA/flag-only; real direction repair deferred to Phase 3

**Correction:** An earlier build marked 71 null-direction pipes `QA_STATUS="repaired"` and claimed
"direction inferred from invert elevations" — but no inference actually happened (FROMMH/TOMH stayed
null, geometry unchanged). Verified against the output shapefile. That was a false claim in a file
meant for the GIS maintainer.

**Decision:** Phase 2 does NOT modify pipe direction. Pipes with null FROMMH/TOMH that have usable
invert elevations are marked `QA_STATUS="direction_inferrable"` (not "repaired"); the description says
the graph builder can recover direction, not that it has. Actual direction-setting (writing FROMMH/TOMH
or a flow field from endpoint geometry + invert elevation) is built into Phase 3, where geometry-based
node assignment makes it clean. The snap-gap check is likewise flag-only (sub-tolerance gaps are merged
by the graph builder, not written back to the layer).
**Rationale:** Honest labeling for a deliverable that goes to the maintainer. Inferring direction
properly needs the Phase 3 node model; doing it halfway in Phase 2 would duplicate that logic. Mutating
the authoritative layer is a bigger step best owned where it's done correctly. `QA_STATUS` values are
now: original / flagged / direction_inferrable.

**Decision:** Accept manhole_id (FACILITYID) as primary input; coordinate as fallback.
**Rationale:** A dedicated manholes layer with FACILITYID exists. Using the ID directly is unambiguous and skips coordinate-to-node snapping. Coordinate input is retained for cases where the user doesn't know the FACILITYID.

**Decision:** Preserve existing Sample_ID field from parcels in output.
**Rationale:** Parcel layer already has ~12k parcels linked to sample sites via Sample_ID. Preserving this enables cross-referencing output sewersheds with existing sample site assignments. (Superseded 2026-06-23: user said ignore Sample_ID — no longer a requirement.)

## 2026-06-23 — Phase 2 (network QA) build & results

**Result:** First full QA run on all 38,359 the city pipes produced 1,276 flags (~3.3%):
isolated_manhole 522, disconnected_component 345, invert_conflict 281, missing_direction 112,
snap_gap 13, negative_slope 2, directed_cycle 1. (Cycle later split per-SCC; see below. Status counts
later corrected — no pipes are truly "repaired" in Phase 2; see the QA/flag-only entry below.)

**Finding — large directed cycle:** The single directed_cycle flag implicates 558 pipes in one loop.
Gravity can't flow in a circle, so this is a systematic direction error across a whole subnetwork,
not an isolated mistake. High priority for the GIS maintainer; will corrupt any sewershed traced
through that area. Needs targeted investigation (where is it, what's the correct direction).

**Decision:** Snap-gap check uses a scipy cKDTree over endpoints, not a buffer self-join.
**Rationale:** The buffer-based GeoDataFrame self-join over ~76k endpoints was O(n²) and hung for
minutes on the full dataset. KD-tree `query_pairs` is O(n log n) and returns in seconds. Also
simplified the logic: gaps within node_snap_tolerance are auto-merged by the graph builder (no flag);
only gaps in the band (tolerance, 2×tolerance] are flagged as genuine disconnections.

**Decision:** PDF maps generated only for direction/connectivity flag types, not all flags.
Config key `pdf_flag_types` controls this (default: invert_conflict, missing_direction, snap_gap,
negative_slope, directed_cycle → ~409 pages). All flags still appear in the CSV.
**Rationale:** 1,276 map pages took ~20 min to generate and is unusable for review. The 867 bulk
completeness flags (isolated_manhole + disconnected_component) read fine as CSV rows and don't need
zoomed geometry. Making it config-driven lets the maintainer re-scope without code changes.

**Decision:** CSV written explicitly as UTF-8.
**Rationale:** Default Windows encoding (cp1252) mangled em-dashes in flag descriptions. UTF-8 is the
portable choice for a file that may be opened in Excel, Python, or a GIS on any platform.

**Open item — repaired shapefile size:** The repaired layer keeps all 62 original columns, so the
.dbf is ~131 MB. Acceptable for handoff to the GIS maintainer (preserves their schema), but the
downstream graph builder only needs geometry + FACILITYID + FROMMH/TOMH + invert/slope + QA fields.
Consider a slim variant if the full file becomes a nuisance.

**Note — matplotlib backend:** pdf_maps.py forces the non-interactive `Agg` backend so PDF generation
works headless (no display). Required for background/script runs.

## 2026-06-23 — Directed-cycle investigation & per-SCC redesign

**Investigation:** The single "558-pipe cycle" was an artifact of counting every pipe touching a cycle
node. Strongly-connected-component (SCC) analysis revealed the true structure: 4 non-trivial SCCs —
one of 270 nodes/305 pipes spanning ~10×16 miles, plus three tiny ones (4, 3, 2 nodes).

The 270-node SCC is not a physical loop (gravity can't circulate citywide); it's a logical loop created
by a modest number of misdirected pipes stitching the real tree network into a cycle. Within it: 17 pipes
flow uphill (invert conflict), 9 have negative slope, 42 have no invert data. Removing the 17 uphill
edges collapses it from 270 → 131 nodes — so invert conflicts are necessary but not sufficient to
untangle it; the residual flips lack invert evidence and need manual review.

**Decision:** Replace the lumped `directed_cycle` flag with per-SCC reporting via
`nx.strongly_connected_components` (O(V+E), vs. exponential `simple_cycles`).
- Small SCCs (≤ `max_mappable_cycle_nodes`, default 10) → `directed_cycle` flag, normal map page.
- Large SCCs → `large_cycle` flag (no map) + `large_cycle_suspects.csv` listing all member pipes with
  invert/slope suspects flagged and sorted to the top.
**Rationale:** Separates the easy local flips (mappable, quick fixes) from the pathological tangle
(unmappable, needs human review with a prioritized suspect list). A citywide map of the big SCC shows
nothing useful; a ranked pipe list does.

**Decision:** Big-SCC handling stops at a suspect list — no automated feedback-arc-set solver (deferred).
**Rationale:** Full untangling is NP-hard and the invert/slope evidence only explains ~half the tangle.
A heuristic solver is more code to build/validate for uncertain payoff; the suspect list gives the
maintainer the actionable 80% now. Revisit if manual cleanup proves too slow.

**Decision:** PDF flag maps generated per flag during Phase 6.
**Rationale:** Reviewers need to evaluate flagged network locations without orienting in a GIS. A zoomed PDF showing the flag location with surrounding network and population units is faster to review and easier to share.

**Decision:** Config-driven via YAML, single `run.py` entry point.
**Rationale:** Makes the tool reproducible (config is a record of the run) and easy to wrap in a future UI. Avoids hardcoded paths that break when data moves.

## 2026-06-24 — Geometry-first redesign & node layer (src/node_layer.py)

**Finding — attribute data is spotty, GIS geometry is reliable.**
QA review confirmed that FROMMH/TOMH nulls, invert conflicts, and disconnected component flags are largely artifacts of incomplete attribute entry, not bad geometry. Pipes are digitized in the correct flow direction (start point = upstream, end point = downstream). This invalidates the attribute-first QA approach and motivates a geometry-first redesign.

**Decision:** Use line geometry as the primary source of pipe direction, not FROMMH/TOMH.
**Rationale:** The GIS maintainer has kept the digitized direction correct even where attribute fields are blank. Geometry is the authoritative source for this dataset. FROMMH/TOMH and invert elevations are treated as cross-checks, not ground truth.

**Decision:** Build a standalone geometry-derived node layer (`src/node_layer.py`, `run_node_layer.py`) before redesigning the QA flag logic.
**Rationale:** Needed to verify that geometry-based node snapping produces a clean network before committing to a full flag-logic rewrite. The node layer also becomes a direct input to the Phase 3 graph builder.

**Decision:** Node layer uses a two-pass snap: pass 1 merges coincident endpoints (1 ft), pass 2 repairs snap gaps among end nodes only (10 ft).
**Rationale:** A single tolerance cannot distinguish between (a) perfectly coincident endpoints that should always merge and (b) slightly-offset endpoints from pipes that share a physical manhole but were digitized a few feet apart. Pass 2 targets only `start_only` / `end_only` nodes — junctions are excluded because they already have continuity. This prevents short pipe segments from being falsely collapsed: a short pipe whose own start and end fall within the repair radius would need to be an end node on both sides, which is impossible if any other pipe connects through it.

**Decision:** Snap gap QC outputs a line layer (`output/snap_gap_pairs.gpkg`) connecting near-miss end-node pairs, not a point layer or CSV.
**Rationale:** A line connecting the two near-miss nodes is directly interpretable in GIS — you can see exactly which pipes are almost-but-not-quite connected and judge whether the gap is a real topology break or acceptable. Distance is an attribute on the line for filtering.

**Result — node layer QC:** After two-pass snap with 10 ft end-node repair, zero residual near-miss pairs remain. All snap gaps in the the city network are resolved at the geometry level. End node counts reflect the true headwaters and outlets of the system.

**Decision:** End node layer (`output/end_nodes.gpkg`) filtered from the repaired node set, not the raw pass-1 nodes.
**Rationale:** Ensures end nodes reflect the repaired topology. Any node classified as `start_only` or `end_only` after repair is a genuine dead end, not an artifact of a snap gap.

## 2026-06-28 — Phase 3: directed graph builder (src/graph_builder.py)

**Decision:** The graph builder shares the node layer's snap, not a second snapping pass.
Refactored `node_layer.py` to expose `snap_endpoints()` → `SnapResult` (the exact per-endpoint
node assignment plus a positional pipe index). `build_node_layer()` is now a thin wrapper over it,
and `graph_builder.build_graph()` consumes the same result.
**Rationale:** One snap, one topology. An earlier option — re-snapping each pipe endpoint to the
nearest node centroid — was rejected: pass-2 merging moves a junction centroid several feet from its
raw endpoints, so nearest-neighbor could assign an endpoint to the wrong node in dense areas, silently
diverging the graph from the node layer it claims to share. The union-find assignment is ground truth.
Verified the refactor is bit-for-bit identical to the prior node layer (38,414 nodes, same roles/coords).

**Decision:** Edges keyed by positional pipe index (`pidx`), not FACILITYID.
**Rationale:** FACILITYID is null on some pipes (stored as "?") and is not unique, so it can't address a
pipe. The positional index into the pipes GeoDataFrame is unique and is the geometry-lookup key Phase 5
will need. Confirmed `pipes.iloc[pidx]` stays aligned even though empty geometries are skipped during
endpoint collection (the skipped row still advances the counter).

**Decision:** Use a `MultiDiGraph` (NetworkX), not a `DiGraph`.
**Rationale:** A plain DiGraph silently collapses two pipes between the same node pair into one edge —
losing real twin mains and hiding duplicates. MultiDiGraph preserves them; parallel edges (73) and
self-loops (1) are reported as QC counts instead of vanishing.

**Decision:** Direction comes from line geometry only; inverts are a cross-check that never overrides it.
`invert_direction_conflicts()` flags edges where invert elevations imply uphill flow, skipping inverts
≤ 0 (the dataset's no-data placeholder).
**Rationale:** Continuation of the 2026-06-24 geometry-first decision. Invert fields are spotty (some
imply nonsensical ~200 ft rises); using them only as corroboration avoids importing their errors.

**Result — geometry-first validated.** First full graph build on all 38,359 pipes:
38,414 nodes / 38,359 edges; 7,610 sources (headwaters), 172 sinks (outlets), 0 isolated;
148 weak components; 1 self-loop; 73 parallel edges; **267 invert cross-check conflicts.**
The headline: a correct gravity network is a forest (zero directed cycles). The attribute-era graph had
a single 270-node strongly-connected component (a citywide logical loop from misdirected attribute
fields). Geometry-first collapsed it to **one 2-node SCC** — pipes 08373/08374 between nodes 6994/7000,
one digitized backwards. That is the entire residual direction error in the city: a one-pipe manual flip.
This confirms the geometry-first pivot and supersedes the attribute-era SCC concern in the roadmap's
open questions.

**Decision:** Graph is not persisted to disk in Phase 3 — Phase 4 (traversal) consumes `build_graph()`
in memory.
**Rationale:** The graph is a runtime structure, not a deliverable; the node-layer `.gpkg` files already
cover GIS inspection of the same topology. Revisit if a serialized graph proves useful for debugging.

**Known cleanup (deferred, logged for later):** `network_qa.py` contains an older directed-graph + SCC
pipeline (`_build_geometry_graph`) that re-snaps endpoints via nearest-neighbor — slower and able to
diverge from the shared snap. Consolidate it onto `build_graph()` when Phase 7 wiring touches QA. Not
urgent; the QA phase already ran and its outputs stand.

## 2026-06-28 — Sample-site pre-flight check before Phase 4

**Decision:** Validate the QA flags only where they matter — against the actual sample sites — rather
than reviewing all ~340 graph flags. Built `check_sample_sites.py`: snaps each sampling-location point
to its nearest graph node and flags any that land in the 2-node SCC, on the self-loop, or in a small
disconnected component (< 50 nodes).
**Rationale:** A direction error or orphaned fragment only corrupts a result if a sample manhole sits
in or upstream of it. Reviewing flags that no sample touches is wasted effort; this targets the only
cases that can silently produce a wrong catchment.

**Source of sample sites:** `Sampling_Locations_05212026.shp` in the CommunityWastewaterDashboard
project (`data/raw/shapefiles/`), EPSG:2264, 25 points. Manhole reference is the `AssetID_tx` field.
The point geometry (not the AssetID) is used for snapping, consistent with the geometry-first approach.

**Result:** 24 of 25 active sites snap cleanly (< 5 ft) onto healthy network components, all with
in-degree ≥ 1 (upstream pipes exist to trace). None touch any of the three problem types. The single
flag is `Tract 18.02_old` (PS103) — a **retired** site whose active replacement (Tract 18.02, AssetID
17506) is clean. **Conclusion: green light for Phase 4; the residual graph flags need no review.**

**Note — site 17863 (Tract 18.06) loose snap (48.6 ft):** Confirmed by user this is expected — the
sampling point was placed off the manhole using satellite imagery to guide field crews, so the point
sits a little off the pipe. Not a data error. It still lands in a healthy 510-node component and traces
normally. (Its own field note also references manhole "17663" vs. AssetID "17863" — a separate label
discrepancy, not affecting the trace.)

**Correction:** The sampling program now has **25 sites**, not the 22 referenced in older docs. Roadmap
updated.

## 2026-06-28 — Phase 4: upstream traversal (src/traversal.py)

**Decision:** Resolve a target manhole to a graph node by snapping its point geometry, not by matching
FROMMH/TOMH. `manhole_id` → look up the point in manholes.shp → snap to nearest node within
`manhole_node_snap_ft` (5 ft); raw `manhole_coordinate` → snap within `manhole_snap_distance_ft` (50 ft).
A snap beyond tolerance raises `TargetResolutionError` (manhole not on the modeled network).
**Rationale:** Consistent with the geometry-first approach. Verified manholes sit essentially on pipe
endpoints — 99.3% snap within 5 ft, median 0.0 ft — so 5 ft is a tight, justified tolerance.

**Decision:** `AssetID_tx` in the sampling layer is the same identifier space as manhole `FACILITYID`
(24/25 sample IDs match exactly; the only miss, PS103, is a pump station, not a manhole). Sample sites
can therefore drive per-site delineation directly by AssetID.

**Decision:** Reverse-BFS traversal with a visited-set on nodes; collect each visited node's in-edges
with traversal depth. Edges are addressed by `pidx` (the Phase 5 handoff). Validated against
`nx.ancestors` (upstream node set matches) and an in-degree sum (edge count matches).
**Rationale:** Hand-rolled rather than a library call because per-edge depth is needed. The visited-set
makes the 2-node SCC and the self-loop safe (no infinite loop), and each contributing edge is collected
exactly once.

**Decision:** A headwater target (in_degree 0) returns an empty edge set as a first-class result
(Phase 6's `no_upstream_found`), not an error.

**Fix — false-headwater guard (from code review):** At a manhole where the incoming and outgoing pipe
endpoints didn't merge, a degenerate `start_only` node (in_degree 0) can sit marginally closer to the
manhole point than the real junction, which would silently return an empty trace. `resolve_target_node`
now prefers a node with in_degree > 0 within tolerance when the nearest has none. This affected exactly
1 of 38,235 the city manholes (51909) — rare, but a silent wrong answer, so worth the guard.

**Fix — FACILITYID matching (from code review):** Exact string match first (so non-numeric IDs like
PS103 work), then an integer-normalized fallback so padded/unpadded numeric IDs ('17506' / '017506' /
17506) all resolve. See `_match_facilityid`.

**Decision:** Config default target changed from `18177` to `17506` (Tract 18.02 sample site). 18177 is a
genuine headwater (no upstream), so it made a misleading demo. 17506 traces 465 contributing pipes.

**Cleanup (from code review):** Centralized the read→reproject→build_graph boilerplate into
`graph_builder.load_graph_from_config()` (was duplicated across three runners, and had already drifted —
one used a default for `snap_gap_search_radius_ft`, two didn't). Added shared `build_node_index` /
`nearest_node` / `nodes_within` spatial helpers in `graph_builder`, consumed by traversal and the
sample-site check. `resolve_target_node` / `trace_manhole` accept an optional prebuilt `NodeIndex` to
avoid rebuilding the KD-tree when batch-tracing many targets.

**Result:** Phase 4 complete and validated. Correctness review returned no bugs after the three fixes
above; cleanup findings applied. Real traces: 17506 → 465 pipes (depth 54), 22942 → 495 pipes, 26532 →
79 pipes (≈ its whole 83-node component, as expected). Ready for Phase 5 (buffer upstream pipes →
population-unit join), which consumes `TraversalResult.pidx_list`.

## 2026-06-28 — Phase 5: population unit assignment (src/population_join.py)

**Decision:** Parcel inclusion rule = **intersect-any** — a parcel is served if it touches the dissolved
upstream-pipe buffer (`pipe_buffer_distance_ft`, 50 ft) at all.
**Rationale (empirical, validated):** Tested three rules against 24 hand-delineated sampling polygons
from the CommunityWastewaterDashboard (`Sampling_Polygons_05212026.shp`; join key **polygon SiteID ==
manhole FACILITYID**, confirmed). Scored by IoU (intersection-over-union) of the dissolved generated
polygon vs the truth polygon:

| rule | median IoU |
|---|---|
| intersect-any | **0.64** (20/24 sites ≥ 0.5) |
| ≥50% parcel area inside buffer | 0.03 |
| centroid inside buffer | 0.04 |

The strict rules collapse because the 50 ft buffer is a thin ribbon — the median served parcel is only
~16% inside it, so "fully inside" yields ~4 parcels/site and centroid-in misses street-frontage parcels
the main actually serves. The edge-straddling parcels ARE the sewershed.

**Decision reversal (with user sign-off):** This overturns the earlier "don't include boundary parcels"
instruction. That instruction was given against a mental model (parcels mostly inside the buffer) the
geometry contradicts. User was shown the IoU table + the saved GIS overlay and confirmed intersect-any.
Boundary parcels are therefore neither excluded nor flagged in Phase 5.

**Decision:** Buffer distance stays at 50 ft for now. Intersect-any at 50 ft already gives median IoU
0.64; a buffer sweep to optimize is deferred (offered, not requested).

**Validation artifact:** `output/phase5_validation.gpkg` (layers `generated_intersect_50ft`,
`validation_truth`, each with per-site IoU) for GIS review. Per-run output:
`output/sewershed_<id>.gpkg` (served_parcels / sewershed / pipe_buffer).

**Known limitation — lift stations / pumped systems:** 3 of 24 sites (30804, 03442, 02201) have zero
overlap with truth. At least 30804 (Garrett Rd Lift Station) is pumped, not gravity-fed, so a gravity
trace structurally cannot reproduce its catchment. Force-main / pumped sites are a known gap; gravity
delineation only applies to gravity-served sites. Investigate the other two separately.

**Design:** Phase 5 returns served parcels (`PopulationResult`) + the buffer; the dissolved polygon is a
helper. Final shapefile output + delineation-level flags (large_catchment, no_upstream_found, etc.)
remain Phase 6. `load_population_units` assigns the parcel layer's missing CRS from
`population_units_crs` then reprojects, and drops null/empty geometries.

**Result:** Site 17506 → 1,227 served parcels, 832 acres, IoU 0.58 vs truth (1,199 ac). Empty pidx_list
(headwater) → empty result. CRS reprojection verified against the polygons' Shape_Area field (exact).

## 2026-06-29 — Phase 6: polygon construction + delineation flags (src/polygon_output.py)

**Decision:** Dropped the `boundary_parcel` flag.
**Rationale:** It was specified ("parcel straddles the pipe buffer edge", warning) before
the Phase 5 finding. With intersect-any at a 50 ft buffer, the median served parcel is only
~16% inside the buffer — edge-straddling is the *normal* case, not an anomaly. The flag would
fire on ~95% of parcels every run: pure noise. User signed off on dropping it (the other three
delineation flags remain).

**Decision:** `low_population_match` is scale-invariant — it fires when the fraction of the
**pipe buffer ribbon** overlapping any served parcel falls below
`low_population_match_min_buffer_coverage` (default **0.5**), NOT on an absolute parcel count.
**Rationale:** An absolute count false-flags legitimately small catchments (few pipes → few
parcels is correct, not suspicious). The coverage fraction is ~1.0 for a well-covered catchment
regardless of size; a low value means pipes run through unparcelled ground (coverage gap,
undeveloped/industrial land, or a CRS mismatch). **Threshold is untuned** — there is no
validation set for it the way intersect-any was empirically validated. Added to config.

**Decision:** Memoized `PopulationResult.dissolve()`; reused the dissolved polygon across flags,
shapefile, and map instead of recomputing `union_all()` 3-4× per run.
**Rationale:** The served-parcel union is the most expensive op in the pipeline. Code review
found it recomputed in `compute_flags` (×2), `build_sewershed_gdf`, and the map title. One cache
on the result object collapses all of them.

**Deferred:** The Phase 4→5 trace/join preamble is now duplicated between `run_population_join.py`
and `run_polygon_output.py`. Not extracted — Phase 7 (`run.py`) wires all phases together and is
the right home for a shared `(res, pipes, pop)` helper. Extracting now would just be reworked then.

**Provenance note (parcel layer):** `data/nc_the city_parcels_poly.shp` originates from the
**the city County Assessor** (per the layer's `SOURCEAGNT` field on every row), normalized to a
standardized national parcel schema (ALTPARNO, CNTYFIPS, PARUSECODE, LANDVAL, PARVAL, …). It
ships no `.prj`, so EPSG:2264 is declared in config. It already carries land value / use code /
address fields, which will help the eventual ACS demographic join. There is also a pre-existing
`Sample_ID` field tagging parcels to sample sites — not yet used.

**Schema (provisional):** sewershed.shp summary fields are manhole / area_acres / n_parcels /
n_pipes / max_depth (all ≤10 chars for DBF). Still an open question whether the GIS maintainer
wants specific field names — see roadmap.

**Result:** Site 17506 → 832.4 ac, 1,227 parcels, 465 pipes (depth 54); fires `large_catchment`
only. Headwater 18177 → `no_upstream_found` (review_required), no polygon written, no crash.
flags.csv always written (header even when clean). Overview map verified visually
(`output/preview_png/sewershed_17506_map.png`). Phases remaining: 7 (integration); 2 & 3 built
but never marked ✓ in roadmap — worth a status check before Phase 7.
