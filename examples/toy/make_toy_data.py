"""
Generate the toy example dataset (roadmap Phase 9, Track B).

Builds a small synthetic sewer network and parcel fabric so anyone can run the
delineation pipeline end to end without the municipal GIS layers, which are not
redistributable. Everything here is invented: the coordinates are a round-number
origin in the EPSG:2264 range, not a real place.

Deterministic in content — no randomness, no inputs, no clock-dependent values
in any field. It is NOT byte-reproducible: GDAL stamps a write timestamp into
the GeoPackage's `gpkg_contents.last_change`, so re-running changes all three
file hashes and dirties the working tree even though every feature is identical.
Regenerate only when you mean to change the data.

`tests/test_toy_example.py::test_committed_data_matches_the_generator` is what
actually holds the committed layers to this file: it regenerates into a temp
directory and compares geometry and attributes feature by feature. That check is
on content, which is the property worth guarding — a byte hash would break on a
GDAL version bump without anything real having changed.

    python examples/toy/make_toy_data.py

Writes, next to this script:
    data/gravity_mains.gpkg   12 LineStrings, one per pipe
    data/manholes.gpkg        13 Points, one per node
    data/parcels.gpkg         484 square parcels on a 200 ft grid

Design notes (why the numbers are what they are)
------------------------------------------------
* **Direction is geometry.** Each pipe's LineString runs upstream -> downstream
  (first vertex = upstream), which is the convention `graph_builder` relies on.
  FROMMH/TOMH are not written at all; inverts and slope are cross-checks only.
* **Node spacing is 1000 ft, deliberately.** The shipped
  `delaunay_max_edge_ft` is 500 ft. At tighter spacing Delaunay would bridge
  straight across the gaps between branches and return roughly the convex hull
  of the whole tree — a blob that hides what the tool actually does. 1000 ft
  spacing leaves ~600 ft between neighbouring parcel corridors, comfortably
  above the threshold, so the toy config can keep the real production
  parameters instead of demo-tuned ones.
* **Manholes are emitted from the same NODES dict as the pipe endpoints**, so a
  manhole point is exactly coincident with its endpoint. `manhole_node_snap_ft`
  is 5.0 ft and `node_snap_tolerance_ft` is 1.0 ft; near-identical coordinates
  would produce a confusing "not on the gravity-main network" error.
* **No pipe passes through a node that is not its own endpoint** (every pipe is
  a single grid step), so the midspan-junction splitter finds nothing to do.
"""

from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Point, Polygon

CRS = "EPSG:2264"          # NAD83 / NC State Plane, US survey feet

# Round-number synthetic origin inside the EPSG:2264 coordinate range.
X0, Y0 = 2_000_000.0, 800_000.0

SPACING = 1000.0           # ft between adjacent manholes (see design notes)
PIPE_SLOPE = 0.005         # ft/ft; 5 ft of fall over each 1000 ft pipe
OUTLET_INVERT = 200.0      # invert elevation at the outlet, ft

PARCEL_SIZE = 200.0        # ft; square parcels
GRID_HALF_X = 2200.0       # parcel fabric extent, ft from origin
GRID_MIN_Y, GRID_MAX_Y = -200.0, 4200.0

# --- network ---------------------------------------------------------------
# Node offsets from (X0, Y0) in feet. MH01 is the outlet; the tree drains south
# to it. MH08, MH11, MH12 and MH13 are headwaters (nothing upstream of them).
NODES = {
    "MH01": (0, 0),           # outlet
    "MH02": (0, 1000),
    "MH03": (0, 2000),        # junction — west lateral joins here
    "MH04": (0, 3000),        # junction — east lateral joins here
    "MH05": (0, 4000),        # junction — two headwater branches join here
    "MH06": (-1000, 2000),
    "MH07": (-2000, 2000),
    "MH08": (-2000, 3000),    # headwater
    "MH09": (1000, 3000),
    "MH10": (2000, 3000),
    "MH11": (2000, 4000),     # headwater
    "MH12": (-1000, 4000),    # headwater
    "MH13": (1000, 4000),     # headwater
}

# (FACILITYID, upstream node, downstream node). Order is drawn direction.
PIPES = [
    ("P001", "MH02", "MH01"),
    ("P002", "MH03", "MH02"),
    ("P003", "MH04", "MH03"),
    ("P004", "MH05", "MH04"),
    ("P005", "MH06", "MH03"),
    ("P006", "MH07", "MH06"),
    ("P007", "MH08", "MH07"),
    ("P008", "MH09", "MH04"),
    ("P009", "MH10", "MH09"),
    ("P010", "MH11", "MH10"),
    ("P011", "MH12", "MH05"),
    ("P012", "MH13", "MH05"),
]

def _steps_from_outlet() -> dict[str, int]:
    """How many pipes each node sits above the outlet.

    Used only to set invert elevations so they fall consistently downstream
    (cross-check attributes — direction itself comes from geometry). Derived
    from PIPES rather than hand-tabulated, so editing the network can't leave a
    stale step count behind.
    """
    downstream_of = {up: dn for _, up, dn in PIPES}
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
    """Invert elevation, rising by one pipe's fall per step from the outlet."""
    return OUTLET_INVERT + STEPS_FROM_OUTLET[node] * SPACING * PIPE_SLOPE


def build_mains() -> gpd.GeoDataFrame:
    """The gravity mains, drawn upstream -> downstream."""
    rows = []
    for fid, up, dn in PIPES:
        geom = LineString([_xy(up), _xy(dn)])
        rows.append({
            "FACILITYID": fid,
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

    PARUSEDESC and PARVAL match the schema the demographics phase expects so the
    layer is shape-compatible, but the toy cannot run that phase (see README).
    """
    rows = []
    n = 0
    y = GRID_MIN_Y
    while y < GRID_MAX_Y - 1e-9:
        x = -GRID_HALF_X
        while x < GRID_HALF_X - 1e-9:
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
