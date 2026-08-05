# Force-main integration plan (draft — 2026-07-30)

## Context

The pipeline is gravity-only. `docs/roadmap.md` item 7 and the 2026-07-02 decision-log
correction already named the real gap: not "a pumped target gives a wrong polygon"
(that was retracted), but **a trace that must cross a force main mid-basin stops at
the discharge point**, so every subbasin feeding a lift station is silently missing
from the sewershed. the city's `snForceMain` layer (received 2026-07-30, now in
`sewershed-lab/data/snForceMain/`) is the missing piece.

This is the network-crossing detector, implemented — not "add a layer".

## What the data actually is

`Publicworks_PUBLICWORKS_snForceMain.shp` — 1101 LineStrings, NAD83 / NC State Plane
ftUS (matches `parameters.crs` EPSG:2264, no reprojection surprise), 67.8 total miles.

| Field | Notes |
|---|---|
| `FACILITYID` | unique, no nulls, no dups |
| `TYPE` | Force 1089, Pressure 2, Bridge 2, null 8 |
| `LIFECYCLES` | Active 1092, **Inactive 8**, null 1 |
| `OWNER` | DNC 862, **PVT 119**, PEND 114, DNC-PC 4, OWASA 1 |
| `DIAMETER` | 2–42 in; **109 features ≤3 in**; **15 features = 0** |
| `MONITORBAS` | 40 basins, 31 null — possible cross-check on basin assignment |
| geometry | all have Z, but **Z is 0.0 for 6297/6303 vertices — unusable** |

No `FROMMH`/`TOMH`, no invert elevations, no pump-station layer shipped.
Segment lengths: median 38 ft, 25th pct 7 ft, **214 segments under 5 ft**.

## Errors and gaps found (the "bring problems to my attention" half)

1. **The force-main layer is not snapped to the gravity network.** Only
   **187 / 2202** force-main endpoints sit within 1 ft of a gravity-main endpoint;
   at 10 ft it is 316. Concatenating the layer into `pipes` at the current
   `node_snap_tolerance_ft: 1.0` would produce a disconnected island and change
   nothing except QC noise. An explicit force-main↔gravity junction step with its
   own tolerance is required.
2. **Force-main internal topology is decent but not clean.** 2202 endpoints collapse
   to 1223 clusters at 1 ft, forming **147 connected components**; 65 are single
   pipes. 354 component termini.
3. **39 components (of 147) touch the gravity network nowhere within 10 ft.**
   Some are private laterals; some are genuine snap gaps. These need eyes.
4. **Terminus-to-gravity distance has no natural cutoff.** Connected termini:
   157 @ 5 ft → 170 @ 10 → 190 @ 15 → 225 @ 25 → 256 @ 50 → 283 @ 100. Smooth,
   no cliff — so the tolerance is a judgment call, and every accepted junction
   must be logged with its distance.
5. **No usable direction source.** Geometry direction is unverified for pressurized
   mains, there is no FROMMH/TOMH, and Z is zeros. Direction must be derived
   (see below) or reviewed.
6. 8 Inactive + 2 Bridge features, 15 zero-diameter records, 214 sub-5-ft slivers.

## Direction rule (validated, not assumed)

Tested against the existing directed gravity graph (`load_graph_from_config`,
38417 nodes / 38382 edges). Claim: a pump station wet well is where the gravity
network *ends* (`out_degree == 0`), and a force-main discharge manhole is a gravity
node that *continues downstream* (`out_degree > 0`) — often a `start_only`
headwater that "starts from nowhere" precisely because a force main feeds it.

Result on the 45 components with two gravity-touching termini:

- **35 / 45** resolve cleanly — exactly one terminus on a terminal sink
  (`end_only`, in>0/out==0) and the other on a node with downstream gravity.
- 7 have both termini with downstream gravity (ambiguous).
- 3 have both on terminal sinks (ambiguous).

So: **the rule holds for ~78% and fails loudly, not silently, on the rest.**
Adopt it as primary; route the residual to human review. Z is dead — do not use it.

Free cross-check already owned: `graph_builder.summarize_graph`'s no-directed-cycles
invariant. A correctly directed force main (wet well → discharge) adds no cycle; a
flipped one usually creates an SCC > 1. Every SCC > 1 after insertion is a
mis-directed force main.

### Ground-truth spot check (partial, and it raises a data request)

`Sampling_Locations_05212026.shp` has a `LiftStatio` column naming four stations —
the only independent anchor available. Result at 10 ft:

| Station | Asset | Gravity node role | Nearest FM terminus |
|---|---|---|---|
| East End P.S | PS103 | `end_only` (in 1 / out 0) | 32 ft → **classified wetwell ✓** |
| Heritage Drive P.S. | 22942 | `junction` (1/1) | 53 ft |
| Lutravil Pump Station | 26532 | `junction` (1/1) | 158 ft |
| Garret Rd Lift Station | 30804 | `junction` (3/1) | 77 ft |

The one case where the sampling point coincides with a force-main terminus confirms
the rule. The other three sampling points are 53–158 ft from any force-main end, so
they neither confirm nor refute it — the sampling manhole is not the wet well.

**Implication and open data request:** a force main begins at the pump discharge
flange, which is a *station*, not a manhole, so the suction-side terminus is often
not represented in either the gravity-main or manhole layer at all. the city almost
certainly maintains a pump/lift-station point layer (`snPumpStation` or similar in
the same `Publicworks_PUBLICWORKS_*` family). **Requesting it would replace the
inferred wet-well rule with a direct one** and is the single highest-value thing to
ask for. Build without it, but ask.

## Design

### Core invariant to preserve
`node_layer.snap_endpoints` owns "one topology everywhere" — the node layer, the
graph, QA, and traversal all share it. Force mains go **through** that machinery
(as extra rows in `pipes` plus pass-3 `manual_snaps`), never around it.

### The one thing that will silently break if ignored
**Force mains are traversable but not bufferable.** A gravity main collects along
its length; a pressurized main collects nothing. `TraversalResult.pidx_list` is
documented as "the entire handoff to Phase 5" — if force-main `pidx` values flow
through it, Phase 5 buffers a parcel corridor along miles of transmission main that
drains nowhere near it, inflating every polygon downstream of a discharge.
Fix: tag edges `is_force=True`; add `TraversalResult.gravity_pidx_list` for
buffering and keep `pidx_list` as the full traced set. Phase 5 consumes the former.

### Three things that will bite if not handled explicitly

- **`FACILITYID` collides across layers — 218 exact duplicates** between
  `gravity_mains` and `snForceMain` (both use the same 5-digit zero-padded
  namespace). `build_graph` writes `facilityid` onto every edge, and `flags.csv`,
  `qc_flags.gpkg`, the review CSVs, and the app's lookup all key on that string.
  Add a `layer` column to the concatenated frame and namespace the edge attribute
  (`FM:00018`) so a force main can never be mistaken for a gravity main.
- **Component-level direction must propagate to individual pipes.** The largest
  component has 210 nodes; knowing which terminus is the wet well says nothing
  about the ~100 edges between them. Orient by BFS from the discharge terminus,
  pointing every edge back toward it. Caveat: the force-main graph is not a forest —
  E − V + C = 1101 − 1223 + 147 = **25 independent cycles**. Recount after the
  inclusion filter and sliver drop; components with a surviving real loop go to
  review rather than letting BFS pick arbitrarily.

  **Corrected 2026-07-30 (as built).** An earlier draft of this line also sent
  components with *three or more gravity-touching termini* to review. That was
  wrong — it conflated "three termini" with "three discharges". Two wet wells
  pumping to one discharge is an ordinary configuration (comp 18 in the delivered
  output is exactly that), and BFS from the single discharge orients it correctly.
  What is undecidable is **more than one discharge**, which `multi_discharge`
  already catches. The verdict rule tests discharge count, not terminus count.
- **Force mains break gravity-semantics QA.** They ship no `SLOPE`, `UPSTREAMIN`,
  or `DOWNSTREAM`, so `_num()` returns NaN on every row; 214 sub-5-ft segments and
  unsnapped termini would also flood `snap_gap`. Every gravity-only check in
  `network_qa.py` and `validation.py` filters on `is_force == False`; force mains
  get their own flag types (`fm_no_gravity_contact`, `fm_direction_ambiguous`,
  `fm_junction_candidate`).

### New module: `src/force_mains.py`
1. `load_force_mains(cfg)` — read, reproject, apply the inclusion filter, strip Z.
2. `build_fm_components(fm, tol)` — union-find on endpoints (reuse `node_layer`'s
   `_make_uf/_find/_union`), return components and termini.
3. `classify_termini(components, G, node_index, tol)` — nearest gravity node +
   `(role, in_degree, out_degree)` per terminus; assign
   `discharge` / `wetwell` / `ambiguous` / `no_gravity_contact`.
4. `force_main_junctions(...)` — emit one row per proposed junction:
   `fm_facilityid, end, x, y, gravity_node, dist_ft, gravity_role, classification,
   decision`. Written to `QC/force_main_review.csv`, mirroring the existing
   `QC/qa_review_decisions.csv` contract so `qa_review.manual_snaps_from_config`
   consumes accepted rows as pass-3 manual snaps.

### Wiring into `graph_builder.load_graph_from_config`
Insert after `apply_pipe_edits`, before `build_graph`:
- if `inputs.force_main_shapefile` is absent → unchanged gravity-only behaviour
  (this is how the public repo runs and how IoU 0.875 was earned — it must stay valid);
- else concatenate filtered force mains onto `pipes` with `is_force=True`
  (gravity rows `is_force=False`), reversing geometry where the direction rule says
  the discharge is at the geometry start;
- pass the accepted junctions in via the existing `manual_snaps` argument;
- carry `is_force` onto each edge in `build_graph`;
- report ambiguous / unconnected components as QA flags alongside `snap_gap`.

### Config additions (both repos)
```yaml
inputs:
  force_main_shapefile: "data/force_mains.gpkg"   # omit → gravity-only
parameters:
  force_main_snap_tolerance_ft: 10.0    # FM terminus → gravity node (auto-accept)
  force_main_review_radius_ft: 500.0    # candidates logged for review, not accepted
  force_main_component_tol_ft: 1.0      # FM-to-FM endpoint clustering
  force_main_include:
    lifecycle: ["Active"]
    exclude_owner: ["PVT"]              # private grinder-pump laterals
    min_diameter_in: 4
QC:
  force_main_review: "QC/force_main_review.csv"
outputs:
  force_main_junctions: "output/force_main_junctions.gpkg"
```

### Data hygiene
Convert the shapefile to `data/force_mains.gpkg` to match the other `data/` layers.
That drops the four ArcGIS `.sr.lock` files and the 766 KB `.shp.xml`. Confirm
`.gitignore` covers `data/`. Data stays in `sewershed-lab` only; code lands in
`SewershedDelineation/src` and is vendored over (the two `src/` trees are identical
except `candidate_screen.py`).

## Verification (acceptance gate)

1. **Unit** — synthetic 2-component fixture in `tests/`: known wet well, known
   discharge, one deliberately reversed geometry. Assert direction rule recovers both.
2. **Graph invariant** — `summarize_graph` after insertion: SCC > 1 count must be 0.
   Any cycle is printed with its force-main FACILITYID.
3. **Buffer isolation** — assert no force-main `pidx` reaches Phase 5 for a site
   whose trace crosses a discharge.
4. **IoU regression — diagnostic, not pass/fail.** The gate has real signal:
   with the chosen filter (945 force mains, 78 components) and 10 ft snapping,
   **57 discharge nodes** land in the graph and **9 of the 25 validation sites have
   at least one discharge node in their upstream reachable set** — Tracts 16.04,
   17.12, 18.02_old, 18.06, 20.29, 20.20, 19, 1.02, 18.02. Those nine are the ones
   that can move; the other 16 must be byte-identical, and a change there is a bug.

   Treat a drop as a question, not a failure: the expert truth polygons were very
   likely drawn gravity-only, so a *correct* force-main trace can legitimately lower
   IoU. For every site that drops, inspect whether the added area is a real pumped
   subbasin or a false junction, and record the finding. Report per-site deltas
   against the current 0.875 median / 0.863 LOO. Watch **30804 (Garrett Rd, Tract
   20.29)** — currently 0.74 and now confirmed to have 2 discharge nodes upstream.
5. **Smoke** — `python run.py --config config.yaml --sites 27508,09289`, then the
   Streamlit app.

## Decisions (Grace, 2026-07-30)

- **Snap tolerance: 10 ft, conservative.** Auto-accept junctions ≤10 ft
  (56 / 147 components usable out of the box). Every remaining candidate out to
  500 ft is written to `QC/force_main_review.csv` with its distance and
  classification, unaccepted, for manual approval via the existing pass-3
  `manual_snaps` route. Consequence to expect: the first run will move few
  sewersheds; most of the value arrives after a review pass.
- **Inclusion: Active + public + ≥4 in.** Drop 8 `LIFECYCLES=Inactive`, drop
  `OWNER=PVT` (119), drop `DIAMETER ≤ 3` (109). The 15 `DIAMETER=0` records fail
  `min_diameter_in` and are excluded — they will be listed in the run log rather
  than dropped silently, since a zero is missing data, not a small pipe.
- **Buffer: traverse but never buffer.** `is_force` edges join the graph and carry
  the trace; Phase 5 consumes `gravity_pidx_list` only.
- **Repo split: code public, gravity-only default.** Methodology, tests, and config
  keys land in `SewershedDelineation/src` and are vendored to `sewershed-lab`.
  The public `config.yaml` omits `force_main_shapefile`, so the public repo runs
  exactly as today. The converted `data/force_mains.gpkg` exists only in the lab repo.

## Build order

0. **Ask the city for the pump/lift-station point layer** (see the spot-check section).
   Not a blocker, but it arrives while steps 1–2 are being built and would replace
   the inferred wet-well rule with a direct one.
1. ~~`data/force_mains.gpkg` conversion + inclusion filter~~ **DONE 2026-07-30.**
   1101 read, 945 kept (53.9 mi); dropped 9 non-Active, 118 `OWNER=PVT`,
   9 `DIAMETER=0`, 20 under 4 in.
2. ~~`src/force_mains.py` + `run_force_mains.py`~~ **DONE 2026-07-30.**
   78 components, 210 termini. Outputs `QC/force_main_review.csv` (210 rows, every
   decision blank) and `output/force_main_junctions.gpkg`. No topology changed.
   `tests/test_force_mains.py`, 14 tests; full suite 118 passing.

   **Result — read this before reviewing.** 31 of 78 components resolve, but that is
   only **19.5 of 53.9 miles (36% of length)**. The resolved set skews small and
   simple; the big pumped systems are concentrated in the unresolved buckets —
   `cyclic` alone is 7 components holding **31% of the network's length**, led by
   comp 8 at 173 pipes / 14.0 mi. Three of the four stations named in
   `Sampling_Locations.LiftStatio` land in unresolved components. Review by length,
   not by row order; the runner prints the five longest unresolved components.
3. Review pass with Grace on the flagged components (39 with no gravity contact,
   10 ambiguous-direction, and the 10–500 ft candidates).
4. `graph_builder` wiring behind the optional config key + `is_force` edge tag.
5. Traversal `gravity_pidx_list` split and the Phase 5 consumer change.
6. Tests (unit fixture, SCC invariant, buffer isolation) and the IoU regression gate.
