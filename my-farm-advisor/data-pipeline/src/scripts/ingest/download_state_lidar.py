#!/usr/bin/env python3
# ruff: noqa: E402
# pyright: reportMissingImports=false
"""Clip every lidar DEM collection covering each field from ArcGIS ImageServers.

Opt-in companion to download_dem_terrain.py for multi-epoch elevation work
(e.g. detecting land leveling between flights). For each field it lists the
primary rasters (Category=1) of each configured ImageServer that intersect the
buffered boundary, then exports each one separately with a lock-raster mosaic
rule so a clip always comes from exactly one named collection. Clips are raw
float32 elevation in the service's native projected CRS, snapped to the
collection's pixel grid, with no server-side resampling.

Defaults are chosen from the farm's state (``--preset`` overrides):

    arkansas     Arkansas GIS Office: DEM_HALF_METER_2026 (0.5 m, 2026) and
                 DEM_1M_2018 (1 m county collections, epoch parsed from each
                 collection name, e.g. Languille_2011). One clip per collection.
    missouri     MSDIS MO_Southeast11Counties_2024_B24 (USGS 3DEP QL1, 0.5 m,
                 spring 2024). Covers the Missouri Bootheel incl. Pemiscot.
    mississippi  MARIS/MDEQ statewide lidar mosaic 2019 (Delta tiles from the
                 USGS MS Delta project). Served in US survey feet on Web
                 Mercator; clips are requested in UTM 15N and converted to m.

Tiled services (Missouri, Mississippi) index individual tiles rather than whole
flights, so they run in mosaic mode: one clip per service per field, with the
service's own mosaic rule, instead of one clip per locked raster item.

Outputs per field (runtime tree, never Git):
    fields/<field>/terrain/state_lidar/<epoch>_<collection>.tif
    fields/<field>/terrain/state_lidar/manifest.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from bootstrap_runtime import ensure_runtime_environment

ensure_runtime_environment()

import geopandas as gpd
import numpy as np
import rasterio

from arcgis_imageserver import export_clip, new_session, request_json, service_info, sha256_file
from naming import slugify
from paths import farm_boundary_path, farm_manifest_dir, field_terrain_dir
from reporting_bootstrap import field_slug_map_from_inventory

ARKANSAS_BASE = "https://gis.arkansas.gov/arcgis/rest/services/ImageServices"
DEFAULT_SERVICES = (
    f"{ARKANSAS_BASE}/DEM_HALF_METER_2026/ImageServer",
    f"{ARKANSAS_BASE}/DEM_1M_2018/ImageServer",
)
DEFAULT_ATTRIBUTION = "Arkansas GIS Office (gis.arkansas.gov), statewide and county lidar DEMs"
US_SURVEY_FOOT_M = 1200.0 / 3937.0

# mode "collections": one locked clip per raster item (items are whole flights).
# mode "mosaic": one clip per service (items are tiles of a single flight).
PRESETS: dict[str, dict] = {
    "arkansas": {
        "attribution": DEFAULT_ATTRIBUTION,
        "services": [{"url": url, "mode": "collections"} for url in DEFAULT_SERVICES],
    },
    "missouri": {
        "attribution": (
            "Missouri Spatial Data Information Service (lidar.msdis.missouri.edu); "
            "USGS 3DEP MO_SE11County_B24 QL1 lidar"
        ),
        "services": [
            {
                "url": "https://lidar.msdis.missouri.edu/arcgis/rest/services/"
                "MO_Southeast11Counties_2024_B24_DEM_UTM/ImageServer",
                "mode": "mosaic",
                "epoch": "2024",
                "collection": "MO_SE11County_B24",
                "pixel_m": 0.5,
            }
        ],
    },
    "mississippi": {
        "attribution": (
            "MARIS / MDEQ statewide lidar mosaic 2019 (gis.mississippi.edu); "
            "Delta tiles from the USGS MS Delta lidar project"
        ),
        "services": [
            {
                "url": "https://gis.mississippi.edu/server/rest/services/Elevation/"
                "MS_Statewide_Lidar_Mosaic_2019_IS/ImageServer",
                "mode": "mosaic",
                "epoch": "2019",
                "collection": "MS_Statewide_Lidar_Mosaic_2019",
                "pixel_m": 1.0,
                # Web Mercator metres are not ground metres; request UTM 15N.
                "out_wkid": 26915,
                "z_to_m": US_SURVEY_FOOT_M,
            }
        ],
    },
}
STATE_PRESETS = {"AR": "arkansas", "MO": "missouri", "MS": "mississippi"}
NODATA = -9999.0
_YEAR_RE = re.compile(r"(19|20)\d{2}")




def _epoch_for(item_name: str, service_name: str) -> str:
    for text in (item_name, service_name):
        years = [m.group(0) for m in _YEAR_RE.finditer(text)]
        if years:
            return years[-1]
    return "unknown"


def _collections_for(
    session, service_url: str, bounds: tuple[float, ...], wkid: int
) -> list[dict]:
    x0, y0, x1, y1 = bounds
    envelope = {"xmin": x0, "ymin": y0, "xmax": x1, "ymax": y1, "spatialReference": {"wkid": wkid}}
    payload = request_json(
        session,
        f"{service_url}/query",
        {
            "geometry": json.dumps(envelope),
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "where": "Category=1",
            "outFields": "OBJECTID,Name,LowPS",
            "returnGeometry": "false",
        },
    )
    items = [feature["attributes"] for feature in payload.get("features", [])]
    return sorted(items, key=lambda item: (str(item.get("Name")), int(item["OBJECTID"])))





def _coverage(path: Path, field_geom) -> dict:
    from rasterio.mask import mask

    with rasterio.open(path) as src:
        clipped, _ = mask(src, [field_geom.__geo_interface__], crop=True, filled=False)
    values = clipped[0]
    inside = ~np.ma.getmaskarray(values)
    valid = inside & (values.filled(NODATA) > NODATA + 1)
    total = int(inside.sum())
    data = values.filled(np.nan)[valid]
    return {
        "field_coverage_pct": round(100.0 * int(valid.sum()) / total, 2) if total else 0.0,
        "elev_min_m": round(float(np.nanmin(data)), 3) if data.size else None,
        "elev_max_m": round(float(np.nanmax(data)), 3) if data.size else None,
    }



def _scale_elevation(path: Path, factor: float) -> None:
    """Convert elevation units in place (e.g. US survey feet to metres)."""
    with rasterio.open(path, "r+") as dst:
        data = dst.read(1)
        valid = data > NODATA + 1
        data[valid] = data[valid] * factor
        dst.write(data, 1)


def _service_items(session, service: dict, field_geom_4326, buffer_m: float) -> list[dict]:
    """Return the clip requests for one service: per collection, or one mosaic."""
    wkid = service.get("out_wkid") or service["wkid"]
    field_proj = gpd.GeoSeries([field_geom_4326], crs="EPSG:4326").to_crs(wkid).iloc[0]
    bounds = field_proj.buffer(buffer_m).bounds
    if service.get("mode") == "mosaic":
        return [
            {
                "name": service.get("collection") or service["name"],
                "pixel": float(service.get("pixel_m") or 1.0),
                "epoch": service.get("epoch") or _epoch_for("", service["name"]),
                "object_id": None,
                "wkid": wkid,
                "bounds": bounds,
                "field_proj": field_proj,
                "mosaic_rule": None,
            }
        ]
    items = []
    for item in _collections_for(session, service["url"], bounds, wkid):
        name = str(item["Name"])
        items.append(
            {
                "name": name,
                "pixel": round(float(item.get("LowPS") or 1.0), 3),
                "epoch": _epoch_for(name, service["name"]),
                "object_id": int(item["OBJECTID"]),
                "wkid": wkid,
                "bounds": bounds,
                "field_proj": field_proj,
                "mosaic_rule": {
                    "mosaicMethod": "esriMosaicLockRaster",
                    "lockRasterIds": [int(item["OBJECTID"])],
                },
            }
        )
    return items


def _process_field(
    session,
    services: list[dict],
    field,
    field_slug: str,
    out_dir: Path,
    *,
    buffer_m: float,
    dry_run: bool,
    force: bool,
    attribution: str,
) -> dict:
    manifest_path = out_dir / "manifest.json"
    previous = {}
    if manifest_path.exists():
        for entry in json.loads(manifest_path.read_text(encoding="utf-8")).get("collections", []):
            key = entry.get("file") or f"{entry['epoch']}_{slugify(entry['collection'])}.tif"
            previous[key] = entry
    entries = []
    for service in services:
        for item in _service_items(session, service, field.geometry, buffer_m):
            name, pixel, epoch = item["name"], item["pixel"], item["epoch"]
            filename = f"{epoch}_{slugify(name)}.tif"
            target = out_dir / filename
            entry = {
                "epoch": epoch,
                "collection": name,
                "object_id": item["object_id"],
                "mode": service.get("mode", "collections"),
                "pixel_size_m": pixel,
                "service_url": service["url"],
                "crs": f"EPSG:{item['wkid']}",
                "file": filename,
            }
            if service.get("z_to_m"):
                entry["source_vertical_unit"] = "US survey foot"
                entry["z_to_m"] = service["z_to_m"]
            if dry_run:
                print(f"  plan  {field_slug:<24} {epoch}  {name} ({pixel} m)")
                entries.append(entry)
                continue
            reusable = previous.get(filename)
            if not force and reusable and reusable.get("status") == "empty":
                print(f"  skip  {field_slug:<24} {epoch}  {name} (known empty)")
                entries.append(reusable)
                continue
            if (
                not force
                and reusable
                and target.exists()
                and reusable.get("sha256") == sha256_file(target)
            ):
                print(f"  skip  {field_slug:<24} {epoch}  {name} (manifest+file reusable)")
                entries.append(reusable)
                continue
            export_clip(
                session,
                service["url"],
                item["bounds"],
                pixel=pixel,
                wkid=item["wkid"],
                target=target,
                nodata=NODATA,
                mosaic_rule=item["mosaic_rule"],
            )
            if service.get("z_to_m"):
                _scale_elevation(target, float(service["z_to_m"]))
            entry.update(_coverage(target, item["field_proj"]))
            entry["downloaded_at"] = (
                datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            )
            if entry["field_coverage_pct"] == 0:
                # Footprint intersects but the collection has no pixels inside the field.
                target.unlink()
                entry.update({"status": "empty", "file": None})
                print(f"  empty {field_slug:<24} {epoch}  {name} (no pixels inside field)")
                entries.append(entry)
                continue
            entry["status"] = "ok"
            entry["sha256"] = sha256_file(target)
            print(
                f"  ok    {field_slug:<24} {epoch}  {name} ({pixel} m) "
                f"coverage {entry['field_coverage_pct']}%"
            )
            entries.append(entry)

    manifest = {
        "dataset_name": "state_lidar",
        "field_id": str(field["field_id"]),
        "field_slug": field_slug,
        "buffer_m": buffer_m,
        "attribution": attribution,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "collections": sorted(entries, key=lambda e: (e["epoch"], e["collection"])),
    }
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--grower", default=os.environ.get("AG_GROWER_SLUG", "default-grower"))
    parser.add_argument("--farm", default=os.environ.get("AG_FARM_SLUG", "default-farm"))
    parser.add_argument(
        "--service",
        action="append",
        help="ArcGIS ImageServer URL (repeatable, one clip per locked collection). "
        "Overrides --preset.",
    )
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default=None,
        help="Service preset; defaults to the farm's state (AR, MO, MS)",
    )
    parser.add_argument("--buffer-meters", type=float, default=20.0)
    parser.add_argument("--attribution", default=None)
    parser.add_argument("--field", action="append", help="Only these field slugs (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="List collections, no downloads")
    parser.add_argument("--force", action="store_true", default=os.environ.get("AG_FORCE") == "1")
    args = parser.parse_args(argv)

    boundary = farm_boundary_path(args.grower, args.farm)
    if not boundary.exists():
        sys.exit(f"ERROR: farm boundaries not found: {boundary}")
    fields = gpd.read_file(boundary).to_crs("EPSG:4326")
    slugs = field_slug_map_from_inventory(farm_manifest_dir(args.grower, args.farm) / "field-inventory.csv")

    session = new_session()
    if args.service:
        preset_name = "custom"
        specs = [{"url": url, "mode": "collections"} for url in args.service]
        attribution = args.attribution or DEFAULT_ATTRIBUTION
    else:
        states = {str(s).upper() for s in fields.get("state", []) if str(s).strip()}
        preset_name = args.preset or (
            STATE_PRESETS.get(next(iter(states))) if len(states) == 1 else None
        )
        if not preset_name:
            sys.exit(
                f"ERROR: no lidar preset for farm state(s) {sorted(states) or '[blank]'}; "
                f"pass --preset ({', '.join(sorted(PRESETS))}) or --service"
            )
        specs = PRESETS[preset_name]["services"]
        attribution = args.attribution or PRESETS[preset_name]["attribution"]
    services = [
        {**spec, "url": spec["url"].rstrip("/"), **service_info(session, spec["url"].rstrip("/"))}
        for spec in specs
    ]

    print("=" * 60)
    print(f"State lidar collections: {args.grower}/{args.farm}  (preset: {preset_name})")
    print("=" * 60)
    count = 0
    for _, field in fields.sort_values("field_id").iterrows():
        field_slug = slugs.get(str(field["field_id"]))
        if not field_slug or (args.field and field_slug not in args.field):
            continue
        out_dir = field_terrain_dir(args.grower, args.farm, field_slug) / "state_lidar"
        manifest = _process_field(
            session,
            services,
            field,
            field_slug,
            out_dir,
            buffer_m=args.buffer_meters,
            dry_run=args.dry_run,
            force=args.force,
            attribution=attribution,
        )
        count += len(manifest["collections"])
    print(f"\n{'Planned' if args.dry_run else 'Done'}: {count} field/collection clips")


if __name__ == "__main__":
    main()
