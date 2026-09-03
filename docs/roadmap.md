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

### Phase 7 — Main script integration `run.py` ✓ COMPLETE (2026-07-12)

**Built as the multi-site production spine** (roadmap P2-5). One command runs the shipped
full-rules delineation for one site or a whole list: per site `trace → membership →
competing exclude/split/buffer-assign → delaunay → fill_uncovered`, then a single cross-site
pass `align_seams → resolve_overlaps(opt) → bridge_parts`, then writes
`output/sewershed_final.gpkg:boundary` (the layer `run_demographics.py` consumes) + `flags.csv`
+ `qc_flags.gpkg` in one pass. **Subsumes the old two-step `run_competing_review.py` →
`run_seam_align.py` production chain.**

**Equivalence-gated (2026-07-12): run.py reproduces the committed `sewershed_final.gpkg`
24 boundaries geometrically identical (worst symmetric-difference 0.0000 ft²)** — so the
validated median IoU 0.875 / 0.863 LOO is provably untouched. Thin reuse of the existing
phase functions; the per-site loop is a faithful replica of `run_competing_review.py`'s
(extraction into a shared `delineate_site()` deferred to a follow-up PR — see decision_log
2026-07-12).

CLI:
- `python run.py --config config.yaml` — single site from `inputs.manhole_id` / `manhole_coordinate`
- `python run.py --config config.yaml --sites 17506,09289` — explicit FACILITYIDs
- `python run.py --config config.yaml --sites-file sites.csv` — CSV (x/y coords or an id column)
- `python run.py --config config.yaml --qa-only` — network QA only (delegates to `run_qa.py`)

---

### Phase 8 — service-area matching pipeline + QC spatial output + basemaps

**Status (2026-07-02): COMPLETE & validated.** Floor set (0.75 / 20); winner **morph_close
sel_r=100 close=150, median IoU 0.7948, 20/21 aggregate sites ≥ 0.5**; `validation_overlay.gpkg`
written. **Leave-one-out validated: LOO median 0.7898 (optimism gap +0.005), fold-stable** — quote
"median IoU 0.79 (LOO 0.79), 21 gravity-tractable sites." Census-block methods lost (overshoot).
See decision_log 2026-07-02 and `docs/SewershedDelineation_checkin_2026-07-01.html`.

**Re-check 2026-07-04:** after Grace's truth-polygon edits + the topology fixes (midspan splits,
7 pipe edits), single-config score at sel_r=50/close=150 rose to **median IoU 0.8092, 23/24 ≥ 0.5**
(17.12 = 0.846; weakest still 29962/Tract 1.02 = 0.37). NOTE: this is a single-config score, NOT a
fresh sweep/LOO — the defensible generalization number is still the earlier LOO ~0.78. **Re-sweep +
LOO against the edited truth is pending** if a quotable tuned number is needed.

**Re-check 2026-07-06:** competing-pipe membership overhaul — multipart **explode**, **equidistant
split** of border+both-cross parcels, **border/inner surround test** (fixed the closed-footprint
moat), and **buffered-pipe area assignment** of the remaining contested parcels (aggregate in-trace
vs foreign buffer area → keep/exclude); plus dangling-≤3 ignore, 33250 delete, and the **1.02+CB1
truth merge** (1.02 now 0.73, no longer the weak site). Single-config score climbed
**0.8135 → 0.8622 after all rules, 24/24 sites ≥ 0.5** (first run with every site ≥ 0.5). Border
auto-excluded 56, split 48, buffer-assigned 665 keep / 437 exclude. Split + buffer-exclude are
geometry-based (not truth-validated) so NOT monotonic — a few sites dip ≤0.02; net median up. See
decision_log 2026-07-06. **Re-sweep + LOO against the 07062026 truth still pending** for a quotable
tuned number.

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

Data: census blocks supplied at `data/census_blocks/tl_2021_37_tabblock20.shp` (statewide NC TIGER
2020, EPSG:4269; sibling `nhgis0002_csv/` is demographics for the deferred join).

---

### Phase 9 — open release, demographics-inclusive (rescoped 2026-07-12)

**Goal (decision_log 2026-07-12, supersedes 2026-07-07):** a clean, documented, **JOSS-ready**
GitHub repo with an archived **Zenodo DOI** on the tagged release. The software is the **WBE
end-to-end pipeline: trace → catchment → demographics** (the demographic join was built
2026-07-10, after the original plan — its absence was staleness, not a decision; it is the
project's founding purpose). The *validated* contribution stays the delineation core (median IoU
0.875 in-sample / 0.863 LOO vs expert manual delineation); the demographic join ships as the
honestly-caveated applied step — **its outputs are estimates and unvalidated** (no ground truth
for a catchment's demographics exists). NOT a multi-city journal paper; multi-city = future work.

**JOSS submission itself is OPTIONAL and deferred:** citability comes from the Zenodo DOI;
"JOSS-ready" = repo hygiene worth doing regardless. Decide on actual submission after release
(matters mainly if the peer-reviewed line is wanted for the thesis/RDII paper).

**Figures policy (Grace, 2026-07-12):** de-identified screenshots of real results are allowed in
docs/writeup — **no basemap, no Asset/manhole IDs, no data files online**. The toy network remains
the *runnable* demo; screenshots show, toy runs.

**Fan-out (tracks by dependency; critical path B → E):**

**Track A — Repo hygiene (partially done).**
- [x] `requirements.txt` + `README.md` — DONE 2026-07-12 (commit 97d1e25).
- [x] `.gitignore` cleanup — DONE 2026-07-12 (6c6be56).
- [ ] `LICENSE` — **MIT** (default; Grace to confirm — OSI license is a JOSS requirement).
- [ ] `pyproject.toml`.
- [ ] `data/README.md` — provenance/CRS/fields per input (Grace supplies sources/dates).

**Track F — Tests + CI. ✓ DONE 2026-07-12** (commit b817cfa): 14 intent-based tests
(traversal, boundary, run.py; 60 total green) + GitHub Actions pytest on push. CI unverified
until first push (geo-stack install on ubuntu-latest). Demographics tests moved to Track G.

**Track C — Unified `run.py` spine. ✓ DONE 2026-07-12** (commit d34ead4, equivalence-gated:
24/24 boundaries identical to the validated output, worst symdiff 0.0000 ft²).

**Track B — Runnable example. ✓ DONE 2026-07-26.** `examples/toy/` — 12-pipe synthetic tree
(13 manholes, 484 gridded parcels), committed as GeoPackages plus the deterministic
`make_toy_data.py` that generates them. `python run.py --config examples/toy/config.yaml`
→ 12 pipes / 124 parcels / 120.8 acres. Config carries the **shipped production values for
every parameter that affects the delineation** (two documented departures, neither
geometric: `basemap_style` street-not-satellite, and the validation-only `iou_floor_*` keys
omitted) — nothing tuned to flatter the demo; the network is spaced at 1000 ft so branch gaps clear
`delaunay_max_edge_ft` (500) and the boundary follows the network instead of blobbing to the
convex hull. `tests/test_toy_example.py` (12 tests) asserts hand-counted upstream oracles
(MH01→12, MH03→10, MH04→6, MH08→0 headwater) and is the only test running the full `run.py`
path against files on disk. Delineation-only by design — **no demographics** (needs real FIPS
geography + Census key; Grace's call 2026-07-12, walkthrough covers it instead).
- **Found and fixed a live bug — a NEW instance of the P3-10 *family*, not one of the four
  defects P3-10 lists; P3-10 itself stays open.** `run.py` prefixed `_base_dir` onto
  `outputs.flags_report` / `qc_flags_gpkg`, which `load_config` had **already** resolved —
  writing to `<base>/<base>/output/…`. Invisible for every config at the repo root (base `.`)
  and for absolute config paths (`Path(a) / abs` discards `a`), so only a *relative* config in
  a subdirectory exposes it. That is exactly how the README invokes the toy. Regression test
  verified RED against the pre-fix code. Checked 2026-07-26: the other runners do **not**
  share this particular defect (`run_qa.py` already used the resolved values;
  `run_competing_review.py` / `run_demographics.py` prefix `base` but their keys are absent
  from `config.py`'s resolve list, so they are correct) — the fix is complete for what it
  claims and sweeps no wider.

**Track G — Demographics packaging (NEW 2026-07-12).**
- `tests/test_demographics.py` — intent-based tests for the pure apportionment math
  (`dasymetric_weights`, `apportion`/`apportion_moe`, `proportion_moe`, `pooled_median_income`,
  `property_stats`) against hand-computed values. CI-safe (no data/API).
- `docs/demographics_walkthrough.md` — exact commands, config excerpts, Census-key setup,
  de-identified result screenshots per the figures policy.
- README caveat: demographic outputs are unvalidated estimates (dasymetric apportionment per
  Hill & Larsen 2023).

**Track D — Validation numbers + writeup (parallel; feeds E).**
- [x] **Re-sweep + full-rules LOO — DONE 2026-07-08** (`run_fullrules_loo.py`).
  **Median IoU 0.875 in-sample / 0.863 LOO (optimism +0.012), fold-stable
  (delaunay edge=500 in all 24 folds); 24/24 sites ≥ 0.5.** Caveat: LOO
  cross-validates the sel_r×edge pick only — thresholds/method are hand-fit and
  frozen, so this is a stability result, not out-of-sample generalization (needs
  a held-out city). See decision_log 2026-07-08.
- Rich-HTML writeup: method, validation, boundary-method sweep, results, figures
  (de-identified screenshots per the figures policy above).
- Honest scoping paragraphs: **truth = agreement with expert manual delineation, not ground-truth
  accuracy** (state who drew the polygons); **demographics = unvalidated estimates** (no
  catchment-demographic ground truth exists); **force-main coverage is partial, not absent**
  — as of 2026-08-06, 41.2 of 55.4 mi (74%) of the city's pressurized network is traced;
  the remaining 14.2 mi is listed per system in `QC/force_main_review.csv`. The old
  "gravity-only, force-main-fed subbasins undercounted" caveat is superseded — restate it
  as a quantified residual, not a blanket limitation. The published median IoU of 0.875
  was earned gravity-only and has NOT been re-measured with force mains wired in.

**Track E — Release artifacts (final; needs A/B/D/G).**
- [ ] **BLOCKER — history scrub → new public repo (decided 2026-07-26, do this FIRST in E).**
  The current repo is **private**, and the 2026-07-12 anonymization (`7c046ec`/`e4fdce9`)
  scrubbed only the working tree — every pre-scrub commit still carries the site name, and
  those objects are on GitHub. Recipe, verified 2026-07-26:
  0. **FIRST, before any rewriting:** read the **two identifying strings** — the city name and
     the named creek interceptor — out of `git show 7c046ec` / `git show e4fdce9` in the
     private archive, and write the filter-repo replacements file **outside the repo** (e.g.
     `~/scrub_replacements.txt`). Order matters: after the rewrite those diffs are themselves
     scrubbed, so the strings are unrecoverable from the clone. They are deliberately not
     written into any tracked file — quoting them here would re-contaminate the tree.
  1. `pip install git-filter-repo`; work on a **fresh clone** (the private repo stays as the
     unredacted dev archive — do NOT force-push it).
  2. `--replace-text ~/scrub_replacements.txt` (18 files / 15 commits) AND `--replace-message`
     with the same file — the strings are in commit messages too, not just blobs.
  3. Delete blobs for the six ever-tracked binaries: `QC/trace_17_09_24430.gpkg`, `_v2.gpkg`,
     `QC/competing_pipe_review.gpkg`, `QC/diagnostics_downstream.gpkg`,
     `QC/recheck_v2_4parcels.gpkg`, `QC/validation_traces.gpkg` — real network data, and two
     embed the name in layer names (un-text-scrubbable). **Safe to delete — checked
     2026-07-26:** every remaining reference in the tracked tree is an *output* path the code
     writes (`config.yaml:308`, `run_seam_align.py`, `run_validation_traces.py`) or historical
     narrative in `decision_log.md`. Nothing reads these files as an input, so dropping the
     blobs leaves no dangling dependency.
  4. Verify `git grep -i -E "<name1>|<name2>" $(git rev-list --all)` is empty, then push to a
     **new public repo**; tag + Zenodo from there.
  - **Hazard:** untracked `run_meter_service_areas_from_coords.py` contains the name — scrub
    before committing it, or the rewrite must be redone. Same for anything from `experiments/`.
- `CITATION.cff` + `.zenodo.json`; Zenodo integration → archived **DOI** on tagged release.
- `paper/paper.md` (~600 words: statement of need, framed per the rescope — end-to-end WBE
  pipeline, validated delineation core) + `paper.bib`. Written regardless; submitted only if
  Grace opts into JOSS later.
- Community docs: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, issue templates.
- Author list / affiliations / ORCID.

**Status 2026-07-26:** B, C, F, **G** done (`tests/test_demographics.py`,
`docs/demographics_walkthrough.md`, `docs/img/demographics_choropleth.png`); A partial.
Backlog **pushed 2026-07-26** (`1767a2a..334e5d0`, 11 commits) — first-ever CI trigger, geo-stack
install on ubuntu-latest still to be confirmed. **Next: remaining A (LICENSE / pyproject.toml /
data README), D writeup, E artifacts (history scrub first).** Open confirms:
license (MIT?), author list/ORCID, data provenance for `data/README.md`, P2-7 drop-or-redefine.

**Undocumented side quest, 2026-07-17 → 2026-07-26** (untracked, no log entry): `experiments/`
(`run_edge_rescore.py`, `run_snap_rescore.py`, `run_blockfill_rescore.py`,
`run_hull_candidates.py`, `pop_by_operational_area.*`), `QC/competing_pipe_review_edge500.csv`,
`run_meter_service_areas_from_coords.py`. The rescore scripts score *deltas* off the ~0.81
pre-full-rules baseline, so they do not touch the quotable 0.875 / 0.863 LOO — but **whether any
variant won is unrecorded.** Decide: commit as a documented side quest, or gitignore. Either way
log the outcome.

---

### Post-release — QA flag-classification rework (Grace's deep-dive; NOT Phase 9)

Scoped out of the release on 2026-07-12 (ship QA as-is with honest docs). The repair machinery
(`qa_review.py` / `pipe_edits.py` / `pipe_splits.py`) ships regardless — `load_graph_from_config`
applies the decisions CSV on every delineation run — so this rework targets the flag-*emission*/
triage side. Starting requirements, from the 2026-07-12 code audit:

- **Severity is a hard-coded literal per check** (`network_qa.py:101-112` dispatch) — no scoring,
  no confidence, two fixed strings. Severity does not track actionability (tier-2 `snap_gap` is
  review_required but ~all resolved as non-issues; `disconnected_component` is warning but every
  reviewed one needed human judgment).
- **~85% of flags are by-design non-actionable bulk** (last reviewed run: isolated_manhole 522,
  invert_conflict 267, missing_direction 112 — all zero reviewer attention), hidden from the PDF
  but still drowning the CSV.
- **snap_gap ran ~1/44 true-positive** by Grace's own review comments — the one type that draws
  human effort is almost all false alarms.
- **`component_<i>` IDs are unstable across runs** (`qa_review.py:24-27`), forcing the
  proximity-fallback machinery just to keep decisions attached.
- **None of the 11 `_check_*` functions has a direct unit test** (repair modules are tested;
  emission/severity logic is not).
- **`gravity_mains_repaired.shp` is consumed by no code** — human/GIS artifact only; consider
  dropping or documenting as such.
- **User-input burden:** 68 hand-authored decision rows for one network; `snap` rows require a
  GIS round-trip to read gap-midpoint x/y. Reducing this (e.g. auto-suggested decisions, better
  pre-filtering) is the payoff target.

A truth-set exists to build on: `QC/qa_review_decisions.csv` + the reviewed flags CSV are
labeled data for what a human actually did with each flag type.

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
   Plus: `disconnected_component` reframed to fragments ≤ 50 nodes (the network is multiple
   real basins — largest component is only ~27% of nodes). QA rerun: 1,066 flags, all
   count changes vs baseline reconciled. See decision_log 2026-07-02.

**P2 — correctness/integrity fixes before multi-site use**
4. [~] **Unify target resolution — evidence gathered, fix deferred (2026-07-12).**
   `validation.trace_sites` uses bare `nearest_node`; production `resolve_target_node`
   has the false-headwater guard. **Discriminating check (`tmp/resolver_diff.py`): both
   resolvers pick the identical node AND identical `pidx_list` for all 24 validation
   sites** — no false-headwater junction among them, so unifying validation onto the
   guarded resolver is a *proven no-op* on the current data and cannot move the validated
   IoU. New `run.py` uses the guarded resolver natively (correct by construction), so the
   sweep/production split no longer affects production. The actual edit to
   `validation.trace_sites` is left for the `delineate_site()` extraction PR (where the
   sweep is re-run anyway), to keep this change off the paper-number code path.
5. [x] **Redefine Phase 7 as the multi-site runner — DONE (2026-07-12).** `run.py` loops a
   site list → one boundary + flag set per site (see Phase 7 above). `trace_manhole` /
   `resolve_target_node` now take an explicit `target` (`("manhole_id", v)` or
   `("coordinate", [x,y])`) so `run.py` passes it per site with **no `cfg["inputs"]`
   mutation** (additive, backward-compatible — existing callers unchanged). Batch callers
   (`run_competing_review.py` etc.) still mutate cfg; migrating them is part of the
   extraction PR.
6. [x] **Fix `qc_flags.gpkg` layer clobbering — DONE (2026-07-12).** `run.py` writes all
   sites' delineation flags in a **single pass**, so the per-site re-run clobber cannot
   occur. Cross-invocation stale-layer case handled by `_clear_delineation_layers`: drops
   old `delin_*` layers (preserving `net_*` QA layers from `run_qa`) before the fresh
   write. The original bug in `run_polygon_output.py:85` (single-site path) is unfixed but
   now superseded by `run.py` as the production entry point.
7. [!] **Pumped-site guard — NOT implemented; premise is stale (flagged 2026-07-12).** The
   item's rationale ("a pumped/lift-station target produces a confidently wrong polygon")
   is the *exact wrong premise the project already corrected and signed off* — see the
   2026-07-02 domain-rule correction (`CLAUDE.md`, `validation.py:19-20`): a lift station
   at the sampling point is a terminal end of a gravity basin and traces normally (30804's
   gravity trace lands in the correct basin). A naive "target is pumped → flag/refuse"
   guard would re-introduce that error. **Decision for Grace:** either (a) drop this item,
   or (b) redefine it as the genuine residual — a *force-main-fed subbasin* upstream is
   silently undercounted (the gravity-only limitation, already a Track-D honest-scoping
   caveat and the live meter/FAO case). That is a network-crossing detector, not a
   per-target lookup, and is much larger scope.

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
    **Still open** — none of the four above is fixed. Two further members of this family
    surfaced 2026-07-26 while building the toy example: (a) `run.py` double-prefixed
    `_base_dir` onto already-resolved output paths — **fixed**, see Track B; (b) `run.py:314`
    hardcodes `base / "output" / "sewershed_final.gpkg"` and ignores the resolved
    `outputs.output_polygon`, so that config key is dead in the primary runner —
    **not fixed**, deliberately out of Track B's scope because changing the default output
    path is a behaviour change for every existing caller. Fold into this item when it is
    taken up.
11. [ ] **`data/README.md` provenance:** source, download date, expected CRS/fields for
    each input (municipal GIS layers, parcels, TIGER blocks, NHGIS extract).

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
   `.gpkg` (contested_parcels / boundary / truth).
   **Update 2026-07-04 (QC round 2):** intersect-priority fix (in-trace pipe
   crossing a parcel is decisive) dropped warnings 699→624; **`cp_owner`
   annotation** added — each contested parcel names which validation site's
   trace its crossing pipe belongs to (blank = non-sampled main). The four
   243778/239919/102868/232487 turned out to be Tract 23 ↔ 10.01 boundary
   overlaps (each parcel served by both catchments) — this IS item 3 surfacing
   in the review. Grace's QC_v2 decisions recorded: those 4 = reassign between
   the neighbours; 132952/140089 = exclude (blank cp_owner). Batch: 1,215
   contested (591 review / 624 warning). **Remaining:** (a) Grace finishes the
   decision column (use cp_owner to spot reassigns; Tract 22 outlier 42%);
   (b) build the consuming pass (exclude | keep | reassign before dissolve) —
   needs a **persisted** decisions file (the review CSV is regenerable);
   (c) decide 216813 (widen foreign-search radius vs leave).
   **Also (QC round 2): pipe-edit feedback loop** (`src/pipe_edits.py`) —
   flip/delete/extend decisions in qa_review_decisions.csv, applied in memory in
   all load paths, `manual_edit` flags for the maintainer. Grace's 7 edits
   applied (flip 41145/60075, delete 54794/54796, extend 12071/50418/64653).
   System-agnostic per Grace's directive: code is general, edits are per-system
   CSV input. See decision_log 2026-07-04.
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
4. [x] **Polygon cosmetics — LARGELY DONE (2026-07-08).** Boundary method now
   delaunay (straight parcel-line edges, no arcs); `align_seams` coincides
   shared borders; `bridge_parts` (runs last) stitches highway-split parts
   (1.02); `fill_uncovered_trace` fills trace-served voids the parcels missed
   (18.02 0.69->0.79). **24/24 sites single-part, all IoU >= 0.5, median ~0.875.**
   Remaining: edge alignment to census blocks (deferred; blocks lost the IoU
   sweep, see 2026-07-07 — parcel edges preferred). See decision_log 2026-07-08.
5. [x] **Site-label → manhole mapping — RESOLVED (2026-07-02):** join
   `Sampling_Locations_05212026.shp` field `Tract` to `AssetID_tx`. Known
   pairs: 5→27508, 7→09289, 14→11069, 18.06→17863, 18.08→30976,
   20.20→03442, 20.23→02201, 20.29→30804, 1.02→29962, 13.01→28175, 23→28080.

---

## New-site selection — Phase B (opened 2026-07-29)

Phase A shipped `src/candidate_screen.py` + `run_candidate_screen.py`: **97 independent
candidate sites** (56 nesting groups over 2,032 qualifying manholes), filtered on served
population ≥ 515 plus THREE overlap tests against the existing 24 — non-nesting (graph), not
inside an existing polygon (point), and no traced pipe inside one (catchment). None subsumes the
others; see decision_log 2026-07-29. 97 is well past the ~25–30 Grace wants to review in Street
View, so narrowing is needed.

Phase B, in the order it should be built:

- **B1 — Socioeconomic contrast score.** Grace chose contrast-vs-existing over
  within-catchment heterogeneity: z-score each candidate on `pct_black`, `pct_hispanic`,
  `median_income`, `poverty_rate`, `snap_rate` against the 24 existing sites, rank by distance
  from that cloud. Fills gaps in the study's design space, which is the defensible framing for
  a paper. Reuse `src/demographics.py:demographics_for_site` — it already returns every needed
  field for an arbitrary polygon — with the existing baseline in
  `output/demographics/sewershed_demographics.csv`.
- **B2 — Siting proxies from data already on disk.** Parcel land use (`PARUSEDESC`) for
  residential/commercial/vacant/park context and a "middle of the woods" flag; a right-of-way
  proxy from the *un-parceled* gaps between parcels for the ~200 ft cart-haul metric. Report
  cart distance as a ranked column with a soft flag, not a hard drop — see the open question
  below.
- **B3 — Exact delineation for finalists only.** `run_meter_service_areas_from_coords.py:
  delineate_at_coord` + `demographics_for_site`. This is where population > 500 is enforced
  for real; the Phase A screen is an estimate that runs ~6% low.
- **B4 — Street View review harness.** HTML sheet, one row per finalist: aerial thumbnail (via
  `src/pdf_maps.py:_add_basemap`, satellite), a per-point Google Street View deeplink
  (`https://www.google.com/maps/@?api=1&map_action=pano&viewpoint=<lat>,<lng>`), the
  demographic and siting columns, and a blank verdict column. Grace named the Street View pass
  as the bulk of the work, so making it click-through rather than copy-paste is the real
  time-saver.

**Attrition is already absorbed, but verify it in B3.** The floor is 515, above the 510.5 implied
by 500 × the worst observed estimator ratio (1.021), so every one of the 97 should clear a true
500. The runner prints this adequacy check every run. Confirm it against B3's exact delineations
and record the real hit rate — that is the number that tells us whether 515 was right.

If attrition still bites, **re-screen at a higher floor rather than filtering this list.** The two
are not equivalent: `minimal_candidates` returns the upstream-most node clearing the floor, so a
higher floor moves each branch's pick downstream instead of dropping the branch. Measured
2026-07-29: filtering the floor-450 list to ≥532 gave 56 sites; re-screening at 560 gave 97.
Use `--population-floor N --output-dir output/candidate_sites_floorN`.

Open questions for Phase B:

- **Cart-haul metric has no road layer.** Grace chose aerial imagery over vector road/sidewalk
  data, but a hard 200 ft cutoff needs geometry. Current plan is the parcel-gap ROW proxy
  (zero new dependencies, honest imprecision). If it proves noisy, city road centerlines are a
  drop-in upgrade for that one metric.
- **Is 515 the right population floor?** It compensates a measured 0.94 median estimator bias
  (worst case 0.71 at the smallest site). Revisit once B3 gives exact delineated populations
  for finalists — that will show directly how many 450–500 estimates were really above 500.
- **How much clearance from an existing sewershed is enough?**
  `existing_boundary_buffer_ft` is 0, so only candidates *inside* an existing polygon are
  rejected. 1 of the 112 sits within 500 ft, and its catchment may clip a neighbour once
  delineated. `dist_to_existing_ft` is carried on every output row; decide the threshold when B3
  shows whether it actually collides.
- **Are the 97 pairwise disjoint from EACH OTHER on the ground?** All three overlap tests check
  candidates against the EXISTING 24, not against one another. Non-nesting among the 97 is
  guaranteed, but two non-nested catchments on unrelated branches can still produce overlapping
  delineated polygons — the same asymmetry that needed tests two and three. Once B3 delineates the
  finalists, run a pairwise intersection over them before handing the lab a list described as
  independent. `cs.trace_pipes_in_existing` generalises to this: pass the other candidates'
  boundaries instead of the existing ones.
- **Should the minimal set be the maximum antichain instead?** `minimal_candidates` returns a
  canonical, conservative antichain. A branch could in principle host two non-nested
  qualifying sites it collapses to one. Only worth solving if Phase B leaves too few options
  in some part of the city.

---

## Force mains — status (2026-08-07)

Pressurized mains are ingested, reviewed, and **wired into the traversal graph**. A trace
now crosses a lift station: standing below a discharge point it follows the force main back
to the wet well and picks up the whole pumped basin above it. Optional and off by default —
`inputs.force_main_edges` is null in the public repo, set in sewershed-lab.

**Coverage on the city's network:**

| | |
|---|---|
| Force main traced | 41.2 of 55.4 mi (74%) |
| Systems wired | 45 of 68 |
| Gravity pipes newly reachable | ~9,900 |
| Still invisible to a trace | 14.2 mi, 23 systems |
| Open review rows | 34 |

**Where the remaining 14.2 mi sits** (`QC/force_main_review.csv`):

- 8 systems, 2.6 mi — no discharge found (nowhere it empties back into gravity)
- 8 systems, 3.3 mi — no wet well found (no pump station identified)
- 3 systems, 2.8 mi — competing discharges, can't tell which is real
- 2 systems, 0.2 mi — only near-miss contacts, nothing accepted
- 2 systems, 5.4 mi — end at a treatment plant; correctly excluded, nothing traces through a plant

**Open questions — updated 2026-09-03**

- ~~Validation has not been re-run with force mains wired~~ **ANSWERED 2026-09-03**:
  measured on an independent 14-meter truth set (real the city monitoring basins, not the
  25-site set the 0.875 number is measured against) — mean IoU 0.731 (gravity-only) →
  0.896 (with force mains). Force mains help, not hurt. See decision_log 2026-09-03.
  Still open: the ORIGINAL 25-site number pooled with this 14-site one, through the same
  production pipeline — `sewershed-lab/validate_pooled.py` running as of this writing.
- ~~`/code-review` has never been run~~ **DONE 2026-09-03** (see decision_log). One hard
  violation found and fixed (`force_mains.py` split into 4 phase modules); several
  judgement-call refactors (enum for classification strings, a long `main()`, tolerance
  params bundled) identified but not yet done — low priority, real risk, worth doing
  only if touching that code again for another reason.
- `qa_review.load_review_decisions` still has no duplicate-header validation and no cp1252
  fallback. Excel round-trips are the known trigger; worked around locally in
  `force_mains._read_csv_rows` but never fixed at the source. A duplicated `decision`
  column silently reads zero decisions and reports success — the worst failure shape.
- `data/snForceMain/` and `data/snForceMain.zip` in sewershed-lab are redundant with
  `data/force_mains.gpkg` and remain uncommitted pending a delete decision.
- 4 of 14 (monitoring-basin set) and an unknown number of the 25-site set fail
  target-resolution snap (surveyed coordinate >50 ft from the network). One-off manhole-id
  overrides fix it per-site; no decision yet on whether a permanent tolerance/coordinate
  fix is worth it.

---

## Deferred / Future

- **Socioeconomic stats:** Join output polygon to ACS census data (race, income, poverty).
  Library candidates: `censusdatadownloader`, direct Census API.
- ~~Multiple manholes per run~~ **DONE** — `run.py --sites` / `--sites-file`.
- ~~Interactive UI~~ **DONE 2026-09-03** — `demo_app.py`, Streamlit, runs on the toy
  network. No real-network GUI in this public repo by design (no data ships with it);
  see `sewershed-lab/app.py` for the real-data version.
- **Multi-state support:** Promote CRS to required config field if project expands beyond NC.
- **CI badge:** GitHub Actions running `pytest` on push, badge in README. Cheap, not yet
  done — discussed 2026-09-03, worth it but not urgent.

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
