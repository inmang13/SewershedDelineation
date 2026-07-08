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

from shapely import concave_hull, delaunay_triangles, make_valid
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union, snap

VALID_METHODS = ("morph_close", "blocks_dissolve", "hybrid", "concave",
                 "block_fill", "delaunay")


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

def morph_close(geom, close_ft: float, join_style: str = "round"):
    """
    Morphological close: dilate by close_ft then erode by the same. Bridges gaps
    up to ~2*close_ft (streets between parcels) without net-growing the
    footprint. `geom` is the dissolved served union.

    join_style "round" (default) turns every corner into a close_ft arc —
    visually "blobby". "mitre" keeps corners as corners (mitre_limit caps how
    far a spike can extend at acute angles).
    """
    if geom is None or geom.is_empty:
        return None
    kw = {"join_style": join_style}
    if join_style == "mitre":
        kw["mitre_limit"] = 2.0
    return geom.buffer(close_ft, **kw).buffer(-close_ft, **kw)


def trim_to_parcels(closed, served_union, units_gdf):
    """
    Clip a closed boundary's rounded overhang back to the parcel fabric.

    morph_close's dilate/erode leaves arcs that hang over neighbouring
    *unserved* parcels — area the truth polygons (drawn along parcel lines)
    never include. This keeps the street/ROW fill the close added (area on no
    parcel at all) but replaces every on-parcel piece with the served parcels
    themselves, so the outline follows real parcel edges except across streets:

        trimmed = served_union ∪ (closed − all_parcels)

    Returns the trimmed geometry (caller finalizes), or None for empty input.
    """
    if closed is None or closed.is_empty:
        return None
    if units_gdf is None or units_gdf.empty:
        return closed
    idx = units_gdf.sindex.query(closed, predicate="intersects")
    all_parcels = units_gdf.iloc[sorted(idx)].geometry.union_all()
    street_fill = closed.difference(all_parcels)
    return unary_union([served_union, street_fill])


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


def delaunay_fill(union, max_edge_ft: float):
    """
    Parcels + short Delaunay bridges (chosen 2026-07-07; winner on hole-heavy
    sites): triangulate the served union's vertices, keep the gap triangles
    (not inside the parcels) whose longest side <= max_edge_ft, and union them
    with the parcels. The straight-edged equivalent of a morphological close
    with radius ~max_edge/2 — bridges streets and bays without buffer arcs;
    every edge is a real parcel line or a straight bridge.
    """
    if union is None or union.is_empty:
        return None
    keep = [union]
    for t in delaunay_triangles(union).geoms:
        if t.is_empty:
            continue
        xs, ys = t.exterior.coords.xy
        longest = max(((xs[i] - xs[i + 1]) ** 2 + (ys[i] - ys[i + 1]) ** 2) ** 0.5
                      for i in range(3))
        if longest > max_edge_ft:
            continue
        if union.contains(t.centroid) and t.within(union):
            continue  # interior triangle, adds nothing
        keep.append(t)
    return unary_union(keep)


def block_fill(served_union, blocks_gdf, min_frac: float = 0.5):
    """
    Fill gaps with census blocks instead of dilation (Grace's method,
    2026-07-07): a block joins the sewershed when served parcels cover
    >= min_frac of its area; the boundary is the union of served parcels +
    qualifying blocks. Blocks are street-bounded, so a qualifying block brings
    the streets/ROW between its parcels with it — no morphological close, no
    buffer arcs; every edge is a real parcel or block line. Residual interior
    slivers (streets in blocks that didn't qualify) are handled by the
    universal fill-holes rule in build_boundary.
    """
    if served_union is None or served_union.is_empty:
        return None
    if blocks_gdf is None or blocks_gdf.empty:
        return served_union
    idx = blocks_gdf.sindex.query(served_union, predicate="intersects")
    keep = [served_union]
    for blk in blocks_gdf.iloc[sorted(idx)].geometry:
        if blk is None or blk.is_empty or blk.area == 0:
            continue
        if blk.intersection(served_union).area / blk.area >= min_frac:
            keep.append(blk)
    return unary_union(keep)


def concave(geom, ratio: float):
    """
    Concave hull (Delaunay alpha shape) of the served union's vertices. `ratio`
    is shapely's single knob: 0 hugs the points tightly, 1 -> convex hull.
    """
    if geom is None or geom.is_empty:
        return None
    return concave_hull(geom, ratio=ratio)


# ---------------------------------------------------------------------------
# Cross-site post-passes (operate on the finished per-site boundaries)
# ---------------------------------------------------------------------------

def bridge_parts(geom, max_gap_ft: float):
    """
    Stitch the disjoint parts of one site's boundary across a wide gap, filling
    the whole channel between them (e.g. a site split by a major highway —
    Tract 1.02, 2026-07-08).

    delaunay/morph_close bridge gaps only up to their tuned radius, so a
    highway wider than that leaves the catchment as two parts. This runs a
    morphological close at radius max_gap_ft, then keeps ONLY the fill pieces
    that touch two or more of the original parts. That gives the full-width
    corridor between the parts —
    not a thin neck — while discarding the rounded bulges a close would other-
    wise add along the outer edge (those touch just one part). Mitre joins keep
    the corridor's own edges straight. A single-part boundary is returned
    unchanged; established parts' shapes are never altered (only gap area is
    added).

    max_gap_ft is deliberately separate from (and larger than) the boundary
    method's bridge length: it only ever fills between established parts, so it
    can't over-bridge the outer fringe the way a large global radius would.
    """
    if geom is None or geom.is_empty:
        return geom
    parts = _polygon_parts(geom)
    if len(parts) < 2:
        return geom
    # Radius == the gap width (not half): a real inter-part gap tapers to a
    # pinch, and closing a pinch of width w needs r ~= w, not w/2 (erosion kills
    # a half-radius bridge). The touch>=2 filter below discards the extra outer
    # rounding this larger radius would otherwise leave behind.
    r = max_gap_ft
    closed = geom.buffer(r, join_style="mitre", mitre_limit=2.0) \
                 .buffer(-r, join_style="mitre", mitre_limit=2.0)
    fill = closed.difference(geom)
    keep = [geom]
    for piece in _polygon_parts(fill):
        # A corridor fill abuts >= 2 parts; a rounded outer bulge abuts only 1.
        touched = sum(1 for p in parts if p.distance(piece) < 1.0)
        if touched >= 2:
            keep.append(piece)
    return unary_union(keep)


def align_seams(bounds_by_site: dict, tol_ft: float = 50.0) -> dict:
    """
    Make shared borders between output sewersheds coincide.

    Each site's morph_close boundary is smoothed independently, so where two
    catchments abut, the two outlines wobble within a few tens of feet of each
    other — sliver gaps and hairline overlaps along a border that should be one
    line. This pass snaps each polygon's vertices (and inserts the neighbour's
    vertices into its segments) wherever they fall within tol_ft of an
    already-processed polygon, so both sides of a seam end up on the same line.
    Away from seams nothing is within tol_ft, so the tuned boundary is
    untouched — the IoU cost is ~zero by construction.

    Deterministic: sites are processed in sorted-key order, each snapped to the
    union of all previously aligned polygons. Returns a new dict; input geoms
    are not modified. Geometries are made valid after snapping (vertex moves
    can introduce self-intersections).
    """
    out = {}
    ref = None
    for sid in sorted(bounds_by_site):
        g = bounds_by_site[sid]
        if g is None or g.is_empty:
            out[sid] = g
            continue
        if ref is not None:
            g = keep_all_parts(make_valid(snap(g, ref, tol_ft)))
        out[sid] = g
        ref = g if ref is None else unary_union([ref, g])
    return out


def enforce_containment(bounds_by_site: dict, min_frac: float = 0.8) -> dict:
    """
    Where one sewershed is (almost) nested in another, make the nesting exact.

    An upstream site's catchment is physically a subset of its downstream
    neighbour's, but two independently smoothed boundaries let the inner one
    poke out. For every pair whose overlap covers >= min_frac of the smaller
    polygon, the larger is unioned with the smaller so containment holds
    exactly. Merely-adjacent pairs (small fractional overlap) are untouched.

    Returns a new dict; deterministic (pairs visited in sorted order).
    """
    out = dict(bounds_by_site)
    ids = sorted(out)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            ga, gb = out[a], out[b]
            if ga is None or gb is None or ga.is_empty or gb.is_empty:
                continue
            inter = ga.intersection(gb).area
            if inter == 0:
                continue
            small, big = (a, b) if ga.area <= gb.area else (b, a)
            if inter / out[small].area >= min_frac:
                out[big] = keep_all_parts(
                    make_valid(unary_union([out[big], out[small]])))
    return out


def snap_to_blocks(geom, blocks_gdf, min_frac: float = 0.5):
    """
    Snap a boundary polygon to the census-block fabric: replace it with the
    union of blocks whose coverage fraction (area of block inside `geom` /
    block area) >= min_frac.

    Blocks tile the county, so two adjacent sewersheds snapped to the same
    fabric share seams exactly (same block edge on both sides) — the per-site
    morph_close wobble along shared borders disappears. As a bonus, a
    block-snapped polygon makes the demographic join exact block sums instead
    of areal weighting.

    min_frac trades off overshoot (low values grab barely-touched blocks)
    against dropping real served area (high values discard blocks the smooth
    boundary only partly covers) — pick it empirically against the truth set.

    Returns the snapped Polygon/MultiPolygon (holes filled, all parts kept),
    or None if no block clears the threshold or `geom` is None/empty.
    """
    if geom is None or geom.is_empty:
        return None
    if blocks_gdf is None or blocks_gdf.empty:
        return None
    idx = blocks_gdf.sindex.query(geom, predicate="intersects")
    keep = []
    for blk in blocks_gdf.iloc[sorted(idx)].geometry:
        if blk is None or blk.is_empty or blk.area == 0:
            continue
        if blk.intersection(geom).area / blk.area >= min_frac:
            keep.append(blk)
    if not keep:
        return None
    return _finalize(unary_union(keep))


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def build_boundary(served_gdf, method, *, close_ft=100.0, concave_ratio=0.3,
                   blocks_gdf=None, served_union=None, block_fill_frac=0.5,
                   delaunay_max_edge_ft=1000.0):
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
    elif method == "block_fill":
        raw = block_fill(union, blocks_gdf, block_fill_frac)
    elif method == "delaunay":
        raw = delaunay_fill(union, delaunay_max_edge_ft)
    else:  # concave
        raw = concave(union, concave_ratio)

    return _finalize(raw)
