#!/usr/bin/env python3
"""Draw pictures for a headlands ring built by make_headlands_ring.py.

Reads ``field_boundary.gpkg`` and ``headlands_ring.gpkg`` (EPSG:4326) from ``--dir`` and writes
two PNGs next to them:

- ``<prefix>_headlands_overview.png``: metric map of the field, headlands ring, and inner buffer
  line with area labels.
- ``<prefix>_headlands_workflow_steps.png``: six panels following the workshop steps 1-9.

The interior is taken from the ring's hole(s), so the pictures need no width argument. Existing
pictures are never overwritten unless ``--force`` is given.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from shapely.geometry import Point, Polygon  # noqa: E402
from shapely.ops import nearest_points, unary_union  # noqa: E402

ACRES_PER_SQM = 0.0002471053814671653
FIELD_FILE = "field_boundary.gpkg"
RING_FILE = "headlands_ring.gpkg"
RING_FILL, RING_EDGE, OUTLINE, FIELD_FILL = "#ffe500", "#8a7d00", "#1b5e20", "#e8e8f5"
DASH = (0, (4, 3))


def picture_paths(folder: Path, title: str | None, prefix: str | None) -> tuple[str, Path, Path]:
    title = title or folder.resolve().name
    prefix = prefix or "".join(c if c.isalnum() else "_" for c in title.lower()).strip("_")
    return (
        title,
        folder / f"{prefix}_headlands_overview.png",
        folder / f"{prefix}_headlands_workflow_steps.png",
    )


def read_inputs(folder: Path) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    field = gpd.read_file(folder / FIELD_FILE)
    ring = gpd.read_file(folder / RING_FILE)
    for name, gdf in ((FIELD_FILE, field), (RING_FILE, ring)):
        if gdf.crs is None or gdf.crs.to_epsg() != 4326:
            raise ValueError(f"{name} must be EPSG:4326, found {gdf.crs}")
        if len(gdf) != 1:
            raise ValueError(f"{name} must have exactly one row, found {len(gdf)}")
        missing = {"meters_squared", "acres"} - set(gdf.columns)
        if missing:
            raise ValueError(f"{name} is missing columns: {sorted(missing)}")
    return field, ring


def ring_interior(ring_u: gpd.GeoDataFrame, utm) -> gpd.GeoSeries:
    """Interior = the holes of the ring (no sliver artifacts from a field - ring difference)."""
    geom = ring_u.geometry.iloc[0]
    polygons = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
    holes = [Polygon(hole) for poly in polygons for hole in poly.interiors]
    return gpd.GeoSeries([unary_union(holes)] if holes else [Polygon()], crs=utm)


def draw_overview(field, ring, field_u, ring_u, interior_u, utm_epsg, title, path, dpi) -> None:
    f_m2, f_ac = field["meters_squared"].iloc[0], field["acres"].iloc[0]
    r_m2, r_ac = ring["meters_squared"].iloc[0], ring["acres"].iloc[0]
    fig, ax = plt.subplots(figsize=(11, 7.5))
    field_u.plot(ax=ax, color=FIELD_FILL, edgecolor="none")
    ring_u.plot(ax=ax, color=RING_FILL, edgecolor=RING_EDGE, linewidth=1)
    if not interior_u.is_empty.all():
        interior_u.boundary.plot(ax=ax, color="black", linewidth=1.2, linestyle=DASH)
    field_u.boundary.plot(ax=ax, color="black", linewidth=2.2)

    minx, miny, maxx, maxy = field_u.total_bounds
    label_xy = (minx + (maxx - minx) * 0.20, maxy - (maxy - miny) * 0.10)
    target = nearest_points(ring_u.geometry.iloc[0], Point(label_xy))[0]
    ax.annotate(
        f"{width_label(field_u, interior_u)}\nheadlands ring",
        xy=(target.x, target.y),
        xytext=label_xy,
        fontsize=12,
        fontweight="bold",
        ha="center",
        va="center",
        bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="black"),
        arrowprops=dict(arrowstyle="-|>", color="black", lw=1.5, shrinkB=0),
    )
    if not interior_u.is_empty.all():
        c = interior_u.geometry.iloc[0].representative_point()
        i_m2 = interior_u.area.iloc[0]
        ax.text(c.x, c.y, f"Interior\n{i_m2:,.0f} m\u00b2 \u00b7 {i_m2 * ACRES_PER_SQM:.2f} ac",
                ha="center", va="center", fontsize=12, color="#333")
    ax.legend(
        handles=[
            Patch(fc=RING_FILL, ec=RING_EDGE,
                  label=f"Headlands ring: {r_m2:,.0f} m\u00b2 \u00b7 {r_ac:.2f} ac ({r_m2 / f_m2 * 100:.1f}%)"),
            Line2D([], [], color="black", lw=2.2, label=f"Field boundary: {f_m2:,.0f} m\u00b2 \u00b7 {f_ac:.2f} ac"),
            Line2D([], [], color="black", lw=1.2, ls=DASH, label="Inner buffer"),
        ],
        loc="lower right", fontsize=10, frameon=True,
    )
    ax.set_title(f"{title} \u2014 headlands ring", fontsize=15, fontweight="bold")
    ax.set_aspect("equal")
    ax.set_xlabel(f"Easting (m) \u00b7 EPSG:{utm_epsg}", fontsize=9)
    ax.set_ylabel("Northing (m)", fontsize=9)
    ax.ticklabel_format(style="plain", useOffset=False)
    ax.tick_params(labelsize=8)
    plt.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def width_label(field_u, interior_u) -> str:
    """Estimate the ring width from area/perimeter of the field for the callout text."""
    if interior_u.is_empty.all():
        return "Whole field"
    inner = interior_u.geometry.iloc[0]
    outer = field_u.geometry.iloc[0]
    width = (outer.area - inner.area) / ((outer.length + inner.length) / 2)
    return f"~{round(width):d} m"


def draw_steps(field, ring, field_u, ring_u, interior_u, utm_epsg, title, path, dpi) -> None:
    f_m2, f_ac = field["meters_squared"].iloc[0], field["acres"].iloc[0]
    r_m2, r_ac = ring["meters_squared"].iloc[0], ring["acres"].iloc[0]
    i_m2 = float(interior_u.area.sum())
    wl = width_label(field_u, interior_u)

    def shade_interior(a):
        field_u.boundary.plot(ax=a, color=OUTLINE, linewidth=2)
        if not interior_u.is_empty.all():
            interior_u.plot(ax=a, color="#c7d2fe", edgecolor="black", linewidth=1.2, linestyle="--")

    panels = [
        ("1  Read boundary\nEPSG:4326 (degrees)",
         lambda a: field.plot(ax=a, color=FIELD_FILL, edgecolor=OUTLINE, linewidth=2), True),
        (f"2\u20134  UTM copy + full area\nEPSG:{utm_epsg}  \u00b7  {f_m2:,.0f} m\u00b2 \u00b7 {f_ac:.2f} ac",
         lambda a: field_u.plot(ax=a, color=FIELD_FILL, edgecolor=OUTLINE, linewidth=2), False),
        (f"5  Negative inner buffer ({wl})\ninner area {i_m2:,.0f} m\u00b2", shade_interior, False),
        (f"6\u20137  Full \u2212 inner = ring\n{r_m2:,.0f} m\u00b2 \u00b7 {r_ac:.2f} ac",
         lambda a: (ring_u.plot(ax=a, color=RING_FILL, edgecolor=RING_EDGE),
                    field_u.boundary.plot(ax=a, color=OUTLINE, linewidth=1.5)), False),
        ("8  Ring only \u2192 EPSG:4326\n(original_gdf stays 4326)",
         lambda a: (field.boundary.plot(ax=a, color=OUTLINE, linewidth=1.5),
                    ring.plot(ax=a, color=RING_FILL, edgecolor=RING_EDGE)), True),
        ("9  Two GeoPackages (1 row each)\nfield_boundary.gpkg + headlands_ring.gpkg",
         lambda a: (field.plot(ax=a, color=FIELD_FILL, edgecolor=OUTLINE, linewidth=2),
                    ring.plot(ax=a, color=RING_FILL, edgecolor=RING_EDGE, alpha=0.9)), True),
    ]
    lat = math.radians(float(field.total_bounds[[1, 3]].mean()))
    fig, axes = plt.subplots(2, 3, figsize=(16, 9.5))
    for ax, (panel_title, draw, geographic) in zip(axes.flat, panels):
        draw(ax)
        ax.set_title(panel_title, fontsize=11, fontweight="bold")
        ax.set_aspect(1 / math.cos(lat) if geographic else "equal")
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_color("#999")
    fig.suptitle(f"{title} \u2014 headlands ring workflow (workshop steps 1\u20139)",
                 fontsize=15, fontweight="bold")
    plt.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", required=True, type=Path,
                        help="folder holding field_boundary.gpkg and headlands_ring.gpkg")
    parser.add_argument("--title", default=None, help="picture title (default: folder name)")
    parser.add_argument("--prefix", default=None, help="output file prefix (default: slug of title)")
    parser.add_argument("--dpi", type=int, default=170)
    parser.add_argument("--force", action="store_true", help="overwrite existing pictures")
    args = parser.parse_args(argv)

    title, overview, steps = picture_paths(args.dir, args.title, args.prefix)
    existing = [p for p in (overview, steps) if p.exists()]
    if existing and not args.force:
        raise FileExistsError(
            f"refusing to overwrite existing picture(s): {', '.join(map(str, existing))} (use --force)"
        )

    field, ring = read_inputs(args.dir)
    utm = field.estimate_utm_crs()
    field_u, ring_u = field.to_crs(utm), ring.to_crs(utm)
    interior_u = ring_interior(ring_u, utm)

    draw_overview(field, ring, field_u, ring_u, interior_u, utm.to_epsg(), title, overview, args.dpi)
    draw_steps(field, ring, field_u, ring_u, interior_u, utm.to_epsg(), title, steps, args.dpi)
    print(f"wrote: {overview}")
    print(f"wrote: {steps}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileExistsError, ValueError) as exc:
        import sys

        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
