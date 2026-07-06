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

## 2026-07-01 — Phase 8: service-area matching pipeline + QC spatial output + basemaps

**Decision:** Replace the raw parcel-dissolve polygon with an empirically-chosen **boundary method**,
selected by sweeping four candidates against the 25 truth polygons. Candidates: (a) parcels +
**morphological close** (buffer +r → fill ALL holes → buffer −r, keep ALL parts), (b) census-blocks
dissolved, (c) hybrid (blocks fill parcel gaps, then close), (d) concave hull (`shapely.concave_hull`).
**Rationale:** The dissolved parcel union is full of holes (parcels omit streets/ROW) and doesn't
resemble the hand-drawn truth polygons; median IoU 0.64 leaves headroom. Rather than guess distances,
tune method + radii against the validation set. `morph_close` is the locked *primary* method (automates
Grace's manual workflow); the others are compared, IoU decides.

**Decision:** **Fill ALL holes** → single solid polygon per part, and **keep ALL parts** (multipart).
**Rationale:** Interior holes are lakes/cemeteries/ROW with no served population — filling them avoids
skewing the eventual parcel demographic join, and matches how the truth polygons were drawn.
Disconnected served fragments are real (a pumped sub-area, a detached served pocket), so keep them.

**Decision:** **Success metric = IoU floor** — median IoU ≥ T1 **and** ≥ N/25 sites ≥ 0.5. **T1 and N
are deliberately left unset** until the first sweep reveals the achievable ceiling; Grace sets them
after seeing the full printed table. **Rationale:** Setting a floor before knowing what's achievable
either lowballs (accepts a bad method) or is impossible (rejects the best method). The sweep is a hard
stop for human input, not an auto-pick.

**Decision:** Census blocks are a **candidate boundary unit and population unit**, not just parcels.
Source: `data/the city Blocks/tl_2021_37_tabblock20.shp` (statewide NC TIGER 2020, EPSG:4269; filter
`COUNTYFP20 == '063'`, reproject to 2264 on load). **Rationale:** Blocks *tile* the landscape — exactly
the "parcels leave gaps" problem — so blocks-dissolved is a plausible sweep winner, and Hill & Larsen
2023 establishes census-block apportionment as the WBE standard for the downstream demographic join.

**Decision:** Split **`selection_radius_ft`** (unit selection, swept) from **`pipe_buffer_distance_ft`**
(kept as the thin QC ribbon). **Rationale:** `low_population_match` measures `sewershed ∩ buffer /
buffer.area` against the selection buffer; if selection radius grows during the sweep, the ribbon
fattens and the flag misfires. Keeping the QC ribbon on its own thin buffer preserves the flag's meaning.

**Decision:** Validation join is **truth `SiteID` == sampling-location `AssetID_tx`** (not FACILITYID —
the points layer has no FACILITYID field; all three ids are the same space, log 2026-06-28). The sweep
snaps the point *geometry* to the graph node (geometry-first), and **asserts ~24 matched pairs, aborting
loudly otherwise.** **Rationale:** The join is the linchpin of the research deliverable; a silent
mismatch yields an all-garbage IoU table. Drop terminal/pumped **30804** from the aggregate; keep
**03442/02201** in but tagged for later diagnosis.

**Decision:** QC flags emitted to a **GeoPackage** (`output/qc_flags.gpkg`, multi-layer by type/severity)
alongside the existing CSVs, covering both network flags (`network_qa`, keyed `pipe_id`) and delineation
flags (`polygon_output`, keyed `manhole`). **Rationale:** Cross-referencing a CSV against GIS to find a
problem pipe is painful; a spatial layer lets the maintainer filter and zoom directly. CSV stays for
non-GIS review.

**Decision:** Basemap default = **satellite** (Esri World Imagery via `contextily`), config toggle to
`street` (CartoDB Positron). Plot reprojected to **EPSG:3857 for tile display only** (data stays 2264
on disk); degrade gracefully (skip basemap + warn) when offline. **Rationale:** Field crews and
reviewers orient faster on imagery; requiring internet at map time shouldn't break headless runs.

**Research verdict:** Build custom — no portable Python tool does WBE service-area delineation from a
pipe network + parcels (SewerGEMS/InfoSewer/ArcGIS UN need licensed platforms; DEM stream-burning is
the wrong paradigm for a known pipe network; `sewergraph` validates the architecture but produces no
service-area polygon). Borrow `shapely.concave_hull` (candidate) and Hill & Larsen 2023 (census-block
apportionment for the demographic join).

## 2026-07-02 — IoU floor set, LOO validation, network_qa reconciled onto the shared graph

**Decision:** IoU floor set at median >= 0.75 and >= 20 aggregate sites >= 0.5 (roadmap recommendation,
user-approved). Sweep winner auto-finalized: **morph_close, sel_r=100 ft, close=150 ft — median IoU
0.7948, 20/21 aggregate sites >= 0.5**. `output/validation_overlay.gpkg` written for the winner.

**Result — leave-one-out validation (the quotable number):** LOO median IoU **0.7898** vs in-sample
0.7948 — optimism gap +0.005, i.e. the tuned number generalizes. Fold-stable: morph_close close=150
won all 21 folds (sel_r=50 in 20 folds, sel_r=100 in 1 — those two are an in-sample near-tie, 0.7937
vs 0.7948, adjacent grid points). Write-up phrasing: "median IoU 0.79 (leave-one-out 0.79) across the
21 gravity-tractable validation sites." Implemented as `--loo` in src/validation.py: pure pandas
re-aggregation of the per-site sweep rows (no geometry rerun; reads output/validation_sweep.csv or
runs with --sweep). Fold rule = highest median over the other 20 sites (tie-break: n sites >= 0.5);
the IoU floor is not applied inside folds — its site-count term has no 20-site analogue. Fold logic
smoke-tested on a hand-computed synthetic table before the real run.

**Decision:** network_qa.py ported off its private `_build_geometry_graph` nearest-centroid re-snap
onto the shared `graph_builder.build_graph()`/pidx topology (closes the 2026-06-28 "known cleanup").
QA flags now describe exactly the network the tool traces. User signed off on the three deliverable-
changing calls below. Specifics:
- **Flags keyed by pidx**, not FACILITYID (null/non-unique IDs smeared flags; the old
  disconnected_component flags in fact never tagged any pipe — their "component_N" pseudo-IDs matched
  nothing, a silent no-op). Manhole flags (isolated_manhole) carry no pidx and never touch pipe fields.
- **invert_conflict, severity warning** (was attribute_direction_error): the check is now
  `graph_builder.invert_direction_conflicts(G)` — inverts disagreeing with geometry direction,
  skipping <= 0 placeholder inverts. Geometry-first: attributes are cross-checks and never override
  geometry, so warning, not review_required. Name matches config pdf_flag_types and the roadmap table.
- **snap_gap is two-tier:** (1) pass-1 end-node pairs within (snap_tol, 10 ft] are auto-connected by
  the graph's pass-2 repair — warning, "fix source geometry" (the old "these will not connect" wording
  was false for these); (2) node pairs that survived the repair, within 20 ft, sharing no pipe, at
  least one an end node — review_required, genuinely unconnected. Tier 2 includes end-junction pairs,
  which pass 2 never repairs: 7 such gaps sit INSIDE the 10 ft repair radius and were invisible to the
  old check.
- **disconnected_component = small fragments only** (new config `disconnected_component_max_nodes`,
  default 50, matching check_sample_sites.py). The port revealed the city is genuinely multiple large
  basins — the largest weak component holds only ~27% of nodes (10,350 of 38,414; next largest 5,879,
  2,919, ...). "Not in the largest component" would tag 72% of pipes as broken; fragments <= 50 nodes
  are the actual QC signal.
- O(pipes x components) membership loops replaced by single-pass node->component maps over graph edges.
- Config pdf_flag_types: negative_slope -> attribute_slope_error (the emitted name; nothing ever
  emitted negative_slope).

**Result — QA rerun on all 38,359 pipes:** 1,066 flags vs the 1,076 of the 2026-06-25 baseline, every
per-type change reconciled: isolated_manhole 522 (unchanged); invert 281 -> 267 (placeholder-invert
skip + geometry orientation; matches Phase 3's validated 267); disconnected 146 -> 105 (<= 50-node
semantics); missing_direction 112 (unchanged); snap_gap 13 -> 57 (13 tier-1 + 44 tier-2; the old 2 ft
band both overstated "will not connect" and under-searched); attribute_slope_error 2 (unchanged);
directed_cycle 0 -> 1 — the known 08373/08374 two-node SCC, which the old private re-snap MISSED:
exactly the topology-divergence defect that motivated the port. Full PDF maps not regenerated (6-page
preview only); run `python run_qa.py` for the full deliverable before handoff.


---

## 2026-07-02 — QA review feedback loop (decisions file + manual snaps)

**Decision:** Human QA review is consumed from a decisions CSV
(`QC/qa_review_decisions.csv`, config key `inputs.qa_review_decisions`;
loader `src/qa_review.py`) rather than being re-done each round. Three
decision values:
- `snap` — human-confirmed connection: merged in the shared snap as a new
  pass 3 (`node_layer.snap_endpoints(manual_snaps=...)`), no junction
  restriction. Applied identically in QA and in `load_graph_from_config`,
  so QA, traversal, and delineation share one repaired topology.
- `resolved` — flag still fires but carries `review_status=resolved`:
  kept in CSV/GPKG for the record, dropped from PDF maps and the "open"
  review_required count.
- `keep` — still open; the reviewer comment rides along on the flag.

Matching is exact `(flag_type, pipe_id)` with a 50 ft proximity fallback
(same flag_type) because `component_N` ids are not stable across runs.

**Rationale:** Grace reviewed all 44 tier-2 snap_gap flags (2026-07-02):
only ONE is a real gap (pipes 30481/30482/30483, 2.78 ft — an end node
beside a junction, exactly the end↔junction case pass 2 skips by design).
The rest are real non-connections (opposite flow, laterals, WWTP/pump
ends). Editing source geometry was rejected: data/ is the city's layer;
a config-driven merge is reproducible and reversible.

**Result (QA rerun, 38,359 pipes):** snap_gap 57→56 (the snapped gap no
longer fires; graph nodes 38,414→38,413, edges unchanged, the merged node
is a junction carrying 30481/30482/30483 with in=2/out=1);
disconnected_component 105→103 (the snap joined the formerly dangling
fragment to the network — expected effect, not a regression);
review_required 44 with only 2 open (the two flags Grace marked unsure);
55 flags annotated resolved. Flags CSV/GPKG gained
review_status/review_comment (`review` field in GPKG). run_qa.py now
survives Windows file locks on any output (collects locked paths,
writes the rest, exits 1 listing them).

**Post-review hardening (same day, /code-review findings):** a `snap`
decision that reaches fewer than two node clusters now raises (silent
no-op on a typo'd coordinate was the failure mode); proximity fallback
is nearest-first and consumes each decision once; `review_match_radius_ft`
moved to config (50 ft); repaired-shapefile write joined the file-lock
handling; smoke tests added at `tests/test_qa_review.py` (5 passing —
Grace to read them per the testing gate). Final QA rerun identical:
1,063 flags, 56 snap_gap, 2 open review_required, 55 resolved.

---

## 2026-07-02 — "Downstream leakage" at 20.23/20.29 diagnosed: truth-polygon SiteID rotation, not a tracing bug

**Finding:** The QC comment "not including area downstream of the point — see
20.23 and 20.29" traced back to the validation truth shapefile
(`Sampling_Polygons_05212026.shp`), not to the delineation. Its `SiteID`
labels are cyclically rotated among the three 20.x sites while `Sample_ID`
(the tract number) is correct:

| polygon Sample_ID | labeled SiteID | actually belongs to |
|---|---|---|
| 20.20 | 30804 | 03442 (Tract 20.20) |
| 20.23 | 03442 | 02201 (Tract 20.23) |
| 20.29 | 02201 | 30804 (Tract 20.29) |

**Evidence:** (1) every other site's sampling point sits 0–531 ft from its
labeled polygon; these three are 7,910–18,965 ft away. (2) Each rotated
polygon *contains* the sampling point of the site it actually belongs to.
(3) Our upstream traces land inside the correctly-matched polygons: the
30804 trace buffer overlaps the Sample_ID-20.29 polygon 547 of 549 ac; the
02201 trace buffer overlaps the Sample_ID-20.23 polygon 130 of 130 ac.
Diagnostic layers: `QC/diagnostics_downstream.gpkg` (upstream + downstream
trace per site).

**Consequences:** the roadmap's "3 zero-IoU pumped sites" open question is
explained — 30804/03442/02201 scored zero because the join key (SiteID) was
rotated, not because gravity tracing fails at pumped sites. 30804 is still
genuinely a lift station, but its gravity trace lands in the right basin.

**Fix pending Grace's call** (external data — we do not silently edit the
lab's shapefile): either (a) correct the three SiteID values in the source
shapefile, or (b) switch the validation join to Sample_ID ↔ point Tract.
Either way, rerun validation — real IoU for these three sites is currently
unknown, and the sweep-tuned parameters were chosen with 3 of 24 sites
scoring a false zero.

---

## 2026-07-02 — Validation re-sweep against corrected truth: median IoU 0.64 → 0.79

**Context:** Truth set corrected twice today: (1) the three rotated SiteID
labels fixed at the source (previous entry; Grace approved editing the
shapefile), (2) Grace fixed genuine digitizing errors in the 18.06 and 20.20
truth polygons — noting the generated boundary was more accurate than her
manual delineation at those sites. Sites 02201/03442 rejoined the aggregate
(their FLAG exclusion was the label bug); 30804 remains DROP.

**Results (23 aggregate sites, full grid, output/validation_sweep.csv):**
- Table winner: morph_close, sel_r=100, close=150 — median IoU **0.7948**,
  22/23 sites ≥ 0.5.
- Fold-stable winner: morph_close, **sel_r=50, close=150** — median 0.7937,
  won 22 of 23 LOO folds. **LOO median 0.7898, optimism gap +0.005.**
- **Config set to sel_r=50 / close=150** (not the raw table winner): the
  0.0011 median difference is noise; 50 ft won 22/23 folds; and a 100 ft
  selection radius would pull in more foreign parcels — the exact defect the
  upcoming competing-pipe check addresses. Flip to 100 if the smaller radius
  underperforms on new sites. Only close_radius_ft changed (100 → 150).
- **30804 (Garrett Rd lift station) scores IoU 0.74** despite the DROP tag —
  the "gravity tracing structurally can't reproduce a pumped site" rationale
  is empirically wrong here: sampling at the wet well captures the gravity
  basin feeding it (matches the field note "captures all of 20.29").
  RECOMMENDED: un-drop 30804 next sweep, making the aggregate 24 sites.
- Weakest site: 29962 (Tract 1.02) at IoU 0.37 — consistent with Grace's QC
  comment about the 1.02/13.01 highway-ramp gap; everything else ≥ 0.66.

**Method note (defensibility):** the 18.06/20.20 truth edits corrected
digitizing mistakes verified against the pipe layer — not adjustments toward
the model output. Documented here so the IoU remains an independent metric.

---

## 2026-07-02 — Correction (Grace): lift stations at sampling points are terminal ends; 30804 un-dropped

**Decision:** All validation site exclusions removed (`DROP_SITES = set()`).
The aggregate is all 24 sites.

**Rationale (Grace's correction of a repeated AI misunderstanding):** a lift
station AT the sampling point means everything upstream of it is gravity-fed —
it is a terminal end of a gravity basin and traces like any normal point. A
pump only matters if a delineation would have to trace THROUGH it (a force
main mid-basin), which the upstream trace never does. The earlier "pumped
site, gravity trace structurally can't reproduce it" rationale (2026-07-01)
was a wrong premise, and 30804's zero IoU that seemed to confirm it was
actually the SiteID label rotation.

**Result (re-aggregated from the same sweep CSV, 24 sites):** winner is
morph_close, sel_r=50, close=150 — median IoU **0.7917**, 23/24 sites ≥ 0.5
(matching the config set earlier today, now as outright table winner).
LOO median **0.7846**, optimism gap +0.007; fold wins split 12/12 between
sel_r 50 and 100 (both close=150). 30804 itself: IoU 0.757. The only site
below 0.5 remains 29962 (Tract 1.02, 0.37).

---

## 2026-07-02 — Phases 2 & 3 signed off ✓ COMPLETE

**Decision:** Mark Phase 2 (network QA) and Phase 3 (graph builder) complete
in the roadmap, closing the long-standing open question that they were built
but never verified.

**Rationale / evidence:**
- `tests/test_phase2_phase3.py` (new): synthetic 12-pipe network with one
  planted defect per QA check (flipped pipe → cycle, tier-1 and tier-2 snap
  gaps, uphill inverts, null FROMMH/TOMH, negative slope, isolated manhole).
  Asserts every flag fires at the planted location with the right severity,
  QA_STATUS values are correct, and geometry/attributes are unmodified
  (Phase 2's flag-only contract). Phase 3 side: one edge per pipe, every
  edge oriented start→end of its geometry, tier-1 gap merged / tier-2 not,
  planted flip = the lone SCC. Both tests pass.
- Real-network check (scratchpad `verify_phase3_real.py`, full graph via
  `load_graph_from_config`): 38,359 edges = 38,359 valid pipes; **0** edges
  whose direction disagrees with geometry (within 2× repair radius); exactly
  one non-trivial SCC, size 2, the known 08373/08374 flip.
- The roadmap's original Phase 3 test criterion (invert-based direction
  inference) was superseded by the 2026-06-24 geometry-first redesign; noted
  in the roadmap so the stale criterion doesn't get re-applied.

Per the testing gate, Grace should read `tests/test_phase2_phase3.py` before
it counts as the standing regression test.

---

## 2026-07-03 — Competing-pipe check built, validated on QC sites; review batch generated

**Decision:** Competing-pipe check (QC round 1 item 2) implemented as a
radius-bounded, flag-only annotation (`population_join.competing_pipe_check`):
for each served unit, compare distance to nearest in-trace pipe vs nearest
foreign gravity main (any main not in this trace). Severity tiers:
`review_required` when a foreign pipe crosses the unit or is closer than the
in-trace pipe; `warning` when a foreign pipe is within the selection radius
but farther. Foreign pipes beyond the selection radius are ignored by
construction — they could never have selected the unit.
**Rationale:** Mirrors Grace's stated rule ("compare distance to in-shed pipes
vs any out-of-shed main; exclude or flag when a foreign pipe is closer /
crosses the parcel") with exclusion deferred: flag-only until Grace reviews a
batch, per the 2026-07-02 review-loop pattern. Tests drafted from the stated
intent (toy geometry, one parcel per rule) in `tests/test_competing_pipe.py`;
17 tests pass repo-wide.

**Result — verified against the QC-named parcels:** catches 6 of 7
still-served parcels (18.08: 225252 review / 225315 warning; Tract 14:
116991/116992/116994 review, foreign mains cross them; Tract 5: 108674
warning). The 7th, **216813, escapes** — its foreign main sits beyond the
50 ft selection radius, so by the check's own logic no network could claim
it; decision deferred (widen the foreign-search tier vs leave). The other
QC-named parcels (164674/165265, 116993/235214/156798) are no longer served
at all — the sel_r 100→50 change (2026-07-02 re-sweep) already dropped them.

**Finding — 18.06 "missing parcels" mirror case is source data, not code:**
parcels 169852/169854/169865 sit on pipes 64951–64955 (`component_141`,
6 nodes), a fragment that touches the 17863 traced network at 0.0 ft but
shares no node — its endpoint lands on a main's midspan (T-junction digitized
without splitting the main). The manual-snap feedback loop cannot fix
endpoint-to-interior; needs a pipe split in the source layer. Tract 14's
235214 sits on the neighboring fragment 64930–64960 (`component_140`,
25 nodes), 712 ft from any other pipe — genuinely unmapped connection. Both
fragments already carry unreviewed `disconnected_component` flags; they are
the highest FACILITYIDs in the layer (newest construction). → GIS-maintainer
list.

**Decision:** Review batch delivered as `run_competing_review.py` — all 24
validation sites at production config (sel_r=50, morph_close close=150),
emitting `QC/competing_pipe_review.csv` (one row per contested parcel; blank
decision/comment columns, values exclude | keep | reassign; x/y carried for
proximity fallback, matching the qa_review_decisions pattern) and
`QC/competing_pipe_review.gpkg` (contested_parcels / boundary / truth).
Uses the guarded production resolver (false-headwater guard), not the sweep's
bare nearest_node; failed/headwater sites get a status row instead of
vanishing. Per-site IoU matches the validation sweep.
**Result:** 1,325 contested parcels (613 review_required / 712 warning).
Outlier: Tract 22 at 167/399 served = 42% contested — eyeball first. Tract
1.02 (287 contested, IoU 0.37) remains the known weak site. Code review run
per the quality gate; fixes applied (config-driven output paths, shared
CP_SEVERITY / unit_id_column in polygon_output, empty-layer write guards).
Commits 4f105db, c0bd918. (Superseded 2026-07-03: batch regenerated after
midspan splits — 1,296 contested; see next entry.)

---

## 2026-07-03 — Midspan-junction pipe splitting (src/pipe_splits.py)

**Origin:** Walked through parcel 222903 (Tract 17.09, manhole 24430) with
Grace: flagged review_required because "foreign" pipe 62112 crossed it. Grace
caught that 62112 belongs to the network — it drains to MH 58048, which sits
on pipe 34720's *interior* (0.17 ft off the line, 43 ft from the nearest
endpoint). The main was digitized without being split at the tee, so
endpoint-to-endpoint snapping could never connect the lateral: same defect
class as fragments component_140/141 (logged 2026-07-03 as "needs a source
pipe split"). Grace's instruction: "If there is a MH in the middle of the
pipe, then split the pipe. For entire the city network."

**Decision:** Split receiving pipes at midspan junctions in memory at load
time (`pipe_splits.apply_midspan_splits`), wired into BOTH shared load paths
(`graph_builder.load_graph_from_config`, `network_qa.run_qa`) plus the node
layer writer — one topology everywhere. `data/` untouched (same
reproducibility contract as manual snaps); each junction emits a new
`midspan_junction` QA flag (warning, mapped) telling the maintainer to split
the pipe at the source. Config-gated: `split_midspan_junctions` (default on),
`midspan_interior_tol_ft` 2.0, `midspan_endpoint_exclusion_ft` 10.0.

**Trigger = union of two rules** (Grace's rule + the connectivity case):
a manhole on a pipe's interior, OR another pipe's endpoint terminating on a
pipe's interior (a crossing pipe passes through; an endpoint terminating
there is a tee, manhole or not). Junctions within the exclusion radius of the
pipe's own endpoints are left to the existing snap/snap_gap machinery — no
sliver segments. Citywide scan: 26 junctions on 23 pipes (16 of 18
endpoint-junctions have a manhole; 7 pipes have MH-only junctions).

**pidx stability:** the parent row keeps the first segment; extra segments
are appended as new rows with attributes copied (existing pidx references
stay valid). Appended rows are labeled `QA_STATUS="split_segment"` so the
repaired shapefile doesn't pass tool-made geometry off as source data.
Segments keep the parent's orientation (geometry-first rule preserved).

**Weld pass (from code review — the bug that mattered):** interior tolerance
(2 ft) exceeds the pass-1 snap (1 ft), so a lateral endpoint 1–2 ft off the
main would get its junction split but still not merge — split pipe, still
disconnected. Fix: after cutting, every pipe endpoint within the interior
tolerance of a cut point is moved exactly onto it. Regression test added
(`test_weld_band_endpoint_connects`). Other review fixes: node_layer writer
had bypassed the split (divergent topology); splits were silent in the
delineation path (now one summary print); `split_segment` labeling; `_fid_str`
/ `SPLIT_LOG_COLUMNS` dedup. Known acceptable: appended segments inherit the
parent's inverts/slope verbatim, so a conflicted parent flags on both halves
(invert_conflict 267 → 268) — the fields are cross-checks only.

**Results (real network):**
- 38,359 → 38,385 pipe rows; weak components 148 → 133 (fragments joined).
- 62112 in 24430's trace (541 → 568 pipes); parcel 222903 no longer contested.
- component_141 (pipes 64951–64955, the 18.06 "missing parcels" case) now in
  17863's trace — fixed WITHOUT a source-layer edit.
- **New 2-node SCC exposed, not created:** pipes 62451/62452 are a duplicate
  main digitized twice, one copy backwards; the split closed the loop that
  the unsplit geometry hid. Flagged directed_cycle (open review item), far
  southeast, touches no sample site. Same category as the known 08373/08374.
- QA rerun: 1,076 flags — midspan_junction +26, disconnected_component
  103 → 90, snap_gap 56 → 54, directed_cycle 1 → 2; 3 open review_required
  (the 2 pre-existing + the duplicate-main cycle).
- Median IoU unchanged: 0.79, 23/24 sites ≥ 0.5 (splits fix connectivity
  without moving the boundary metric). Competing batch regenerated:
  1,325 → 1,296 contested (597 review / 699 warning).

**Decision (Grace, weld vs prior review):** the weld connected 2 locations
Grace had reviewed as "no snap" (53476/53480/59316 and 00322/53297/62925) —
surfaced rather than silently overridden. Grace approved keeping the
connections: junction evidence (endpoint/MH on a pipe's interior) is stronger
than the gap-pair view her review had. Decisions file comments annotated as
superseded.

**Consequence for the review batch:** QC/competing_pipe_review.csv was
regenerated twice (post-split, post-weld); any decisions filled into the
pre-split version would have been stale. Grace had not started — no loss.

**Testing gate closed (2026-07-04):** Grace read and approved
`tests/test_pipe_splits.py` — it now counts as standing regression coverage
for the split/weld machinery.

---

## 2026-07-04 — Competing-pipe check: intersect prioritized over proximity

**Origin:** Walking parcel 183779 (Tract 16.03, manhole 25387): flagged
`warning` because foreign sibling main 50818 sat 42.6 ft away (within the 50 ft
selection radius). But an in-trace pipe runs *through* 183779 (cp_din = 0), and
50818 does not intersect it. Grace: "Intersect should be prioritized over
nearby."

**Decision:** The `warning` tier now fires only when the unit is a *marginal*
selection — no in-trace pipe intersects it (`cp_din > 0`) — and a foreign pipe
is within radius but farther. When an in-trace pipe physically intersects the
unit (`cp_din == 0`), the claim is decisive and a merely-nearby foreign pipe
no longer contests it. One-line predicate change in
`population_join.competing_pipe_check`:
`contested = cp_dout.notna() & ~review & (cp_din > 0)`.
**The `review` tier is unchanged** — a foreign pipe that crosses the unit
(`cp_cross == 1`) or is strictly closer (`cp_dout < cp_din`) still escalates to
`review_required` regardless of cp_din, because both are genuine competing
claims even when an in-trace pipe also touches.

**Rationale:** intersect-any selection means most boundary parcels are only
~16% inside the buffer, but a parcel the in-trace main actually passes through
is unambiguously served by this network. Flagging it because a sibling branch
happens to run down the next street was noise. cp_din is always finite for a
served unit (the trace is non-empty), so the guard is safe.

**Result:** batch regenerated — contested 1,296 → 1,221; warnings 699 → 624
(75 cleared), `review_required` unchanged at 597 (as designed). 183779 no
longer appears. Regression test `test_in_trace_intersecting_parcel_not_contested`
added (P_ONPIPE: in-trace crosses, foreign 45 ft, expects no flag); full suite
25 passing. Self-reviewed inline (6-line logic + docstring + test); no full
`/code-review` spawned given the surface.

---

## 2026-07-04 — Finding (Grace): RMO monitoring basin geometry appears wrong

While hand-editing the Tract 17.12 sewershed polygon (manhole 22500), Grace
observed that the city's **monitoring basin for meter RMO** (written "RMP" in the
request — read as RMO; RMP is not among the 15 RDII meters, RMO is — CONFIRM if
wrong) looks wrong where it meets the 17.12 catchment.

Significance: this is a cross-project data-quality catch, not a tool bug. RMO's
basin is **already flagged unconfirmed in the RDII project** — it is one of the
three meters (ENOR/MCO/RMO) whose crosswalk to `MonitoringBasins.shp` is
NaN-blocked, so its per-acre RDII indices can't be computed. Our geometry-first
delineation now gives independent evidence the basin polygon itself is
mis-drawn, which would explain the crosswalk trouble. Logged in the RDII
decision_log too; added to the the city Water Management notification task in
`TASKS/tasks.md`.

**Second basin (added 2026-07-04):** while editing the **Tract 16.07** polygon,
Grace observed the **monitoring basin HTP** also looks wrong. Unlike RMO, "HTP"
is NOT one of the 15 RDII meters and has no obvious meter match — recorded
verbatim, not mapped. Could be a treatment-plant / headworks basin or a
non-metered monitoring basin; needs Grace to identify it.

**Pending Grace:** (1) confirm RMP == RMO; (2) identify what HTP is; (3) pin the
specific mismatch for each (basin edge vs the corrected 17.12 / 16.07 catchment)
so the maintainer note is precise.

---

## 2026-07-04 — QC round 2: pipe-edit mechanism + boundary-overlap annotation

Grace completed `QC/QC_Review_v2.xlsx` (10 comments) and directed: fixes must
be **system-agnostic — built into the pipeline for any city, not hardcoded to
the city.** Design honored: all new logic is general code; the city specifics live
only in inputs (`config.yaml`, the QA review CSV). Schema assumption unchanged
(`FACILITYID` field on pipes + manholes).

**1. Pipe-edit feedback loop (`src/pipe_edits.py`) — flip / delete / extend.**
QC round 1 gave us `snap` (node merge); round 2 needed three more reviewer-
directed source fixes the snap can't express. Added as new decisions in the
existing `qa_review_decisions.csv` loop (qa_review.py): `flip` (reverse a
backwards pipe's geometry), `delete` (drop a stray/duplicate main), `extend`
(move a pipe's nearer endpoint to a manhole or an "x,y" so it connects).
- Applied in memory in **every** shared load path (load_graph_from_config,
  network_qa.run_qa, node_layer.write_node_layer), after midspan splits, so QA/
  traversal/delineation share one topology. Source `data/` untouched; each edit
  emits a `manual_edit` QA flag (warning, mapped) for the maintainer.
- **pidx-stable:** edits mutate the pipe's own row; `delete` empties the
  geometry (snap_endpoints skips empty geoms → no edge) rather than dropping the
  row, so positional indices — the graph edge key — stay valid. Edited rows
  tagged `QA_STATUS = manual_edit`.
- New schema: `target` column (extend only; MH FACILITYID or "x,y") and x/y now
  optional (a locator to disambiguate duplicate/null FACILITYIDs; still required
  for `snap`, which needs the gap midpoint). Config: `apply_pipe_edits` (default
  on), `pipe_edit_locate_tol_ft` (50). Tests: `tests/test_pipe_edits.py`
  (8, from intent — one per edit type + locate + schema).

**Grace's 7 QC_v2 edits applied & verified on the real network:** flip 41145 &
60075 (both reversed, no new cycles); delete 54794 & 54796 (gone from graph);
extend 12071→MH 20198, 50418→MH 53623, 64653→pipe 63249's endpoint (target as
"x,y", 3.5 ft gap) — all three formerly-disconnected fragments now join real
networks (5,937 / 1,317 / 5,937-node components). QA rerun: 1,078 flags,
`manual_edit` 7, disconnected_component 90→86, snap_gap 54→53, SCCs still the 2
known duplicate-main cycles.

**2. Boundary-overlap annotation (`cp_owner` in run_competing_review.py).**
Grace caught that the four "foreign" pipes crossing parcels 243778/239919/
102868/232487 are actually part of the **adjacent validation site's** trace —
Tract 23 ↔ Tract 10.01 catchments overlap at their shared boundary, and each
parcel is served by both. The competing check couldn't tell "another network's
main" from "the neighbouring sample site's main." Fix: the batch pre-traces all
sites, builds a FACILITYID→owning-tracts map, and labels each contested parcel's
crossing pipe with `cp_owner` — the neighbouring site it belongs to (blank =
in no sampled trace = genuinely foreign). Turns a per-pipe investigation into
"reassign to Tract X" at a glance. General: it just cross-references the traces
produced that run.

**Grace's 6 parcel decisions recorded** (competing_pipe_review.csv decision
column; durable copy here): 102868/232487 → **reassign** to Tract 23;
243778/239919 → **reassign** to Tract 10.01 (cp_owner names each); 132952
(Tract 14) & 140089 (Tract 20.29) → **exclude** (blank cp_owner — crossing main
is in no sampled trace). Note: the review CSV is regenerable, so these 6 live
here as the durable record until the consuming pass (roadmap QC item 2b) reads a
persisted decisions file.

**Answered — 2-pipe networks:** 12 two-node weak components; 23 components with
≤2 edges. Grace asked about auto-filtering them; deferred (a blanket small-
component drop risks removing real small basins) — the per-pipe `delete`
decision already generalizes, and disconnected_component already flags them.

**Batch regenerated:** 1,215 contested (591 review / 624 warning), cp_owner
populated. Full suite 34 passing.

**Code review (2-axis) — two real bugs found & fixed:**
- *cp_owner mislabel on duplicate FACILITYID.* The owner map keyed tracts by
  FACILITYID, which is null/non-unique here — a different pipe sharing an id
  could credit the wrong tract. Fixed: `competing_pipe_check` now returns
  `cp_fpidx` (the foreign pipe's positional index), and the owner map keys by
  pidx (exact). Regression test `test_cp_fpidx_is_the_foreign_pipes_position`.
- *flip left invert/slope inconsistent → considered swapping them.* First fix
  swapped UPSTREAMIN↔DOWNSTREAM on flip; testing on 60075 (UPSTREAMIN 363.48 >
  DOWNSTREAM 358.93, originally consistent) showed the swap *invented* a
  physically-implausible invert_conflict (water climbing). Reverted to
  **geometry-only flip**: the invert check reads columns by name, so a
  geometry-only flip never fabricates a conflict, whereas swapping can. Inverts
  stay as source data (geometry-first; maintainer reconciles, manual_edit flags
  it). Verified 60075 no longer invert-flagged after the revert.
- Minor: deduped the empty-result early return (`_no_edits`). Standards axis
  confirmed no the city values hardcoded in code — the system-agnostic directive
  is met.

**IoU re-verified vs edited truth (2026-07-04):** after Grace's truth-polygon
edits + the topology fixes, single-config score (sel_r=50/close=150) is **median
IoU 0.8092, 23/24 ≥ 0.5** (17.12 = 0.846; weakest 29962 = 0.37). Single-config,
NOT a fresh sweep/LOO — quotable tuned number stays the earlier LOO ~0.78 until
a re-sweep is run. Full verification this session covered: 34 tests + QA
deliverable + competing batch + real-network edit check, all on committed
`b06f5a8`.

**Project map** regenerated at `docs/SewershedDelineation_project_map.html`
(render-verified: 20 nodes, no Mermaid error). Ph1–6/8 done; new load-time
repair layer (splits/edits/snaps) done; **Ph7 multi-site `run.py` is the one
open pipeline gap** (partial — loop exists across runners, not unified).

---

## 2026-07-06 — Border/inner parcel labeling + auto-exclude rule (Grace)

**Origin:** Walking parcel 196966 (Tract 10.02, manhole 30659), flagged
`review_required`: no in-trace pipe touches it (`cp_din` = 31.9 ft — selected only
because an in-trace main clipped its 50 ft buffer) but foreign pipe 51960 runs
straight through it (`cp_cross` = 1). Graph check confirmed 51960 sits in the
same connected network but drains to a *different* outlet (its flow never reaches
30659's target node), so it is a neighbouring sub-basin's main, not a fragment to
connect. 196966 belongs to the neighbour → should be excluded.

**Grace's rule:** Label each served unit **border** vs **inner**. Inner units get
checked for fragments (dangling 2–3 pipe networks to connect); border units get a
different test — *a border unit that does NOT touch the trace pipe but IS crossed
by a foreign pipe should not be in the sewershed.* Auto-exclude those.

**Why the border/inner split is load-bearing:** the same signal (foreign pipe
crosses a unit no in-trace pipe touches) means opposite things by position. On a
**border** unit it means the unit belongs to a neighbouring network → drop it. On
an **inner** unit it means a foreign fragment passes through → *connect* the
fragment (split/snap/extend), never carve a donut hole in the sewershed. Applying
the exclude everywhere would punch holes; the label prevents that.

**Decision (implementation):**
- `population_join.competing_pipe_check` gained an optional `footprint` polygon
  arg and two columns: `cp_pos` ("border"/"inner") and `cp_excl` (0/1).
- **Border/inner is judged against the CLOSED morphological-close footprint**, not
  the raw parcel union. Closing is extensive (footprint ⊇ every served unit), so a
  unit is **inner** iff `served.within(footprint)` (strictly interior) and
  **border** iff its edge reaches the perimeter. This is essential: parcels don't
  tile (streets/ROW between them), so against the raw union ~every unit reads as
  edge (2,351 border / 1 inner at 30659 — useless). Against the closed footprint:
  133 border / 2,219 inner. **No tuning parameter** — plain `within`, honouring
  Grace's "no tolerance."
- **Auto-exclude:** `cp_excl = (cp_pos == "border") & (cp_din > 0) & (cp_cross == 1)`.
  Strict `cp_din > 0` (no tolerance, Grace's call). Foreign-*near*-not-crossing
  border units are left as review flags (Grace reviews those separately).
- `run_competing_review.py` builds the full footprint once (it both scores the
  pre-exclude IoU and defines the border label), drops `cp_excl == 1` units,
  rebuilds the boundary, and reports IoU before→after. Auto-excluded units are
  written to `QC/competing_pipe_review.csv` with `decision` pre-filled `exclude`
  and a comment (Grace audits/overrides rather than re-deciding each).
- **Auto-excluded units are dropped from the `contested_parcels` GeoPackage
  layer** (Grace, 2026-07-06) — they stay in the CSV as the audit record, but the
  spatial layer is trimmed to the OPEN review parcels only (1,165 of 1,215), so
  the GIS layer shows just what still needs a look. Otherwise an already-decided
  exclude looked identical in GIS to an open flag.

**Result (24 sites, sel_r=50 / morph_close close=150):**
- 50 parcels auto-excluded; **median IoU 0.8092 → 0.8439 (+0.035)**, 23/24 ≥ 0.5.
- **No site regressed** — every affected site improved or held flat (30659
  0.76→0.85, Tract 5 0.78→0.87, Tract 3.01 0.87→0.92). Monotonic improvement is
  the empirical validation that the rule only removes neighbour-owned units.
- Weakest site 29962 (Tract 1.02) unchanged at 0.37 — its problem is the
  highway-ramp gap, not border over-inclusion, as expected.
- 233 contested units are **inner** (foreign-crossed but protected) — the fragment
  cases Grace will investigate separately.

**Tests (from Grace's stated rule):** 4 added to `tests/test_competing_pipe.py`
(border+cross+no-touch → excluded; inner+cross → protected; in-trace-crossing
border cp_din==0 → not excluded; no footprint → all inner, nothing excludes).
Full suite 38 passing. Testing gate: Grace to read the 4 new tests before they
count as standing regression coverage.

**Still flag-only for the review batch:** the exclude is applied inside
`run_competing_review.py` to show the IoU effect, but the production delineation
path (`run_polygon_output.py`) does not yet consume `cp_excl` — that is the
consuming pass (roadmap QC item 2b), still pending a persisted decisions file.

---

## 2026-07-06 — Multipart explode + equidistant split of border-contested parcels

Session also added (earlier, same day): border/inner labeling + auto-exclude
(196966), dangling-network ignore (≤3-pipe stubs, config
`competing_ignore_dangling_max_pipes`; test parcel 138509), pipe `delete` of
overflow 33250, and the 1.02+CB1 truth-polygon merge (see the truth-file note
below). This entry covers the two parcel-geometry features Grace approved after
the split prototype.

**Feature 1 — explode multipart parcels into parts** (`population_join._explode_to_parts`,
called from `load_units`, config `explode_multipart_units` default on). A
multipart parcel is one assessor record digitized as several disjoint polygons;
served whole, ONE part touching a foreign main flagged the entire record, and a
part beyond the selection radius rode in on its siblings. Exploding makes each
part an independent selection/contest unit. Unique `PARTID = <ALTPARNO>#<n>`;
original ALTPARNO kept for the deferred demographic join; `unit_id_column` now
prefers PARTID (ALTPARNO/GEOID20 are non-unique across parts — every id-keyed
site had to switch or parts would collide). Validated on 137546 (4 parts):
parts 0/1 clean-kept, part 2 contested, **part 3 dropped** (no in-trace pipe
within the selection radius — over-inclusion fix, free).

**Feature 2 — equidistant split of border-contested parcels**
(`population_join.split_border_contested` + `_equidistant_keep`). For a unit that
is **border AND cp_cross==1 AND cp_din==0** (an in-trace pipe AND a foreign pipe
both cross it — Grace's "intersected by foreign and trace pipes"), the geometry
is replaced by the sub-area closer to an in-trace pipe than any foreign pipe, via
a **Voronoi partition of densified pipe points** (the equidistant line is the
cut). Config `split_near_radius_ft` (100), `split_densify_step_ft` (3). Empty
keep → the unit is dropped. Adds `cp_keep` (fraction retained).

**Decision (Option A, advisor-caught):** split condition requires `cp_din==0`.
The naive `border AND cross` would have swept in 196966 (border, cp_din=31.9,
foreign-only) and kept ~13% of it — silently overriding its signed-off full
**exclude**. So the two mechanisms are mutually exclusive: `cp_din>0` → full
exclude (`cp_excl`, 196966 → 0%); `cp_din==0` → split (137546 part 2 → 46%);
inner → never split. Verified 196966 stays fully excluded, 137546#2 splits 46/54.

**Split parcels drop from the `contested_parcels` gpkg layer** (Grace) — like
auto-excludes, they stay in the CSV (decision pre-filled `split`, `cp_keep` +
comment) but leave the GIS layer, which now shows only OPEN review parcels.

**Results (24 sites, sel_r=50 / close=150, vs Grace's tweaked 07062026 truth):**
- 42 units split, 49 auto-excluded. **Median IoU 0.8135 → 0.8538 after
  exclude+split; 24/24 sites ≥ 0.5** (best yet). Big movers: 3.02 0.83→0.91,
  17.05 0.84→0.89, 5 0.78→0.87.
- **NOT monotonic** (unlike the border-exclude): the split is geometry-based, not
  truth-validated, so it can trim real area. 4 sites dipped slightly — 20.29
  0.76→0.74 (5 splits), 20.20 0.69→0.68, 3.01 0.87→0.86, 18.01 0.94→0.93. Net
  median still up; watch 20.29. Prototypes: `output/proto_split_140112.gpkg`,
  `output/proto_split_137546.gpkg` (+ PNGs in `output/preview_png/`).

**Demographic cost (deferred, but real):** a split/part unit no longer maps 1:1
to an assessor/ACS record — its population must be area-apportioned from the
parent parcel (uniform-density assumption). Blocks nothing geometric now; the
thing to resolve before the demographic join.

**Tests:** 4 added to `tests/test_competing_pipe.py` (explode → unique PARTID +
kept ALTPARNO; border+both-cross → split ~50%; inner → never split; border+din>0
→ not split). Full suite 42 passing. Testing gate: Grace to read the 4 new tests.

---

## 2026-07-06 — Truth-polygon file: CB1 merge into 1.02, archive, and a near-miss

**CB1 merge:** Grace directed merging monitoring basin **CB1** (from
`RDII/.../MonitoringBasins.shp`, col `MONITORBAS`) into the Tract 1.02 truth
polygon (SiteID 29962) to correct an under-drawn catchment. Old truth archived to
`CommunityWastewaterDashboard/.../archive/07062026/Sampling_Polygons_05212026.*`;
new active file `Sampling_Polygons_07062026.shp` (config `validation_truth_polygons`
repointed). 1.02 truth area ~101.7M → ~203.5M ft². Result: **1.02 IoU 0.37 → 0.68+**
— it is no longer the weak site.

**Near-miss (process lesson):** a subagent's write of the new file silently
corrupted 3 non-target polygons (17.12 destroyed −99.5%, 30804/31067 shifted);
its "23 geometries identical" self-verification was false. Caught by an
independent per-row area diff after 17.12 scored IoU 0.00. Compounded by MY error:
I then did a full rebuild from the archived baseline, which reverted Grace's
concurrent ArcGIS edits. Recovered from Grace's `-temp.shp` save.
**Lesson:** Grace edits truth in ArcGIS concurrently — treat any detected diff as
HER edit until told otherwise; never wholesale-rebuild an external data file;
surgical row swaps only; always independently verify a subagent's data-write
claims (per-feature, not aggregate).
