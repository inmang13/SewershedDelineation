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

### Phase 5 — Population unit assignment `src/population_join.py`
Buffer upstream pipes by `pipe_buffer_distance_ft`, spatial join against population units.
Return served units + boundary units (partially inside/outside buffer).

Test: compare against a manually delineated sewershed for one of the 25 existing sites.

---

### Phase 6 — Polygon construction + output `src/polygon_output.py`
Dissolve served population units → write `output/sewershed.shp`.
Write `output/debug_upstream_pipes.shp` for review.

Flags at this stage (delineation-level, not network-level):
| Flag | Trigger | Severity |
|---|---|---|
| `boundary_parcel` | Parcel straddles pipe buffer edge | `warning` |
| `large_catchment` | Polygon exceeds area threshold | `warning` |
| `no_upstream_found` | Traversal returns zero upstream edges | `review_required` |
| `low_population_match` | Upstream pipes cover area but few parcels matched | `warning` |

These are appended to `output/flags.csv` and included in `output/flag_maps.pdf`.

Test: load output in GIS, overlay with pipes and parcels, visually inspect.

---

### Phase 7 — Main script integration `run.py`
Wire all phases. Read config → QA/repair → build graph → traverse → assign units → polygon → flags.
CLI: `python run.py --config config.yaml`
Option: `python run.py --config config.yaml --qa-only` to run just the network QA without delineating.

---

## Deferred / Future

- **Socioeconomic stats:** Join output polygon to ACS census data (race, income, poverty).
  Library candidates: `censusdatadownloader`, direct Census API.
- **Multiple manholes per run:** Loop over a list of IDs, one output shapefile per manhole.
- **Interactive UI:** Streamlit wrapper. Far future.
- **Multi-state support:** Promote CRS to required config field if project expands beyond NC.

---

## Open Questions

- After QA/repair: what fraction of the 38,359 pipes end up flagged vs. repaired?
  Will inform whether manual cleanup is needed before delineation is reliable.
- Does the GIS maintainer want the repaired shapefile in a specific format or with
  specific field names to match their existing schema?
