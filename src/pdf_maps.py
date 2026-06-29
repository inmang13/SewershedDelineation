"""
PDF map generation for QA flags.
One page per flag, zoomed to context buffer, with flow direction arrows.
"""

import math
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")  # non-interactive backend — headless PDF output
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from shapely.geometry import box, Point


def generate_qa_maps(
    pipes: gpd.GeoDataFrame,
    manholes: gpd.GeoDataFrame,
    flags: list[dict],
    output_path: str,
    context_buffer_ft: float,
) -> None:
    """
    Write a multi-page PDF with one map per flag.

    Args:
        pipes            Repaired pipes GeoDataFrame (with QA_STATUS field)
        manholes         Manholes GeoDataFrame for node labels
        flags            List of flag dicts from network_qa.run_qa()
        output_path      Path for the output PDF
        context_buffer_ft  Zoom window half-width in feet
    """
    if not flags:
        print("No flags to map.")
        return

    with PdfPages(output_path) as pdf:
        for i, flag in enumerate(flags):
            fig = _make_flag_page(pipes, manholes, flag, context_buffer_ft, i + 1, len(flags))
            pdf.savefig(fig)
            plt.close(fig)
            if (i + 1) % 100 == 0:
                print(f"  ...PDF page {i + 1}/{len(flags)}", flush=True)

    print(f"Flag maps written to {output_path} ({len(flags)} pages)")


def _make_flag_page(
    pipes: gpd.GeoDataFrame,
    manholes: gpd.GeoDataFrame,
    flag: dict,
    context_buffer_ft: float,
    page_num: int,
    total_pages: int,
) -> plt.Figure:
    loc: Point = flag["geometry"]
    buf = context_buffer_ft

    # Clip data to the view window
    window = box(loc.x - buf, loc.y - buf, loc.x + buf, loc.y + buf)
    pipes_clip = pipes[pipes.intersects(window)].copy()
    mh_clip = manholes[manholes.intersects(window)].copy()

    fig, ax = plt.subplots(figsize=(11, 8.5))

    # --- Base pipe layer ---
    if not pipes_clip.empty:
        # Normal pipes
        normal = pipes_clip[pipes_clip["QA_STATUS"] == "original"]
        if not normal.empty:
            normal.plot(ax=ax, color="#999999", linewidth=0.8, zorder=2)

        # Direction-inferrable pipes (null FROMMH/TOMH, fixable in Phase 3)
        inferrable = pipes_clip[pipes_clip["QA_STATUS"] == "direction_inferrable"]
        if not inferrable.empty:
            inferrable.plot(ax=ax, color="#2196F3", linewidth=1.2, zorder=3)

        # Flagged pipes
        flagged = pipes_clip[pipes_clip["QA_STATUS"] == "flagged"]
        if not flagged.empty:
            flagged.plot(ax=ax, color="#FF9800", linewidth=1.5, zorder=4)

        # Flow direction arrows on visible pipes
        _draw_direction_arrows(ax, pipes_clip)

    # --- Manholes ---
    if not mh_clip.empty:
        mh_clip.plot(ax=ax, color="#555555", markersize=4, zorder=5)
        for _, mh in mh_clip.iterrows():
            ax.annotate(
                str(mh["FACILITYID"]),
                xy=(mh.geometry.x, mh.geometry.y),
                xytext=(4, 4),
                textcoords="offset points",
                fontsize=5,
                color="#333333",
                zorder=6,
            )

    # --- Flag location marker ---
    severity_color = "#D32F2F" if flag["severity"] == "review_required" else "#F57C00"
    ax.plot(loc.x, loc.y, marker="*", color=severity_color, markersize=18, zorder=10,
            markeredgecolor="white", markeredgewidth=0.8)

    # --- Axes formatting ---
    ax.set_xlim(loc.x - buf, loc.x + buf)
    ax.set_ylim(loc.y - buf, loc.y + buf)
    ax.set_aspect("equal")
    ax.tick_params(labelsize=7)
    ax.set_xlabel("Easting (ft, EPSG:2264)", fontsize=7)
    ax.set_ylabel("Northing (ft, EPSG:2264)", fontsize=7)

    # --- Scale bar ---
    _draw_scale_bar(ax, loc.x - buf * 0.9, loc.y - buf * 0.88, scale_ft=500)

    # --- Legend ---
    legend_elements = [
        Line2D([0], [0], color="#999999", linewidth=1.5, label="Pipe — no issues"),
        Line2D([0], [0], color="#2196F3", linewidth=1.5, label="Pipe — direction inferrable (fix in Phase 3)"),
        Line2D([0], [0], color="#FF9800", linewidth=1.5, label="Pipe — flagged"),
        Line2D([0], [0], marker="*", color="w", markerfacecolor=severity_color,
               markersize=12, label="Flag location"),
    ]
    ax.legend(handles=legend_elements, loc="lower right", fontsize=7, framealpha=0.9)

    # --- Title block ---
    severity_label = "REVIEW REQUIRED" if flag["severity"] == "review_required" else "WARNING"
    title = (
        f"[{severity_label}]  {flag['flag_type'].replace('_', ' ').upper()}\n"
        f"Pipe ID: {flag['pipe_id']}  |  Page {page_num} of {total_pages}\n"
        f"{flag['description']}"
    )
    ax.set_title(title, fontsize=8, loc="left", pad=10, wrap=True)

    fig.tight_layout()
    return fig


def generate_sewershed_map(
    served: gpd.GeoDataFrame,
    buffer_geom,
    pipes_sub: gpd.GeoDataFrame,
    target_xy,
    flags: list[dict],
    manhole_id: str,
    area_acres: float,
    output_path: str,
) -> None:
    """
    Write a one-page overview PDF for a delineated sewershed (Phase 6).

    Unlike generate_qa_maps (one zoomed page per pipe-level flag), this is a single
    catchment-wide page: served parcels, the pipe buffer outline, the contributing
    pipes, and the target manhole, with any delineation flags listed in the title.

    Args:
        served       served parcels GeoDataFrame (PopulationResult.served)
        buffer_geom  dissolved pipe buffer geometry (PopulationResult.buffer)
        pipes_sub    contributing pipes (build_debug_pipes_gdf), may be None/empty
        target_xy    (x, y) of the target manhole in the working CRS
        flags        delineation flag dicts from polygon_output.compute_flags
        manhole_id   target manhole id for the title
        area_acres   dissolved sewershed area (already computed; shown in the title)
        output_path  path for the output PDF
    """
    fig, ax = plt.subplots(figsize=(11, 8.5))

    if not served.empty:
        served.plot(ax=ax, facecolor="#C8E6C9", edgecolor="#7CB342",
                    linewidth=0.3, zorder=2)

    if buffer_geom is not None and not buffer_geom.is_empty:
        gpd.GeoSeries([buffer_geom], crs=served.crs).boundary.plot(
            ax=ax, color="#1565C0", linewidth=0.8, zorder=3)

    if pipes_sub is not None and not pipes_sub.empty:
        pipes_sub.plot(ax=ax, color="#37474F", linewidth=0.7, zorder=4)

    tx, ty = target_xy
    ax.plot(tx, ty, marker="*", color="#D32F2F", markersize=18, zorder=6,
            markeredgecolor="white", markeredgewidth=0.8)

    # Frame to the served extent (fall back to the buffer, then the manhole) with a
    # small margin so the boundary isn't flush to the axes.
    if not served.empty:
        minx, miny, maxx, maxy = served.total_bounds
    elif buffer_geom is not None and not buffer_geom.is_empty:
        minx, miny, maxx, maxy = buffer_geom.bounds
    else:
        minx, miny, maxx, maxy = tx, ty, tx, ty
    pad = max(maxx - minx, maxy - miny, 200.0) * 0.05
    ax.set_xlim(minx - pad, maxx + pad)
    ax.set_ylim(miny - pad, maxy + pad)
    ax.set_aspect("equal")
    ax.tick_params(labelsize=7)
    ax.set_xlabel("Easting (ft, EPSG:2264)", fontsize=7)
    ax.set_ylabel("Northing (ft, EPSG:2264)", fontsize=7)

    _draw_scale_bar(ax, minx - pad + (maxx - minx) * 0.04,
                    miny - pad + (maxy - miny) * 0.04, scale_ft=1000)

    legend_elements = [
        mpatches.Patch(facecolor="#C8E6C9", edgecolor="#7CB342", label="Served parcel"),
        Line2D([0], [0], color="#1565C0", linewidth=1.5, label="Pipe buffer edge"),
        Line2D([0], [0], color="#37474F", linewidth=1.5, label="Contributing pipe"),
        Line2D([0], [0], marker="*", color="w", markerfacecolor="#D32F2F",
               markersize=12, label="Target manhole"),
    ]
    ax.legend(handles=legend_elements, loc="lower right", fontsize=7, framealpha=0.9)

    if flags:
        flag_line = "  |  ".join(
            f"{f['flag_type'].replace('_', ' ').upper()} ({f['severity']})"
            for f in flags
        )
    else:
        flag_line = "no flags"
    title = (
        f"SEWERSHED — manhole {manhole_id}\n"
        f"{len(served):,} parcels  |  {area_acres:,.0f} acres\n"
        f"Flags: {flag_line}"
    )
    ax.set_title(title, fontsize=8, loc="left", pad=10, wrap=True)

    fig.tight_layout()
    with PdfPages(output_path) as pdf:
        pdf.savefig(fig)
    plt.close(fig)
    print(f"Sewershed map written to {output_path}")


def _draw_direction_arrows(ax: plt.Axes, pipes: gpd.GeoDataFrame) -> None:
    """Draw a small arrow at the midpoint of each pipe showing flow direction."""
    for _, row in pipes.iterrows():
        coords = list(row.geometry.coords)
        if len(coords) < 2:
            continue
        # Use midpoint segment for arrow direction
        mid_idx = len(coords) // 2
        x0, y0 = coords[mid_idx - 1]
        x1, y1 = coords[mid_idx]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        if length == 0:
            continue
        # Arrow at midpoint, scaled to ~80 ft
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        scale = 80 / length
        ax.annotate(
            "",
            xy=(mx + dx * scale * 0.5, my + dy * scale * 0.5),
            xytext=(mx - dx * scale * 0.5, my - dy * scale * 0.5),
            arrowprops=dict(arrowstyle="->", color="#444444", lw=0.6),
            zorder=7,
        )


def _draw_scale_bar(ax: plt.Axes, x: float, y: float, scale_ft: int = 500) -> None:
    """Draw a simple scale bar."""
    ax.plot([x, x + scale_ft], [y, y], color="black", linewidth=2, zorder=8)
    ax.plot([x, x], [y - 20, y + 20], color="black", linewidth=1.5, zorder=8)
    ax.plot([x + scale_ft, x + scale_ft], [y - 20, y + 20], color="black", linewidth=1.5, zorder=8)
    ax.text(x + scale_ft / 2, y + 40, f"{scale_ft} ft",
            ha="center", va="bottom", fontsize=7, zorder=8)
