# Toy example

A synthetic sewer network you can delineate immediately, without any municipal
GIS data. Everything here is invented — the network, the parcels, the
coordinates. It exists so the pipeline is runnable by anyone who clones the
repo; the real input layers are not redistributable.

```bash
python run.py --config examples/toy/config.yaml
```

Expected output:

```
label           target                  pipes  served     acres  status
MH01            MH01                       12     124     120.8  ok

Wrote examples/toy/output/sewershed_final.gpkg (layer: boundary, 1 sites)
Wrote examples/toy/output/flags.csv (0 flags)
```

`examples/toy/output/` is gitignored — delete it any time.

## What's here

| File | What it is |
|---|---|
| `config.yaml` | Toy config. Parameters are the shipped production values, copied verbatim from the repo-root `config.yaml` — nothing is demo-tuned. |
| `make_toy_data.py` | Deterministic generator for the three layers below. The data is committed, so you never need to run this; it's here so the geometry is reviewable and reproducible. |
| `data/gravity_mains.gpkg` | 12 pipes. Each LineString runs upstream → downstream, which is how the tool derives flow direction. |
| `data/manholes.gpkg` | 13 manholes, one per pipe endpoint. |
| `data/parcels.gpkg` | 484 square parcels on a 200 ft grid. |

## The network

A 12-pipe tree draining south to the outlet at `MH01`. Junctions at `MH03`,
`MH04` and `MH05`; headwaters at `MH08`, `MH11`, `MH12`, `MH13`.

Indented by flow: each manhole sits below the one it drains into, so everything
nested under a node is upstream of it.

```
MH01                      outlet — trace this for the whole network
└── MH02
    └── MH03              junction
        ├── MH04          junction
        │   ├── MH05      junction
        │   │   ├── MH12  headwater
        │   │   └── MH13  headwater
        │   └── MH09
        │       └── MH10
        │           └── MH11        headwater
        └── MH06
            └── MH07
                └── MH08  headwater
```

## Things worth trying

```bash
# A junction partway up — a subset of the network
python run.py --config examples/toy/config.yaml --sites MH04     # 6 pipes

# Several sites at once, including a headwater
python run.py --config examples/toy/config.yaml --sites MH01,MH03,MH04,MH08
```

That last one is the interesting run. It shows two behaviours that matter:

- **`MH08` is reported as a headwater and skipped** — zero upstream pipes is a
  valid answer, not a crash. It gets a `no_upstream_found` flag and no polygon.
- **Competing-pipe flags fire on `MH03` and `MH04`.** Tracing `MH03` makes
  `P002` — the pipe immediately *downstream* of it — a foreign main, so parcels
  that `P002` crosses are flagged `review_required` rather than silently
  assigned. That is the check doing its job on real geometry.

Upstream pipe counts are `MH01` → 12, `MH03` → 10, `MH04` → 6, `MH08` → 0. They
are hand-countable from the diagram above, and `tests/test_toy_example.py`
asserts them.

## Why the network is so spread out

Manholes sit 1000 ft apart, which is generous for a real collection system. That
is deliberate. The shipped `delaunay_max_edge_ft` is 500 ft: at tighter spacing
the boundary step would bridge straight across the gaps between branches and
return roughly the convex hull of the whole tree — a blob that hides what the
tool actually does. Spreading the network out lets the toy config keep the real
production parameters instead of demo-tuned ones.

## What this example does not cover

**Demographics.** The `run_demographics.py` phase needs real Census geography
(actual state/county FIPS codes, real block GEOIDs) and a Census API key, none
of which can be faked on a synthetic network in a made-up location. Building a
synthetic census cache was considered and rejected as a lot of work for little
value (decision_log 2026-07-12).

For the demographic join, see [`docs/demographics_walkthrough.md`](../../docs/demographics_walkthrough.md),
which documents a real-geography run with commands, config, and de-identified
results.

**Network QA.** `--qa-only` runs against the toy, but the network is clean by
construction — no direction errors, no snap gaps, no cycles — so it has nothing
to report. The QA phase is only interesting on real, messy data.
