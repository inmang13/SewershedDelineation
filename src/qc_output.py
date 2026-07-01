"""
Phase 8 — QC flags as a spatial layer.

Both flag streams already carry a `geometry`, but they only ship as CSV, which is
painful to cross-reference against GIS. This writes them to a multi-layer
GeoPackage so the maintainer can filter by flag type and zoom straight to the
problem. The CSVs stay — this is an additional output, not a replacement.

Two flag schemas are normalized into one:
  - network flags (network_qa.run_qa)      key `pipe_id`     -> source "network"
  - delineation flags (polygon_output)     key `manhole`     -> source "delineation"
Both also carry `flag_type`, `severity`, `description`, `geometry`.

Layers are grouped by `flag_type` and prefixed by source (`net_` / `delin_`) so
the two streams never collide in one file, and each layer holds a single geometry
type (GeoPackage requires it — a mixed group is split by geometry type).
"""

import re
from pathlib import Path

import geopandas as gpd

# Field names kept short and DBF-safe even though GPKG allows long names, so the
# layer is portable if ever exported to shapefile.
QC_FIELDS = ["source", "feat_id", "flag_type", "severity", "descr"]


def _normalize(flags: list[dict]) -> list[dict]:
    """Map both flag schemas onto a common record set (drops flags with no geometry)."""
    records = []
    for f in flags:
        geom = f.get("geometry")
        if geom is None or geom.is_empty:
            continue
        is_network = "pipe_id" in f
        records.append({
            "source":    "network" if is_network else "delineation",
            "feat_id":   str(f.get("pipe_id", f.get("manhole", ""))),
            "flag_type": f.get("flag_type", ""),
            "severity":  f.get("severity", ""),
            "descr":     f.get("description", ""),
            "geometry":  geom,
        })
    return records


def _layer_name(source: str, flag_type: str, geom_type: str, split: bool) -> str:
    """A GPKG-safe layer name: `<net|delin>_<flag_type>[_<geomtype>]`."""
    prefix = "net" if source == "network" else "delin"
    base = re.sub(r"[^0-9A-Za-z]+", "_", f"{prefix}_{flag_type}").strip("_").lower()
    return f"{base}_{geom_type.lower()}" if split else base


def write_qc_flags_gpkg(flags: list[dict], path, crs, replace: bool = False) -> list[str]:
    """
    Write flags to a GeoPackage, one layer per (source, flag_type), split further
    by geometry type when a group is mixed.

    Parameters
    ----------
    flags    list of flag dicts (network and/or delineation).
    path     output .gpkg path.
    crs      working CRS for the layers.
    replace  True deletes any existing file first (a fresh network-QA run);
             False appends layers to an existing file (delineation run after QA).
             Source-prefixed layer names keep the two streams from colliding.

    Returns the list of layer names written.
    """
    path = Path(path)
    if replace and path.exists():
        path.unlink()

    records = _normalize(flags)
    if not records:
        return []

    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=crs)
    written = []
    for (source, flag_type), grp in gdf.groupby(["source", "flag_type"]):
        geom_types = grp.geometry.geom_type.unique()
        split = len(geom_types) > 1
        for gtype in geom_types:
            sub = grp[grp.geometry.geom_type == gtype]
            layer = _layer_name(source, flag_type, gtype, split)
            sub[QC_FIELDS + ["geometry"]].to_file(path, layer=layer, driver="GPKG")
            written.append(layer)
    return written
