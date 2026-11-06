#!/usr/bin/env python3
# ruff: noqa: E402
# pyright: reportMissingImports=false
"""Clip high-resolution aerial orthoimagery per field from an ArcGIS ImageServer.

Opt-in companion to the keyless Sentinel-2/Landsat archive (not part of
run_farm_pipeline.py). Defaults target the Arkansas GIS Office 2023 ADOP
Nine Inch Ortho (free public service): 20 cm RGB, flown leaf-off
2023-01-19..2023-02-28 by Sanborn. The service exposes 3 bands (RGB); the
original 4-band NIR delivery is download-only, so no NDVI is produced here.

Clips are exported in a true-metre projected CRS (default EPSG:26915, not the
service's Web Mercator), snapped to the pixel grid, field + buffer, split into
strips under the export limit, and written as JPEG-compressed GeoTIFF.

Outputs per field (runtime tree, never Git):
    fields/<field>/satellite/aerial/<label>_rgb.tif
    fields/<field>/satellite/aerial/manifest.json
    fields/<field>/derived/reports/aerial_<label>.png
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from bootstrap_runtime import ensure_runtime_environment

ensure_runtime_environment()

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.features import geometry_mask

from arcgis_imageserver import export_clip, new_session, service_info, sha256_file
from paths import farm_boundary_path, farm_manifest_dir, field_reports_dir, field_satellite_dir
from reporting_bootstrap import field_slug_map_from_inventory

DEFAULT_SERVICE = "https://gis.arkansas.gov/arcgis/rest/services/ImageServices/IMAGERY_9IN_2023/ImageServer"
DEFAULT_LABEL = "agio_adop_2023"
DEFAULT_ACQUIRED = "2023-01-19/2023-02-28"
DEFAULT_ATTRIBUTION = (
    "Arkansas GIS Office (gis.arkansas.gov), 2023 ADOP Nine Inch Ortho, 20 cm, produced by Sanborn Map Company"
)
DEFAULT_NOTES = "Leaf-off winter flight; RGB only via the service (source delivery is 4-band R,G,B,NIR)."


def _export(session, args: argparse.Namespace, bounds: tuple[float, ...], target: Path) -> list[str]:
    return export_clip(
        session,
        args.service.rstrip("/"),
        bounds,
        pixel=args.pixel_size,
        wkid=args.crs,
        target=target,
        pixel_type="U8",
        nodata=None,
        interpolation="RSP_BilinearInterpolation",
        compress=args.compress,
        max_rows=args.block_pixels,
        max_cols=args.block_pixels,
    )


def _coverage_pct(path: Path, field_geom) -> float:
    with rasterio.open(path) as src:
        inside = geometry_mask([field_geom.__geo_interface__], (src.height, src.width), src.transform, invert=True)
        data = src.read()
    valid = inside & (data.max(axis=0) > 0)
    return round(100.0 * valid.sum() / max(1, inside.sum()), 2)


def _preview(path: Path, field_geom, png: Path, title: str, max_pixels: int = 1600) -> None:
    with rasterio.open(path) as src:
        scale = max(1, int(np.ceil(max(src.width, src.height) / max_pixels)))
        data = src.read(
            out_shape=(src.count, src.height // scale, src.width // scale),
            resampling=rasterio.enums.Resampling.average,
        )
        bounds = src.bounds
    rgb = np.moveaxis(data[:3], 0, -1)
    fig, ax = plt.subplots(figsize=(9, 9 * rgb.shape[0] / max(1, rgb.shape[1])))
    ax.imshow(rgb, extent=(bounds.left, bounds.right, bounds.bottom, bounds.top))
    gpd.GeoSeries([field_geom]).boundary.plot(ax=ax, color="yellow", linewidth=1.5)
    ax.set_title(title, fontsize=10)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_xlabel("")
    ax.set_ylabel("")
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(png, dpi=110)
    plt.close(fig)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--grower", default=os.environ.get("AG_GROWER_SLUG", "default-grower"))
    parser.add_argument("--farm", default=os.environ.get("AG_FARM_SLUG", "default-farm"))
    parser.add_argument("--service", default=DEFAULT_SERVICE, help="ArcGIS ImageServer URL")
    parser.add_argument("--label", default=DEFAULT_LABEL, help="Dataset label used in file names")
    parser.add_argument("--acquired", default=DEFAULT_ACQUIRED, help="Acquisition date or range (ISO)")
    parser.add_argument("--attribution", default=DEFAULT_ATTRIBUTION)
    parser.add_argument("--notes", default=DEFAULT_NOTES)
    parser.add_argument("--pixel-size", type=float, default=0.2, help="Output pixel size in metres")
    parser.add_argument("--crs", type=int, default=26915, help="Projected EPSG code for the clips (metres)")
    parser.add_argument("--buffer-meters", type=float, default=20.0)
    parser.add_argument("--compress", choices=["jpeg", "deflate"], default="jpeg")
    parser.add_argument(
        "--block-pixels",
        type=int,
        default=2048,
        help="Max export block edge in pixels (AGIO returns HTTP 500 for ~13+ Mpx reprojected requests)",
    )
    parser.add_argument("--field", action="append", help="Only these field slugs (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Print planned clips, no downloads")
    parser.add_argument("--force", action="store_true", default=os.environ.get("AG_FORCE") == "1")
    args = parser.parse_args(argv)

    boundary = farm_boundary_path(args.grower, args.farm)
    if not boundary.exists():
        sys.exit(f"ERROR: farm boundaries not found: {boundary}")
    fields = gpd.read_file(boundary).to_crs(args.crs)
    slugs = field_slug_map_from_inventory(farm_manifest_dir(args.grower, args.farm) / "field-inventory.csv")
    session = new_session()
    service = service_info(session, args.service.rstrip("/"))

    print("=" * 60)
    print(f"Aerial orthoimagery ({args.label}): {args.grower}/{args.farm}")
    print("=" * 60)
    done = 0
    failures: list[str] = []
    for _, field in fields.sort_values("field_id").iterrows():
        field_slug = slugs.get(str(field["field_id"]))
        if not field_slug or (args.field and field_slug not in args.field):
            continue
        bounds = field.geometry.buffer(args.buffer_meters).bounds
        width_px = (bounds[2] - bounds[0]) / args.pixel_size
        height_px = (bounds[3] - bounds[1]) / args.pixel_size
        out_dir = field_satellite_dir(args.grower, args.farm, field_slug) / "aerial"
        target = out_dir / f"{args.label}_rgb.tif"
        manifest_path = out_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        datasets = {entry["label"]: entry for entry in manifest.get("datasets", [])}
        previous = datasets.get(args.label)
        if args.dry_run:
            print(f"  plan  {field_slug:<24} {width_px:,.0f} x {height_px:,.0f} px at {args.pixel_size} m")
            continue
        if not args.force and previous and target.exists() and previous.get("sha256") == sha256_file(target):
            print(f"  skip  {field_slug:<24} (manifest+file reusable)")
            continue
        try:
            urls = _export(session, args, bounds, target)
        except Exception as exc:
            failures.append(field_slug)
            print(f"  FAIL  {field_slug:<24} {exc.__class__.__name__}: {str(exc)[:160]}")
            continue
        coverage = _coverage_pct(target, field.geometry)
        png = field_reports_dir(args.grower, args.farm, field_slug) / f"aerial_{args.label}.png"
        _preview(target, field.geometry, png, f"{field.get('field_name', field_slug)} — {args.label} ({args.acquired}, {args.pixel_size} m)")
        datasets[args.label] = {
            "label": args.label,
            "file": target.name,
            "preview": str(png),
            "service_url": args.service.rstrip("/"),
            "service_name": service["name"],
            "service_bands": service["band_count"],
            "request_urls": urls,
            "crs": f"EPSG:{args.crs}",
            "pixel_size_m": args.pixel_size,
            "buffer_m": args.buffer_meters,
            "compression": args.compress,
            "acquired": args.acquired,
            "attribution": args.attribution,
            "notes": args.notes,
            "field_coverage_pct": coverage,
            "bytes": target.stat().st_size,
            "sha256": sha256_file(target),
            "downloaded_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "dataset_name": "aerial_orthoimagery",
                    "field_id": str(field["field_id"]),
                    "field_slug": field_slug,
                    "datasets": [datasets[key] for key in sorted(datasets)],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        done += 1
        print(f"  ok    {field_slug:<24} coverage {coverage}%  {target.stat().st_size / 1e6:.1f} MB")
    print(f"\n{'Planned' if args.dry_run else 'Done'}: {done} new clip(s)")
    if failures:
        sys.exit(f"Failed fields ({len(failures)}): {', '.join(failures)}")


if __name__ == "__main__":
    main()
