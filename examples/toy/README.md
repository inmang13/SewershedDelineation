# Toy example

A sewer network you can delineate immediately, without any municipal GIS
data. The pipe/manhole GEOMETRY is real — it follows actual street
centerlines and intersections in Trinity Park, a residential neighborhood in
Durham, NC (source: OpenStreetMap, (c) OpenStreetMap contributors, ODbL). The
SEWER NETWORK ITSELF IS INVENTED — no real sewer infrastructure is used or
implied, only real street alignment. Parcels stay a synthetic grid (real
assessor values are not redistributable). Census block geometry and
demographics ARE real (2020 decennial + ACS 5-year, public domain) for that
same area. It exists so the pipeline is runnable end to end, including the
demographic join, by anyone who clones the repo; the real municipal input
layers are not redistributable.

```bash
python run.py --config examples/toy/config.yaml
```

Expected output:

```
Loading graph : examples/toy/data/gravity_mains.gpkg

label           target                  pipes  served     acres  status
MH01            MH01                       14      66      76.7  ok

Wrote examples/toy/output/sewershed_final.gpkg (layer: boundary, 1 sites)
Wrote examples/toy/output/flags.csv (0 flags)
Wrote examples/toy/output/qc_flags.gpkg (+0 delineation flag layers)

Delineated 1 site(s); median area 77 acres.
```

`examples/toy/output/` is gitignored — delete it any time.

## Regenerating the data

You don't need to: the layers are committed. `make_toy_data.py` builds the
pipe/manhole/parcel geometry from real street coordinates that are now static
literals in that file (captured once from OpenStreetMap — see its docstring);
regenerating never contacts the network. `fetch_real_census.py` is separate
and DOES contact the network (TIGERweb + the Census API) — it's how the
committed `data/census_blocks.gpkg` and `data/census/*.csv` extract was built,
and you only need to re-run it if you want a fresher Census vintage.

`make_toy_data.py` is deterministic in **content** but not byte-for-byte —
GDAL stamps a write timestamp into each GeoPackage, so re-running dirties all
three files in git even though every feature is identical. Regenerate only
when you mean to change the network. `tests/test_toy_example.py` is what
actually keeps the committed layers honest: it regenerates into a temp
directory and compares geometry and attributes feature by feature.

## What's here

| File | What it is |
|---|---|
| `config.yaml` | Toy config. Delineation parameters are the shipped production values, copied verbatim from the repo-root `config.yaml` — nothing is demo-tuned. `census:`/`demographics:` point at the small real extract below instead of the statewide/county-wide production cache. |
| `make_toy_data.py` | Generator for the network + parcel layers, from real street coordinates baked in as static literals. The data is committed, so you never need to run this; it's here so the geometry is reviewable and reproducible. |
| `fetch_real_census.py` | One-time, network-dependent puller for the real Census extract below. Not part of the test suite. |
| `data/gravity_mains.gpkg` | 14 pipes. Each LineString runs upstream → downstream (real street curves as interior vertices), which is how the tool derives flow direction. |
| `data/manholes.gpkg` | 15 manholes, one per real street intersection. |
| `data/parcels.gpkg` | Synthetic square parcels on a 200 ft grid, sized to cover the real street footprint. |
| `data/census_blocks.gpkg` | 131 real TIGER 2020 census blocks covering the same area (public domain). |
| `data/census/*.csv` | Real 2020 decennial + ACS 5-year estimates for those blocks/block groups (public domain), same cache format `src/census_data.py` reads in production. |

## The network

A 14-pipe tree draining south to the outlet at `MH01`, following real Trinity
Park streets. Junctions at `MH02`, `MH07` and `MH11`; headwaters at `MH08`,
`MH10`, `MH12`, `MH13`, `MH14`, `MH15`.

Indented by flow: each manhole sits below the one it drains into, so everything
nested under a node is upstream of it.

```
MH01                            outlet — trace this for the whole network
├── MH02                        junction (Fernway Avenue)
│   ├── MH05 (Liggett St)
│   │   └── MH09 (W. Corporation St)
│   │       └── MH13            headwater
│   └── MH06 (Fernway Ave)
│       └── MH10                headwater
├── MH03 (Morris St)
│   └── MH07                    junction (Morris St)
│       ├── MH11 (Washington St)
│       │   ├── MH14            headwater
│       │   └── MH15 (W. Geer St)  headwater
│       └── MH12 (W. Corporation St)  headwater
└── MH04 (Morris St)
    └── MH08 (Hunt St)          headwater
```

## Things worth trying

```bash
# A junction partway up — a subset of the network
python run.py --config examples/toy/config.yaml --sites MH02     # 5 pipes

# Several sites at once, including a headwater
python run.py --config examples/toy/config.yaml --sites MH01,MH02,MH07,MH08

# The demographic join, on the real Census extract
python run_demographics.py --config examples/toy/config.yaml --skip-fetch
```

That first multi-site run shows the behaviour that matters:

- **`MH08` is reported as a headwater and skipped** — zero upstream pipes is a
  valid answer, not a crash. It gets a `no_upstream_found` flag and no polygon.

Upstream pipe counts are `MH01` → 14, `MH02` → 5, `MH07` → 4, `MH08` → 0. They
are hand-countable from the diagram above, and `tests/test_toy_example.py`
asserts them.

## Why the node spacing is uneven

Unlike the old synthetic 1000 ft grid, real intersections are 178-1130 ft
apart. That was checked, not assumed: the shipped `delaunay_max_edge_ft`
(500 ft) still keeps the boundary method following the network instead of
bridging across branches into a blob (see docs/decision_log.md 2026-09-06,
which has the actual measured run). Bridging is a function of LATERAL
distance between separate branches, not the sequential spacing along one
pipe's own chain, which is why tighter-than-1000-ft real spacing didn't need
a looser threshold.

## What this example covers now

**Demographics.** `run_demographics.py --config examples/toy/config.yaml
--skip-fetch` runs the full dasymetric join against the real Census extract
committed here — no API key needed to use it (only to refresh it, via
`fetch_real_census.py`). For a walkthrough on the full real-geography
municipal dataset instead, see
[`docs/demographics_walkthrough.md`](../../docs/demographics_walkthrough.md).

**Network QA.** `--qa-only` runs against the toy, but the network is clean by
construction — no direction errors, no snap gaps, no cycles — so it has nothing
to report. The QA phase is only interesting on real, messy data.

**Force mains.** The toy network is gravity-only — no lift station, no pumped
basin — so `demo_app.py` (root of the repo) runs with `wire_force_mains=True`
but it's a no-op here: there's nothing to wire. Adding a synthetic lift
station + force main to this toy network (so the demo can show a trace
actually crossing one) is a candidate future improvement, not yet done. The
real force-main results are on real municipal data — see the README's
[Validation](../../README.md#validation) section.
