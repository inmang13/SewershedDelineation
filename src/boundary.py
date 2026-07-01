"""
Phase 8 — boundary construction.

Turn a set of served population units (parcels or census blocks) into a single
*seamless* sewershed polygon. The raw dissolved parcel union is full of holes —
parcels omit streets and public right-of-way — so it doesn't resemble the
hand-drawn truth polygons. This module offers four candidate methods; the best
(method, radii) is chosen empirically by src/validation.py against the 25-site
truth set.

Common entry point:

    build_boundary(served_gdf, method, close_ft=..., concave_ratio=..., blocks_gdf=...)

Methods (decision_log 2026-07-01):
  morph_close     buffer +r -> buffer -r on the served union (the tuned primary
                  method; automates Grace's manual buffer/fill/shrink workflow)
  blocks_dissolve dissolve the served census blocks (blocks tile -> already seamless)
  hybrid          union parcels with the blocks that fill their gaps, then close
  concave         shapely.concave_hull of the served union (Delaunay alpha shape)

Two rules apply to EVERY method's output, so build_boundary enforces them
centrally rather than in each method:
  - Fill ALL interior holes  -> a solid polygon (interior lakes/cemeteries/ROW
    carry no served population; filling avoids skewing the demographic join).
  - Keep ALL parts (multipart) -> a detached served pocket is real, not dropped.
"""

from shapely import concave_hull
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union

VALID_METHODS = ("morph_close", "blocks_dissolve", "hybrid", "concave")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _polygon_parts(geom) -> list:
    """
    Flatten a geometry to its polygonal parts, dropping stray lines/points that a
    concave hull or union can leave in a GeometryCollection. Returns [] for
    None/empty/non-polygonal input.
    """
    if geom is None or geom.is_empty:
        return []
    t = geom.geom_type
    if t == "Polygon":
        return [geom]
    if t == "MultiPolygon":
        return list(geom.geoms)
    if t == "GeometryCollection":
        parts = []
        for g in geom.geoms:
            parts.extend(_polygon_parts(g))
        return parts
    return []  # LineString/Point/etc. — no area


def keep_all_parts(geom):
    """
    Normalize to a Polygon (single part) or MultiPolygon (many), preserving every
    polygonal part. Never selects the largest part — disconnected served pockets
    are kept. Returns None if there is no polygonal area.
    """
    parts = _polygon_parts(geom)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return MultiPolygon(parts)


def fill_holes(geom):
    """
    Remove all interior rings — rebuild each polygonal part from its exterior and
    re-union (touching parts whose holes are filled may now overlap). Returns None
    if there is no polygonal area.
    """
    parts = _polygon_parts(geom)
    if not parts:
        return None
    filled = [Polygon(p.exterior) for p in parts]
    return unary_union(filled)


def _finalize(geom):
    """Apply the two universal rules: fill all holes, then keep all parts."""
    return keep_all_parts(fill_holes(geom))


# ---------------------------------------------------------------------------
# Candidate methods (raw geometry ops; _finalize is applied by build_boundary)
# ---------------------------------------------------------------------------

def morph_close(geom, close_ft: float):
    """
    Morphological close: dilate by close_ft then erode by the same. Bridges gaps
    up to ~2*close_ft (streets between parcels) and rounds the outline, without
    net-growing the footprint. `geom` is the dissolved served union.
    """
    if geom is None or geom.is_empty:
        return None
    return geom.buffer(close_ft).buffer(-close_ft)


def hybrid(parcels_union, blocks_gdf, close_ft: float):
    """
    Fill parcel gaps with census blocks, then close. Selects the blocks that
    intersect the parcel footprint (they cover the streets/ROW parcels omit),
    unions them with the parcels, and morph-closes any residual seams.
    """
    if parcels_union is None or parcels_union.is_empty:
        return None
    if blocks_gdf is None or blocks_gdf.empty:
        return morph_close(parcels_union, close_ft)
    idx = blocks_gdf.sindex.query(parcels_union, predicate="intersects")
    gap_fillers = blocks_gdf.iloc[sorted(idx)].geometry.union_all()
    merged = unary_union([parcels_union, gap_fillers])
    return morph_close(merged, close_ft)


def concave(geom, ratio: float):
    """
    Concave hull (Delaunay alpha shape) of the served union's vertices. `ratio`
    is shapely's single knob: 0 hugs the points tightly, 1 -> convex hull.
    """
    if geom is None or geom.is_empty:
        return None
    return concave_hull(geom, ratio=ratio)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def build_boundary(served_gdf, method, *, close_ft=100.0, concave_ratio=0.3,
                   blocks_gdf=None, served_union=None):
    """
    Build a seamless boundary polygon from served units.

    Parameters
    ----------
    served_gdf    GeoDataFrame of served units (parcels for morph_close/hybrid/
                  concave; selected census blocks for blocks_dissolve). Its
                  dissolved union is the geometry all methods start from.
    method        one of VALID_METHODS.
    close_ft      morphological close radius (morph_close, hybrid).
    concave_ratio shapely.concave_hull ratio (concave only).
    blocks_gdf    county census blocks to draw gap-fillers from (hybrid only).
    served_union  optional precomputed union of served_gdf.geometry. The sweep
                  passes this so the (expensive) union_all isn't recomputed for
                  every method/close_r combo that shares the same served set.

    Returns a Polygon/MultiPolygon (holes filled, all parts kept), or None if
    there are no served units.
    """
    if method not in VALID_METHODS:
        raise ValueError(f"unknown boundary method '{method}'; "
                         f"expected one of {list(VALID_METHODS)}")
    if served_gdf is None or served_gdf.empty:
        return None

    union = served_union if served_union is not None else served_gdf.geometry.union_all()
    if union is None or union.is_empty:
        return None

    if method == "morph_close":
        raw = morph_close(union, close_ft)
    elif method == "blocks_dissolve":
        raw = union  # served_gdf are blocks; the union IS the dissolve
    elif method == "hybrid":
        raw = hybrid(union, blocks_gdf, close_ft)
    else:  # concave
        raw = concave(union, concave_ratio)

    return _finalize(raw)
