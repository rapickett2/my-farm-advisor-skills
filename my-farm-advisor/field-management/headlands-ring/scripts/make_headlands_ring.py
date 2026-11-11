#!/usr/bin/env python3
"""Build a headlands ring for one field boundary and write two GeoPackages.

Workflow (EPSG:4326 in, UTM metric workspace, EPSG:4326 out):

1. Read the field boundary as ``original_gdf`` and inspect its CRS before any
   reprojection (CRS84 / EPSG:4326 is used as-is; anything else is converted
   to EPSG:4326 once and logged; a missing CRS is an error).
2. Copy ``original_gdf`` to a UTM working GeoDataFrame (zone from the field).
3. Calculate the full boundary area in UTM: ``meters_squared`` + ``acres``.
4. Write both area attributes back to ``original_gdf``.
5. Create a negative ``--width-m`` (default 21 m) inner buffer.
6. Difference: full boundary - inner buffer = headlands ring.
7. Calculate the headlands ring area: ``meters_squared`` + ``acres``.
8. Convert ONLY the headlands ring back to EPSG:4326.
9. Write ``field_boundary.gpkg`` and ``headlands_ring.gpkg`` (one row each,
   columns ``geometry``, ``meters_squared``, ``acres``).

Existing outputs are never overwritten unless ``--force`` is given.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
from pyproj import CRS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from headlands_ring import ACRES_PER_SQM, create_headlands_ring  # noqa: E402

WGS84 = CRS.from_epsg(4326)
OUTPUT_COLUMNS = ["meters_squared", "acres", "geometry"]
FIELD_FILENAME = "field_boundary.gpkg"
RING_FILENAME = "headlands_ring.gpkg"


def describe_crs(crs: CRS | None) -> str:
    if crs is None:
        return "none"
    epsg = crs.to_epsg()
    authority = crs.to_authority()
    label = f"EPSG:{epsg}" if epsg else (":".join(authority) if authority else "custom")
    return f"{label} ({crs.name})"


def is_wgs84_lonlat(crs: CRS) -> bool:
    """True for EPSG:4326 and OGC CRS84 (same WGS 84 datum, lon/lat handled by geopandas)."""
    if crs.to_epsg() == 4326:
        return True
    return crs.to_authority() == ("OGC", "CRS84") or crs.equals(CRS.from_user_input("OGC:CRS84"))


def read_original_boundary(path: Path) -> tuple[gpd.GeoDataFrame, list[str]]:
    """Step 1: read the boundary and resolve its CRS to EPSG:4326 before anything else."""
    log: list[str] = []
    gdf = gpd.read_file(path)
    log.append(f"input: {path}")
    log.append(f"input CRS (as read): {describe_crs(gdf.crs)}")

    if gdf.crs is None:
        raise ValueError(f"{path} has no CRS; refusing to guess. Assign the correct CRS first.")
    if len(gdf) != 1:
        raise ValueError(f"{path} must contain exactly one field row, found {len(gdf)}")

    geom = gdf.geometry.iloc[0]
    if geom is None or geom.is_empty:
        raise ValueError(f"{path} has an empty geometry")
    if geom.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"{path} geometry must be a Polygon, found {geom.geom_type}")
    if not geom.is_valid:
        raise ValueError(f"{path} geometry is not valid; repair it before building headlands")
    if gdf.has_z.any():
        gdf = gdf.set_geometry(gdf.geometry.force_2d())
        log.append("dropped Z coordinates (2D geometry required)")

    if is_wgs84_lonlat(gdf.crs):
        log.append("input is WGS 84 lon/lat; used as original_gdf without reprojection")
        gdf = gdf.set_crs(WGS84, allow_override=True)
    else:
        log.append(f"input is not EPSG:4326; converted {describe_crs(gdf.crs)} -> EPSG:4326 once")
        gdf = gdf.to_crs(WGS84)

    original_gdf = gpd.GeoDataFrame(geometry=[gdf.geometry.iloc[0]], crs=WGS84)
    return original_gdf, log


def area_attributes(gdf_projected: gpd.GeoDataFrame) -> tuple[float, float]:
    meters_squared = float(gdf_projected.geometry.area.sum())
    return meters_squared, meters_squared * ACRES_PER_SQM


def build_headlands(
    original_gdf: gpd.GeoDataFrame, width_m: float
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, CRS, list[str]]:
    log: list[str] = []

    # Step 2: copy to UTM metric workspace.
    utm_crs = original_gdf.estimate_utm_crs()
    utm_gdf = original_gdf.copy().to_crs(utm_crs)
    log.append(f"UTM workspace: {describe_crs(utm_crs)}")

    # Steps 3-4: full boundary area, written back to original_gdf (still EPSG:4326).
    field_sqm, field_acres = area_attributes(utm_gdf)
    original_gdf["meters_squared"] = field_sqm
    original_gdf["acres"] = field_acres

    # Steps 5-6: negative inner buffer, then full boundary - inner buffer.
    ring_utm = create_headlands_ring(utm_gdf, width_m=width_m)
    inner = utm_gdf.geometry.iloc[0].buffer(-width_m)
    if inner.is_empty:
        log.append(f"field is narrower than 2 x {width_m} m; whole field is headlands")
    if ring_utm.empty:
        raise ValueError("headlands ring is empty")
    ring_utm = gpd.GeoDataFrame(geometry=[ring_utm.geometry.union_all()], crs=utm_crs)

    # Step 7: ring area in UTM.
    ring_sqm, ring_acres = area_attributes(ring_utm)
    ring_utm["meters_squared"] = ring_sqm
    ring_utm["acres"] = ring_acres

    # Step 8: only the ring goes back to EPSG:4326.
    ring_gdf = ring_utm.to_crs(WGS84)

    log.append(f"field: {field_sqm:,.2f} m2 = {field_acres:,.4f} ac")
    log.append(f"inner ({width_m} m buffer): {inner.area:,.2f} m2")
    log.append(
        f"headlands ring: {ring_sqm:,.2f} m2 = {ring_acres:,.4f} ac "
        f"({ring_sqm / field_sqm * 100:.1f}% of field)"
    )
    return original_gdf[OUTPUT_COLUMNS], ring_gdf[OUTPUT_COLUMNS], utm_crs, log


def write_outputs(
    field_gdf: gpd.GeoDataFrame, ring_gdf: gpd.GeoDataFrame, out_dir: Path, force: bool
) -> tuple[Path, Path]:
    """Step 9: write two single-row GeoPackages without clobbering existing files."""
    field_path = out_dir / FIELD_FILENAME
    ring_path = out_dir / RING_FILENAME
    existing = [p for p in (field_path, ring_path) if p.exists()]
    if existing and not force:
        names = ", ".join(str(p) for p in existing)
        raise FileExistsError(f"refusing to overwrite existing output(s): {names} (use --force)")
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in (field_path, ring_path):
        if path.exists():
            path.unlink()
    field_gdf.to_file(field_path, layer="field_boundary", driver="GPKG")
    ring_gdf.to_file(ring_path, layer="headlands_ring", driver="GPKG")
    return field_path, ring_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, type=Path, help="one-field boundary file")
    parser.add_argument("--out-dir", required=True, type=Path, help="output folder for .gpkg files")
    parser.add_argument("--width-m", type=float, default=21.0, help="headlands width in meters")
    parser.add_argument("--force", action="store_true", help="overwrite existing outputs")
    args = parser.parse_args(argv)

    if args.width_m <= 0:
        parser.error("--width-m must be positive")

    original_gdf, log = read_original_boundary(args.input)
    field_gdf, ring_gdf, _, build_log = build_headlands(original_gdf, args.width_m)
    field_path, ring_path = write_outputs(field_gdf, ring_gdf, args.out_dir, args.force)

    for line in log + build_log:
        print(line)
    print(f"wrote: {field_path}")
    print(f"wrote: {ring_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
