"""
Generate the toy example dataset (roadmap Phase 9, Track B).

Builds a small sewer network and parcel fabric so anyone can run the
delineation pipeline end to end without the municipal GIS layers, which are
not redistributable.

The pipe/manhole GEOMETRY is real: it follows actual street centerlines and
intersections in Trinity Park, a residential neighborhood in Durham, NC
(source: OpenStreetMap, (c) OpenStreetMap contributors, ODbL,
https://www.openstreetmap.org/copyright -- pulled via the Overpass API on
2026-09-06). The SEWER NETWORK ITSELF IS INVENTED: no real sewer infrastructure
is used or implied. Real streets are dendritic often enough in practice
(a spanning tree taken from the real intersection graph, rooted at the
southernmost junction) that this reads as a plausible sewershed without
claiming to trace an actual pipe. Pipe attributes (FACILITYID, invert
elevations, slope) are synthetic, assigned the same way as before: one
invented elevation step per pipe, falling toward the outlet.

Parcels remain an invented regular grid (PARCEL_SIZE ft squares), now sized to
cover the real street footprint. Real assessor parcel geometry/values are not
redistributable, which is exactly why this stays synthetic -- see the
`examples/toy/data/census_blocks.gpkg` docstring in fetch_real_census.py for
the real data this example DOES ship (Census block geometry + demographics,
which are public domain and fine to commit).

Deterministic in content — no randomness, no live network calls, no
clock-dependent values in any field (the real street coordinates below were
captured once by extract from OpenStreetMap and are now static literals in
this file). It is NOT byte-reproducible: GDAL stamps a write timestamp into
the GeoPackage's `gpkg_contents.last_change`, so re-running changes all three
file hashes and dirties the working tree even though every feature is
identical. Regenerate only when you mean to change the data.

`tests/test_toy_example.py::test_committed_data_matches_the_generator` is what
actually holds the committed layers to this file: it regenerates into a temp
directory and compares geometry and attributes feature by feature. That check
is on content, which is the property worth guarding — a byte hash would break
on a GDAL version bump without anything real having changed.

    python examples/toy/make_toy_data.py

Writes, next to this script:
    data/gravity_mains.gpkg   14 LineStrings, one per pipe (real street shapes)
    data/manholes.gpkg        15 Points, one per real intersection
    data/parcels.gpkg         square parcels on a 200 ft grid, sized to fit

Design notes (why the numbers are what they are)
------------------------------------------------
* **Direction is geometry.** Each pipe's LineString runs upstream -> downstream
  (first vertex = upstream), which is the convention `graph_builder` relies on.
  FROMMH/TOMH are not written at all; inverts and slope are cross-checks only.
* **Manholes are emitted from the same NODES dict as the pipe endpoints**, so a
  manhole point is exactly coincident with its endpoint. `manhole_node_snap_ft`
  is 5.0 ft and `node_snap_tolerance_ft` is 1.0 ft; near-identical coordinates
  would produce a confusing "not on the gravity-main network" error.
* **No pipe passes through a node that is not its own endpoint.** Real street
  curves are kept as interior vertices on the pipe LineString (so the map shows
  the actual street shape), but every interior vertex is a plain shape point,
  not a graph node — the midspan-junction splitter has nothing to do.
* **Node spacing is real, and uneven** (178-1130 ft between adjacent
  intersections) — unlike the original synthetic 1000 ft grid. This is real
  neighborhood block spacing, verified by running the actual pipeline against
  it (see docs/decision_log.md 2026-09-06): the Delaunay boundary method
  handles it fine at the shipped `delaunay_max_edge_ft` (500 ft) because
  bridging is a function of LATERAL distance between separate branches, not
  sequential along-pipe node spacing.
"""

from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Point, Polygon

CRS = "EPSG:2264"          # NAD83 / NC State Plane, US survey feet

# Real-world anchor: MH01 (the outlet) sits at the real intersection of
# Fernway Avenue / Morris Street in Trinity Park, Durham, NC
# (35.999688 N, -78.903643 W), reprojected to EPSG:2264. This MUST be the
# actual real-world coordinate, not a round-number placeholder like the old
# synthetic network used — the real Census block/block-group extract in
# data/census_blocks.gpkg is geolocated to the real Trinity Park area, and the
# demographic join only finds it if this network sits on top of it.
X0, Y0 = 2_028_502.46, 818_798.60

PIPE_SLOPE = 0.005         # ft/ft; invented — real slope was never surveyed
OUTLET_INVERT = 200.0      # invert elevation at the outlet, ft

PARCEL_SIZE = 200.0        # ft; square parcels (invented land fabric)
GRID_MIN_X, GRID_MAX_X = -1400.0, 1400.0
GRID_MIN_Y, GRID_MAX_Y = -400.0, 2500.0

# --- network: real intersections, Trinity Park, Durham NC ------------------
# Node offsets from (X0, Y0) in feet, from real OSM intersection coordinates.
# MH01 is the outlet (real intersection, chosen as the southernmost junction);
# the tree drains south to it, same convention as before.
NODES = {
    "MH01": (0.0, 0.0),
    "MH02": (-248.8, 14.7),
    "MH03": (11.4, 188.6),
    "MH04": (405.7, 174.4),
    "MH05": (-108.9, 940.9),
    "MH06": (-690.4, 274.2),
    "MH07": (59.8, 883.2),
    "MH08": (630.2, 169.9),      # headwater
    "MH09": (-444.6, 985.0),
    "MH10": (-1026.3, 526.5),    # headwater
    "MH11": (166.6, 1485.4),
    "MH12": (443.2, 833.3),      # headwater
    "MH13": (-792.2, 983.2),     # headwater
    "MH14": (485.7, 2113.3),     # headwater
    "MH15": (1064.9, 1489.2),    # headwater
}

# (FACILITYID, upstream node, downstream node, real street name,
#  [interior vertex offsets ft] tracing the real street curve between them).
# Order is drawn direction: first vertex of the LineString = upstream.
PIPES = [
    ("P001", "MH02", "MH01", "Fernway Avenue", [(-19.2, 2.2)]),
    ("P002", "MH03", "MH01", "Morris Street", []),
    ("P003", "MH04", "MH01", "Morris Street",
     [(-7.6, -126.3), (-16.9, -281.4), (10.0, -283.9), (184.6, -285.6),
      (328.0, -286.5), (369.4, -286.1), (373.0, -248.3), (374.4, -228.2)]),
    ("P004", "MH05", "MH02", "Liggett Street",
     [(-246.7, 38.0), (-217.5, 383.1), (-199.3, 580.8), (-183.6, 773.7),
      (-173.2, 811.2), (-122.2, 915.4)]),
    ("P005", "MH06", "MH02", "Fernway Avenue",
     [(-330.4, 21.5), (-351.4, 26.6), (-363.3, 35.0), (-373.0, 41.1),
      (-437.0, 90.9), (-447.0, 99.3), (-499.1, 137.1), (-526.4, 156.8)]),
    ("P006", "MH07", "MH03", "Morris Street",
     [(23.4, 311.3), (28.3, 364.1), (31.8, 421.9), (39.4, 529.7),
      (42.3, 572.3), (42.6, 578.8), (58.7, 818.0)]),
    ("P007", "MH08", "MH04", "Hunt Street", [(597.1, 171.3), (604.4, 170.6)]),
    ("P008", "MH09", "MH05", "West Corporation Street",
     [(-173.7, 964.9), (-203.6, 974.3), (-237.3, 981.9), (-282.8, 986.2),
      (-378.1, 986.5)]),
    ("P009", "MH10", "MH06", "Fernway Avenue",
     [(-709.9, 288.4), (-816.2, 366.5), (-826.5, 374.2), (-1011.8, 517.8)]),
    ("P010", "MH11", "MH07", "Washington Street",
     [(60.9, 951.6), (74.3, 1210.1), (81.6, 1284.0), (86.6, 1314.9),
      (93.3, 1343.7), (141.8, 1438.0)]),
    ("P011", "MH12", "MH07", "West Corporation Street",
     [(80.8, 875.2), (153.0, 847.2), (181.4, 839.2), (212.2, 833.1),
      (247.1, 830.9), (418.9, 829.7), (426.0, 829.7)]),
    ("P012", "MH13", "MH09", "West Corporation Street",
     [(-598.5, 982.7), (-628.9, 982.6), (-770.0, 983.2)]),
    ("P013", "MH14", "MH11", "Washington Street",
     [(204.9, 1559.7), (345.2, 1833.6), (352.8, 1848.9), (362.3, 1867.1),
      (447.9, 2038.2), (470.3, 2082.3)]),
    ("P014", "MH15", "MH11", "West Geer Street",
     [(201.5, 1483.6), (527.7, 1485.0), (637.2, 1485.5), (662.9, 1485.5),
      (693.7, 1485.5), (743.9, 1485.6), (816.7, 1486.0), (860.2, 1486.1),
      (883.5, 1486.4)]),
]


def _steps_from_outlet() -> dict[str, int]:
    """How many pipes each node sits above the outlet.

    Used only to set invert elevations so they fall consistently downstream
    (cross-check attributes — direction itself comes from geometry). Derived
    from PIPES rather than hand-tabulated, so editing the network can't leave a
    stale step count behind.
    """
    downstream_of = {up: dn for _, up, dn, *_ in PIPES}
    steps = {}
    for node in NODES:
        n, cur = 0, node
        while cur in downstream_of:
            cur = downstream_of[cur]
            n += 1
            if n > len(PIPES):                      # a cycle would never exit
                raise ValueError(f"{node} does not drain to an outlet")
        steps[node] = n
    return steps


STEPS_FROM_OUTLET = _steps_from_outlet()


def _xy(node: str) -> tuple[float, float]:
    """Absolute (x, y) of a node, in EPSG:2264 feet."""
    dx, dy = NODES[node]
    return (X0 + dx, Y0 + dy)


def _invert(node: str) -> float:
    """Invert elevation, falling by one invented step per pipe from the outlet."""
    return OUTLET_INVERT + STEPS_FROM_OUTLET[node] * 200.0 * PIPE_SLOPE


def build_mains() -> gpd.GeoDataFrame:
    """The gravity mains, drawn upstream -> downstream, following real street
    curves between real intersections."""
    rows = []
    for fid, up, dn, street, interior in PIPES:
        up_xy = _xy(up)
        dn_xy = _xy(dn)
        interior_xy = [(X0 + dx, Y0 + dy) for dx, dy in interior]
        geom = LineString([up_xy, *interior_xy, dn_xy])
        rows.append({
            "FACILITYID": fid,
            "STREETNAME": street,
            "UPSTREAMIN": round(_invert(up), 2),
            "DOWNSTREAM": round(_invert(dn), 2),
            "SLOPE": PIPE_SLOPE,
            "geometry": geom,
        })
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def build_manholes() -> gpd.GeoDataFrame:
    """One manhole per node, exactly coincident with the pipe endpoints."""
    rows = [{"FACILITYID": name,
             "INVERTELEV": round(_invert(name), 2),
             "geometry": Point(_xy(name))}
            for name in NODES]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def build_parcels() -> gpd.GeoDataFrame:
    """A regular grid of square parcels covering (and overhanging) the network.

    Real parcels do not tile cleanly — that irregularity is exactly what the
    boundary step exists to smooth over — but a regular fabric keeps the toy
    readable and makes the served corridor obvious. Parcels beyond the selection
    radius are included on purpose: a run should leave most of them out.

    PARUSEDESC and PARVAL are still invented (assessor values are not
    redistributable) — real socioeconomic detail for this example comes from
    Census block/block-group data instead (see fetch_real_census.py).
    """
    rows = []
    n = 0
    y = GRID_MIN_Y
    while y < GRID_MAX_Y - 1e-9:
        x = GRID_MIN_X
        while x < GRID_MAX_X - 1e-9:
            # Deterministic land-use pattern: every 7th parcel commercial, the
            # rest single-family residential.
            commercial = (n % 7 == 0)
            rows.append({
                "PARCELID": f"T{n:04d}",
                "PARUSEDESC": "COMMERCIAL" if commercial else "RES/SINGLE FAMILY",
                "PARVAL": 450_000 if commercial else 210_000 + (n % 5) * 15_000,
                "geometry": Polygon([
                    (X0 + x, Y0 + y),
                    (X0 + x + PARCEL_SIZE, Y0 + y),
                    (X0 + x + PARCEL_SIZE, Y0 + y + PARCEL_SIZE),
                    (X0 + x, Y0 + y + PARCEL_SIZE),
                ]),
            })
            n += 1
            x += PARCEL_SIZE
        y += PARCEL_SIZE
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


LAYER_BUILDERS = {
    "gravity_mains": build_mains,
    "manholes": build_manholes,
    "parcels": build_parcels,
}


def main(out_dir: Path | None = None, quiet: bool = False) -> None:
    """Write all three layers. `out_dir` defaults to the data/ dir beside this
    file; the determinism test passes a temp directory instead."""
    out_dir = Path(out_dir) if out_dir else Path(__file__).parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)

    # One layer per file: gpd.read_file() without an explicit `layer=` takes the
    # first layer, and the pipeline never passes one.
    for name, build in LAYER_BUILDERS.items():
        gdf = build()
        path = out_dir / f"{name}.gpkg"
        gdf.to_file(path, layer=name, driver="GPKG")
        if not quiet:
            print(f"wrote {path}  ({len(gdf)} features)")


if __name__ == "__main__":
    main()
