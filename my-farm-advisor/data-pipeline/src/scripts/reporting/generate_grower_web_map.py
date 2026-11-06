#!/usr/bin/env python3
# ruff: noqa: E402
# pyright: reportMissingImports=false
"""Generate a lightweight interactive web map for each grower in the runtime tree.

Reads only data-pipeline outputs: every farm's canonical
``boundary/field_boundaries.geojson`` plus optional per-field summaries (soil,
crop history, terrain, NDVI, lidar change) when they exist. Writes one
self-contained HTML file per grower with the field polygons on an ESRI World
Imagery satellite basemap (with an Esri World Street Map alternative; both
load online via Leaflet and work from a local file:// page), click popups with
grower / farm / field metadata, a field list that zooms to each field, and a
"fit all" control. Optional layers: SSURGO soil map units (colour by series
or drainage, with a legend and per-unit popups) and, per field, the average
NDVI map for each crop (mean of the yearly composites for the years that crop
was grown, per the CDL labels; "Unknown" years skipped). The NDVI maps are the
only embedded images: field-clipped, native 10 m, colourised PNGs on a red-yellow-green
ramp (red = low) stretched to the grower's 2nd-98th NDVI percentile (one scale per map). No raw imagery, rasters, or data bundles are embedded;
polygons are simplified (~0.5 m) and rounded to 1e-6 degrees. Output is
deterministic: same pipeline data in, same HTML out.

Outputs (runtime tree):
    growers/<grower>/derived/reports/<grower>_web_map.html
    growers/web_maps_index.html
"""

from __future__ import annotations

import argparse
import base64
import csv
import html
import io
import json
import re
import sys
import warnings
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

from bootstrap_runtime import ensure_runtime_environment

ensure_runtime_environment()

import geopandas as gpd
import matplotlib
import numpy as np
import rasterio
import shapely
from PIL import Image
from rasterio.features import geometry_mask

from paths import (
    DATA_ROOT,
    GROWERS_ROOT,
    farm_boundary_path,
    farm_cdl_rotation_path,
    farm_manifest_dir,
    farm_tables_dir,
    field_manifest_dir,
    field_soil_dir,
    field_soil_polygon_path,
    field_soil_summary_path,
    field_summaries_dir,
    field_tables_dir,
    grower_dir,
)
from reporting_bootstrap import field_slug_map_from_inventory

EQUAL_AREA_CRS = "EPSG:5070"
ACRES_PER_M2 = 1 / 4046.8564224
SIMPLIFY_M = 0.5
SIZE_WARN_BYTES = 1_000_000
# Okabe-Ito colour-blind-safe palette (black omitted; it is the outline casing).
FARM_COLORS = ["#E69F00", "#56B4E9", "#009E73", "#CC79A7", "#F0E442", "#D55E00", "#0072B2"]
LEAFLET_VERSION = "1.9.4"
# Paul Tol "muted" colour-blind-safe palette for soil series (cyan reserved for Water), kept
# distinct from the Okabe-Ito farm outline colours.
SOIL_SERIES_COLORS = ["#CC6677", "#332288", "#DDCC77", "#117733", "#882255", "#44AA99", "#999933", "#AA4499"]
SOIL_WATER_COLOR = "#88CCEE"
SOIL_OTHER_COLOR = "#BBBBBB"
# Wet to dry mapped onto cividis (dark blue = wet, yellow = dry): ordered and colour-blind safe.
DRAINAGE_CLASSES = [
    "Very poorly drained",
    "Poorly drained",
    "Somewhat poorly drained",
    "Moderately well drained",
    "Well drained",
    "Somewhat excessively drained",
    "Excessively drained",
]
NDVI_VMIN, NDVI_VMAX = 0.0, 0.9  # fallback scale when a grower has no NDVI maps
SKIP_CROPS = {"", "unknown", "no data", "nodata"}


def _hex(rgba) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(int(round(c * 255)) for c in rgba[:3]))


DRAINAGE_COLORS = {
    name: _hex(matplotlib.colormaps["cividis"](i / (len(DRAINAGE_CLASSES) - 1)))
    for i, name in enumerate(DRAINAGE_CLASSES)
}
# Classic agronomy NDVI ramp: red (low) -> yellow -> green (high), per user preference.
# Note: red-green ramps are hard for red-green colour-blind viewers; the soil layer and field
# outlines stay colour-blind safe.
NDVI_COLORMAP = "RdYlGn"
NDVI_GRADIENT = [_hex(matplotlib.colormaps[NDVI_COLORMAP](i / 8)) for i in range(9)]


# --------------------------------------------------------------------------- data


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_csv(path: Path) -> list[dict]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError:
        return []


def _num(value, digits: int = 1) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return f"{number:,.{digits}f}"


def _natural_key(text: str) -> list:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", str(text))]


def _soil(grower: str, farm: str, field_slug: str) -> dict:
    rows = _read_csv(field_soil_summary_path(grower, farm, field_slug))
    if not rows:
        return {}
    row = rows[0]
    out = {}
    if row.get("dominant_soil"):
        out["Dominant soil"] = row["dominant_soil"]
    if row.get("drainage_class"):
        out["Drainage"] = row["drainage_class"]
    ph, om = _num(row.get("avg_ph")), _num(row.get("avg_om_pct"))
    if ph or om:
        out["Topsoil pH / OM"] = f"{ph or '–'} / {om + ' %' if om else '–'}"
    return out


def _crop_history(rotation_rows: dict, field_id: str) -> dict:
    row = rotation_rows.get(field_id)
    if not row or not row.get("rotation_sequence"):
        return {}
    years = f"{row.get('history_start_year', '')}–{row.get('history_end_year', '')}".strip("–")
    return {f"Crop history ({years})" if years else "Crop history": row["rotation_sequence"].replace(" -> ", " → ")}


def _terrain(grower: str, farm: str, field_slug: str) -> dict:
    summary = _read_json(field_tables_dir(grower, farm, field_slug) / "dem_terrain_summary.json")
    manifest = _read_json(field_manifest_dir(grower, farm, field_slug) / "dem_terrain_manifest.json")
    out = {}
    elevation = summary.get("elevation_range_m") or {}
    lo, hi = _num(elevation.get("min"), 2), _num(elevation.get("max"), 2)
    if lo and hi:
        out["Elevation (field + 20 m buffer)"] = f"{lo}–{hi} m"
    source = manifest.get("selected_source") or {}
    if source.get("source_name"):
        resolution = source.get("resolution_m")
        out["Terrain source"] = source["source_name"] + (f" ({resolution:g} m)" if resolution else "")
    return out


def _ndvi(grower: str, farm: str, field_slug: str) -> dict:
    summary = _read_json(field_summaries_dir(grower, farm, field_slug) / "ndvi_yearly_summary.json")
    years = [entry for entry in summary.get("years", []) if entry.get("year")]
    if not years:
        return {}
    scenes = sum(int(entry.get("scene_count") or 0) for entry in years)
    span = f"{min(e['year'] for e in years)}–{max(e['year'] for e in years)}"
    return {"NDVI archive": f"{len(years)} seasons ({span}), {scenes} scenes"}


def _lidar_change(change_rows: dict, field_id: str) -> dict:
    rows = change_rows.get(field_id)
    if not rows:
        return {}
    row = max(rows, key=lambda r: (r.get("late_epoch", ""), -int(r.get("early_epoch") or 0)))
    out = {
        f"Lidar change {row['early_epoch']}→{row['late_epoch']}": (
            f"{row['verdict']} ({row.get('changed_pct', '?')} % of field changed)"
        )
    }
    grade = _num(row.get("late_plane_slope_pct"), 3)
    if grade:
        out[f"Field grade ({row['late_epoch']} lidar)"] = f"{grade} %"
    return out


def _soil_units(grower: str, farm: str, field_slug: str, field_id: str, field_area_m2: float) -> list[dict]:
    """SSURGO map units clipped to one field, with dominant-component topsoil properties."""
    polygons_path = field_soil_polygon_path(grower, farm, field_slug)
    if not polygons_path.exists():
        return []
    try:
        soils = gpd.read_file(polygons_path)
    except Exception:
        return []
    if soils.empty or "mukey" not in soils.columns:
        return []
    components: dict[str, dict] = {}
    for row in _read_csv(field_soil_dir(grower, farm, field_slug) / "ssurgo_full.csv"):
        mukey = str(row.get("mukey", ""))
        try:
            pct = float(row.get("comppct_r") or 0)
            depth = float(row.get("hzdept_r") or 0)
        except ValueError:
            continue
        current = components.get(mukey)
        # Dominant component (highest %), then its shallowest horizon.
        if current is None or (pct, -depth) > (current["_pct"], -current["_depth"]):
            components[mukey] = {**row, "_pct": pct, "_depth": depth}
    soils = soils.to_crs("EPSG:4326")
    projected = soils.to_crs(EQUAL_AREA_CRS)
    simplified = projected.geometry.simplify(SIMPLIFY_M, preserve_topology=True).to_crs("EPSG:4326")
    units = []
    for idx, row in soils.iterrows():
        mukey = str(row["mukey"])
        comp = components.get(mukey, {})
        area = float(projected.geometry.loc[idx].area)
        series = str(comp.get("compname") or "Unrated").strip() or "Unrated"
        drainage = str(comp.get("drainagecl") or "").strip()
        info = [
            ["Soil series", series],
            ["Share of map unit", f"{comp['_pct']:.0f} %" if comp else None],
            ["Drainage", drainage or "Not rated"],
            ["Topsoil pH", _num(comp.get("ph1to1h2o_r"))],
            ["Topsoil OM", f"{_num(comp.get('om_r'), 2)} %" if _num(comp.get("om_r"), 2) else None],
            ["Clay / sand", f"{_num(comp.get('claytotal_r')) or '–'} % / {_num(comp.get('sandtotal_r')) or '–'} %"
             if comp else None],
            ["Water capacity (AWC)", f"{_num(comp.get('awc_r'), 2)} in/in" if _num(comp.get("awc_r"), 2) else None],
            ["Map unit key", mukey],
            ["Area in field", f"{_num(area * ACRES_PER_M2, 1)} ac ({100 * area / field_area_m2:.0f} % of field)"
             if field_area_m2 else None],
        ]
        units.append(
            {
                "type": "Feature",
                "properties": {
                    "field": field_id,
                    "series": series,
                    "drainage": drainage,
                    "acres": area * ACRES_PER_M2,
                    "info": [pair for pair in info if pair[1]],
                },
                "geometry": shapely.geometry.mapping(shapely.set_precision(simplified.loc[idx], 1e-6)),
            }
        )
    return units


def _ndvi_scale(ndvi: dict[str, list[dict]]) -> tuple[float, float]:
    """One colour scale per grower map: 2nd-98th percentile of all crop maps, rounded to 0.05.

    Seasonal composites cluster in a narrow band, so a fixed 0-0.9 ramp hides within-field
    variation; a shared per-grower stretch keeps every field on the map comparable.
    """
    values = [layer["_array"][np.isfinite(layer["_array"])] for layers in ndvi.values() for layer in layers]
    values = [v for v in values if v.size]
    if not values:
        return NDVI_VMIN, NDVI_VMAX
    low, high = np.percentile(np.concatenate(values), [2, 98])
    low, high = np.floor(low * 20) / 20, np.ceil(high * 20) / 20
    if high - low < 0.1:
        high = low + 0.1
    return float(max(-1.0, low)), float(min(1.0, high))


def _ndvi_png(values: np.ndarray, vmin: float, vmax: float) -> str:
    """Colourise an NDVI array (NaN = transparent) on the red-yellow-green ramp; return a PNG data URL."""
    scaled = np.clip((values - vmin) / (vmax - vmin), 0, 1)
    rgba = (matplotlib.colormaps[NDVI_COLORMAP](np.nan_to_num(scaled)) * 255).astype("uint8")
    rgba[..., 3] = np.where(np.isfinite(values), 235, 0)
    buffer = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _ndvi_by_crop(grower: str, farm: str, field_slug: str, field_geom) -> list[dict]:
    """Average the yearly NDVI composites per crop (CDL label of each year) for one field."""
    summary = _read_json(field_summaries_dir(grower, farm, field_slug) / "ndvi_yearly_summary.json")
    by_crop: dict[str, list[tuple[int, Path, int]]] = {}
    for entry in summary.get("years", []):
        crop = str(entry.get("crop_name") or "").strip()
        tif = entry.get("composite_tif")
        if crop.lower() in SKIP_CROPS or not tif:
            continue
        path = Path(tif) if Path(tif).is_absolute() else DATA_ROOT / tif
        if path.exists():
            by_crop.setdefault(crop, []).append((int(entry["year"]), path, int(entry.get("scene_count") or 0)))
    layers = []
    for crop in sorted(by_crop, key=lambda c: (-len(by_crop[c]), c)):
        stack, transform, crs, shape = [], None, None, None
        for _, path, _ in sorted(by_crop[crop]):
            with rasterio.open(path) as src:
                data = src.read(1, masked=True).astype("float64").filled(np.nan)
                if shape is not None and data.shape != shape:
                    continue
                stack.append(data)
                transform, crs, shape = src.transform, src.crs, data.shape
        if not stack:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels stay NaN
            mean = np.nanmean(np.stack(stack), axis=0) if len(stack) > 1 else stack[0]
        geom = gpd.GeoSeries([field_geom], crs="EPSG:4326").to_crs(crs).iloc[0]
        inside = geometry_mask([geom], shape, transform, invert=True)
        mean = np.where(inside, mean, np.nan)
        if not np.isfinite(mean).any():
            continue
        west, north = transform * (0, 0)
        east, south = transform * (shape[1], shape[0])
        if crs and crs.to_epsg() != 4326:
            box = gpd.GeoSeries([shapely.geometry.box(west, south, east, north)], crs=crs).to_crs(4326).total_bounds
            west, south, east, north = box
        years = sorted(year for year, _, _ in by_crop[crop])
        layers.append(
            {
                "crop": crop,
                "years": years,
                "scenes": sum(count for _, _, count in by_crop[crop]),
                "mean": round(float(np.nanmean(mean)), 3),
                "_array": mean,  # colourised later on the grower-wide scale
                "bounds": [[round(south, 7), round(west, 7)], [round(north, 7), round(east, 7)]],
            }
        )
    return layers


def _soil_legend(soil_units: list[dict]) -> dict:
    acres: dict[str, float] = {}
    drainage_by_series: dict[str, set] = {}
    for unit in soil_units:
        props = unit["properties"]
        acres[props["series"]] = acres.get(props["series"], 0.0) + props["acres"]
        drainage_by_series.setdefault(props["series"], set()).add(props["drainage"] or "Not rated")
    series = []
    palette = iter(SOIL_SERIES_COLORS)
    for name in sorted(acres, key=lambda n: (-acres[n], n)):
        color = SOIL_WATER_COLOR if name.lower() == "water" else next(palette, SOIL_OTHER_COLOR)
        series.append({"name": name, "color": color, "acres": round(acres[name], 1),
                       "drainage": ", ".join(sorted(drainage_by_series[name]))})
    present = {unit["properties"]["drainage"] or "Not rated" for unit in soil_units}
    drainage = [{"name": name, "color": DRAINAGE_COLORS[name]} for name in DRAINAGE_CLASSES if name in present]
    if present - set(DRAINAGE_CLASSES):
        drainage.append({"name": "Not rated", "color": SOIL_OTHER_COLOR})
    return {"series": series, "drainage": drainage}


def _farm_features(grower: str, farm_path: Path, grower_label: str | None) -> tuple[list[dict], dict, list[dict], dict]:
    farm = farm_path.name
    boundary = farm_boundary_path(grower, farm)
    if not boundary.exists():
        return [], {}, [], {}
    gdf = gpd.read_file(boundary)
    if gdf.empty:
        return [], {}, [], {}
    gdf = gdf.to_crs("EPSG:4326")
    farm_meta = _read_json(farm_path / "farm.json")
    slugs = field_slug_map_from_inventory(farm_manifest_dir(grower, farm) / "field-inventory.csv")
    rotation = {r["field_id"]: r for r in _read_csv(farm_cdl_rotation_path(grower, farm)) if r.get("field_id")}
    changes: dict[str, list[dict]] = {}
    for row in _read_csv(farm_tables_dir(grower, farm) / f"{farm}_lidar_change_summary.csv"):
        changes.setdefault(row.get("field_id", ""), []).append(row)

    projected = gdf.to_crs(EQUAL_AREA_CRS)
    simplified = projected.geometry.simplify(SIMPLIFY_M, preserve_topology=True).to_crs("EPSG:4326")
    features = []
    soil_units: list[dict] = []
    ndvi: dict[str, list[dict]] = {}
    farm_name = None
    for idx, row in gdf.iterrows():
        field_id = str(row.get("field_id", idx))
        field_slug = slugs.get(field_id, field_id)
        field_name = str(row.get("field_name") or row.get("name") or field_slug)
        farm_name = str(row.get("farm_name") or farm_meta.get("display_name") or farm)
        grower_name = str(row.get("grower_name") or grower_label or grower)
        acres = row.get("area_acres")
        if acres is None or acres != acres:
            acres = projected.geometry.loc[idx].area * ACRES_PER_M2
        county = str(row.get("county_name") or "").strip()
        state = str(row.get("state") or "").strip()
        props = {
            "Grower": grower_name,
            "Farm": farm_name,
            "Field": field_name,
            "Field ID": field_id,
            "Acres": _num(acres, 1),
        }
        if county or state:
            props["County / State"] = ", ".join(part for part in (county and f"{county} Co.", state) if part)
        for extra in (
            _soil(grower, farm, field_slug),
            _crop_history(rotation, field_id),
            _ndvi(grower, farm, field_slug),
            _terrain(grower, farm, field_slug),
            _lidar_change(changes, field_id),
        ):
            props.update(extra)
        geometry = shapely.set_precision(simplified.loc[idx], 1e-6)
        feature_id = f"{farm}/{field_slug}"
        soil_units.extend(_soil_units(grower, farm, field_slug, feature_id, float(projected.geometry.loc[idx].area)))
        crop_layers = _ndvi_by_crop(grower, farm, field_slug, row.geometry)
        if crop_layers:
            ndvi[feature_id] = crop_layers
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "id": feature_id,
                    "farm": farm,
                    "name": field_name,
                    "acres": float(acres),
                    # Ordered [label, value] pairs: popup rows keep this order.
                    "info": [[key, value] for key, value in props.items() if value],
                },
                "geometry": shapely.geometry.mapping(geometry),
            }
        )
    features.sort(key=lambda f: _natural_key(f["properties"]["name"]))
    return features, {"slug": farm, "name": farm_name or farm}, soil_units, ndvi


# --------------------------------------------------------------------------- html

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@__LEAFLET__/dist/leaflet.css" crossorigin="">
<script src="https://unpkg.com/leaflet@__LEAFLET__/dist/leaflet.js" crossorigin=""></script>
<style>
  html, body { height: 100%; margin: 0; font: 14px/1.35 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  #app { display: flex; height: 100%; }
  #side { width: 290px; display: flex; flex-direction: column; background: #12161c; color: #e8eaed; box-shadow: 2px 0 8px rgba(0,0,0,.4); z-index: 1000; }
  #side header { padding: 14px 16px 10px; border-bottom: 1px solid #2a313b; }
  #side h1 { font-size: 17px; margin: 0 0 4px; }
  #side .stats { color: #9aa4b2; font-size: 12.5px; }
  #side .tools { display: flex; gap: 6px; margin-top: 10px; }
  #side input { flex: 1; padding: 6px 8px; border-radius: 6px; border: 1px solid #333c48; background: #1b2129; color: #e8eaed; }
  #side button { padding: 6px 10px; border-radius: 6px; border: 0; background: #2f6fed; color: #fff; cursor: pointer; }
  #side button:hover { background: #4a84f5; }
  #list { overflow-y: auto; flex: 1; padding: 4px 0 12px; }
  .farm { padding: 10px 16px 4px; font-size: 11.5px; letter-spacing: .06em; text-transform: uppercase; color: #9aa4b2; display: flex; align-items: center; gap: 8px; }
  .swatch { width: 11px; height: 11px; border-radius: 2px; display: inline-block; border: 1px solid #fff; }
  .item { padding: 6px 16px 6px 34px; cursor: pointer; display: flex; justify-content: space-between; gap: 8px; }
  .item:hover, .item.active { background: #1f2a38; }
  .item small { color: #9aa4b2; white-space: nowrap; }
  #side footer { padding: 8px 16px; font-size: 11px; color: #7d8794; border-top: 1px solid #2a313b; }
  #map { flex: 1; }
  .leaflet-popup-content { margin: 10px 12px; min-width: 230px; }
  .popup h3 { margin: 0 0 6px; font-size: 15px; }
  .popup table { border-collapse: collapse; font-size: 12.5px; }
  .popup td { padding: 2px 6px 2px 0; vertical-align: top; }
  .popup td:first-child { color: #555; white-space: nowrap; }
  .field-label { background: rgba(0,0,0,.78); color: #fff; border: 1px solid rgba(255,255,255,.6); box-shadow: none; font-weight: 600; padding: 1px 5px; }
  .field-label::before { display: none; }
  .labels-off .field-label { display: none; }
  .popup h4 { margin: 8px 0 4px; font-size: 12.5px; color: #333; border-top: 1px solid #ddd; padding-top: 6px; }
  .ndvi-btn { display: block; width: 100%; margin: 3px 0; padding: 5px 8px; border: 1px solid #2f6fed; border-radius: 5px; background: #eef3fe; color: #1a3f8f; text-align: left; cursor: pointer; font-size: 12px; }
  .ndvi-btn:hover, .ndvi-btn.on { background: #2f6fed; color: #fff; }
  .ndvi-img { image-rendering: pixelated; }
  .viewer-link { display: block; margin-top: 8px; padding: 6px 8px; border-radius: 5px; background: #12161c; color: #fff !important; text-decoration: none; font-size: 12.5px; text-align: center; }
  .viewer-link small { color: #b8c4d6; }
  .legend { background: rgba(255,255,255,.95); padding: 8px 10px; border-radius: 6px; box-shadow: 0 1px 6px rgba(0,0,0,.35); font-size: 12px; max-width: 260px; line-height: 1.3; }
  .legend h4 { margin: 0 0 6px; font-size: 12.5px; }
  .legend section + section { margin-top: 10px; padding-top: 8px; border-top: 1px solid #ddd; }
  .legend .row { display: flex; align-items: flex-start; gap: 7px; margin: 3px 0; }
  .legend .chip { width: 14px; height: 14px; flex: none; border: 1px solid #333; border-radius: 2px; margin-top: 1px; }
  .legend .note { color: #666; font-size: 11px; }
  .legend .toggle { display: inline-flex; margin: 0 0 6px; border: 1px solid #2f6fed; border-radius: 5px; overflow: hidden; }
  .legend .toggle button, .legend .link { border: 0; background: #fff; color: #1a3f8f; padding: 3px 8px; cursor: pointer; font-size: 11.5px; }
  .legend .toggle button.on { background: #2f6fed; color: #fff; }
  .legend .link { padding: 3px 0; margin-right: 10px; text-decoration: underline; background: none; }
  .legend .bar { height: 12px; border: 1px solid #333; border-radius: 2px; margin: 4px 0 2px; }
  .legend .ticks { display: flex; justify-content: space-between; font-size: 11px; color: #444; }
  @media (max-width: 700px) { #side { width: 200px; } }
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <header>
      <h1>__HEADING__</h1>
      <div class="stats">__STATS__</div>
      <div class="tools"><input id="filter" placeholder="Filter fields…" aria-label="Filter fields"><button id="fit" title="Zoom to all fields">Fit all</button></div>
    </header>
    <div id="list"></div>
    <footer>Boundaries: My Farm Advisor data pipeline. Basemaps: Esri World Imagery (Esri, Maxar, Earthstar Geographics) and Esri World Street Map.</footer>
  </aside>
  <div id="map"></div>
</div>
<script>
const DATA = __DATA__;
const FARMS = __FARMS__;
const SOILS = __SOILS__;
const SOIL_LEGEND = __SOIL_LEGEND__;
const NDVI = __NDVI__;
const NDVI_SCALE = __NDVI_SCALE__;
const VIEWER_FILE = __VIEWER_FILE__;
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const map = L.map("map", { zoomControl: true, preferCanvas: false });
const imagery = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 20, maxNativeZoom: 19, attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics" }).addTo(map);
// Esri streets, not OSM's standard tile servers: their policy returns 403 "Access blocked"
// for pages opened from disk (file://, no Referer), which is how these maps are viewed.
const streets = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 20, maxNativeZoom: 19, attribution: "Streets &copy; Esri, HERE, Garmin, USGS, NGA, EPA, USDA, NPS" });
const labels = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 20, maxNativeZoom: 19, attribution: "Labels &copy; Esri" });
L.control.scale({ imperial: true, metric: true }).addTo(map);
// Draw order: basemap < soils < NDVI < field outlines (overlayPane, z 400).
map.createPane("soils").style.zIndex = 350;
map.createPane("ndvi").style.zIndex = 380;

// Colour-blind-safe farm colours (Okabe-Ito) drawn over a dark casing, so outlines keep contrast
// on both dark satellite imagery and the pale street map. Selection uses width + dashes, not hue.
const color = slug => (FARMS.find(f => f.slug === slug) || {}).color || "#E69F00";
let onStreets = false;
let selectedId = null;
let soilsOn = false;
let ndviOn = false;
const fillFor = () => (onStreets ? 0.28 : 0.16);
// Field fills turn off while soils or NDVI are shown so those layers stay visible and clickable.
const baseStyle = f => {
  const selected = f.properties.id === selectedId;
  return {
    color: color(f.properties.farm), weight: selected ? 4 : 2.5, opacity: 1,
    dashArray: selected ? "8 5" : null, fillColor: color(f.properties.farm),
    fill: !(soilsOn || ndviOn), fillOpacity: selected ? fillFor() + 0.14 : fillFor(),
  };
};
const casingStyle = f => ({ color: "#111", weight: f.properties.id === selectedId ? 8 : 5.5, opacity: 0.9, fill: false });
const layers = {};
const casing = L.geoJSON(DATA, { style: casingStyle, interactive: false });
const rowsHtml = info => `<table>${info.map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("")}</table>`;
const yearSpan = ys => ys.join(", ");
const ndviButtons = id => (NDVI[id] || []).map((layer, i) =>
  `<button class="ndvi-btn" data-field="${esc(id)}" data-crop="${i}">Average ${esc(layer.crop)} NDVI · ${
    yearSpan(layer.years)} (${layer.years.length} yr) · mean ${layer.mean.toFixed(2)}</button>`).join("");
const viewerLink = id => VIEWER_FILE
  ? `<a class="viewer-link" href="${esc(VIEWER_FILE)}#field=${encodeURIComponent(id)}" target="_blank" rel="noopener">Open NDVI viewer ↗ <small>(flip through every scene)</small></a>` : "";
const popupHtml = p => `<div class="popup"><h3>${esc(p.name)}</h3>${rowsHtml(p.info)}${
  NDVI[p.id] ? `<h4>Average NDVI by crop (click to map)</h4>${ndviButtons(p.id)}` : ""}${viewerLink(p.id)}</div>`;

const fields = L.geoJSON(DATA, {
  style: baseStyle,
  onEachFeature: (feature, layer) => {
    const p = feature.properties;
    layers[p.id] = layer;
    layer.bindPopup(popupHtml(p), { maxWidth: 360, autoPanPadding: [30, 30] });
    layer.bindTooltip(esc(p.name), { permanent: true, direction: "center", className: "field-label" });
    layer.on("mouseover", () => layer.setStyle({ weight: 4.5, fillOpacity: fillFor() + 0.18 }));
    layer.on("mouseout", () => layer.setStyle(baseStyle(feature)));
    layer.on("click", () => select(p.id, false));
    layer.on("popupclose", () => {
      if (selectedId !== p.id) return;
      selectedId = null;
      restyle();
      document.querySelectorAll(".item.active").forEach(el => el.classList.remove("active"));
    });
  }
});
const fieldGroup = L.featureGroup([casing, fields]).addTo(map);
const restyle = () => { casing.setStyle(casingStyle); fields.setStyle(baseStyle); };
map.on("baselayerchange", e => { onStreets = e.layer === streets; restyle(); });

// ---- SSURGO soils layer (colour by series or drainage) --------------------------------------
let soilMode = "series";
const soilStyle = f => ({
  pane: "soils", color: "#222", weight: 1, opacity: 0.8, fillOpacity: 0.62,
  fillColor: soilMode === "series" ? f.properties.series_color : f.properties.drainage_color,
});
const soils = L.geoJSON(SOILS, {
  pane: "soils",
  style: soilStyle,
  onEachFeature: (feature, layer) => {
    const p = feature.properties;
    const field = DATA.features.find(f => f.properties.id === p.field);
    layer.bindPopup(`<div class="popup"><h3>${esc(p.series)}</h3>${rowsHtml(p.info)}${
      field ? `<p class="note">Field: ${esc(field.properties.name)}</p>` : ""}</div>`, { maxWidth: 320 });
  }
});

// ---- Average NDVI by crop overlays ----------------------------------------------------------
const ndviGroup = L.layerGroup().addTo(map);
let ndviState = null;  // { crop, fieldId | null (all fields) }
function showNdvi(fieldId, cropName, allFields = false) {
  ndviGroup.clearLayers();
  const targets = allFields ? Object.keys(NDVI) : [fieldId];
  let shown = 0;
  targets.forEach(id => (NDVI[id] || []).filter(l => l.crop === cropName).forEach(l => {
    L.imageOverlay(l.png, l.bounds, { pane: "ndvi", className: "ndvi-img", opacity: 1, interactive: false }).addTo(ndviGroup);
    shown += 1;
  }));
  ndviOn = shown > 0;
  ndviState = ndviOn ? { crop: cropName, fieldId: allFields ? null : fieldId, count: shown } : null;
  document.querySelectorAll(".ndvi-btn").forEach(b => {
    const layer = (NDVI[b.dataset.field] || [])[+b.dataset.crop];
    b.classList.toggle("on", !!(ndviState && layer && layer.crop === cropName && (allFields || b.dataset.field === fieldId)));
  });
  restyle();
  renderLegend();
}
function hideNdvi() { ndviGroup.clearLayers(); ndviOn = false; ndviState = null; document.querySelectorAll(".ndvi-btn.on").forEach(b => b.classList.remove("on")); restyle(); renderLegend(); }
document.addEventListener("click", e => {
  const btn = e.target.closest(".ndvi-btn");
  if (btn) {
    const layer = NDVI[btn.dataset.field][+btn.dataset.crop];
    if (ndviState && ndviState.fieldId === btn.dataset.field && ndviState.crop === layer.crop) hideNdvi();
    else showNdvi(btn.dataset.field, layer.crop);
    return;
  }
  const act = e.target.closest("[data-act]");
  if (!act) return;
  if (act.dataset.act === "soil-mode") { soilMode = act.dataset.mode; soils.setStyle(soilStyle); renderLegend(); }
  if (act.dataset.act === "ndvi-all") showNdvi(null, ndviState.crop, true);
  if (act.dataset.act === "ndvi-hide") hideNdvi();
});

// ---- Legend ---------------------------------------------------------------------------------
const legend = L.control({ position: "bottomright" });
legend.onAdd = () => {
  const div = L.DomUtil.create("div", "legend");
  L.DomEvent.disableClickPropagation(div);
  L.DomEvent.disableScrollPropagation(div);
  return div;
};
legend.addTo(map);
function renderLegend() {
  const div = legend.getContainer();
  const parts = [];
  if (soilsOn) {
    const items = soilMode === "series" ? SOIL_LEGEND.series : SOIL_LEGEND.drainage;
    parts.push(`<section><h4>SSURGO soils</h4>
      <div class="toggle"><button data-act="soil-mode" data-mode="series" class="${soilMode === "series" ? "on" : ""}">Series</button><button data-act="soil-mode" data-mode="drainage" class="${soilMode === "drainage" ? "on" : ""}">Drainage</button></div>
      ${items.map(i => `<div class="row"><span class="chip" style="background:${i.color}"></span><span>${esc(i.name)}${
        soilMode === "series" ? `<br><span class="note">${esc(i.drainage)} · ${i.acres.toFixed(1)} ac</span>` : ""}</span></div>`).join("")}
      <div class="note">Click a soil unit for its properties.</div></section>`);
  }
  if (ndviState) {
    const s = ndviState;
    const one = s.fieldId ? NDVI[s.fieldId].find(l => l.crop === s.crop) : null;
    const field = s.fieldId ? DATA.features.find(f => f.properties.id === s.fieldId).properties.name : null;
    parts.push(`<section><h4>Average ${esc(s.crop)} NDVI</h4>
      <div class="bar" style="background:linear-gradient(to right, ${NDVI_SCALE.colors.join(",")})"></div>
      <div class="ticks"><span>≤ ${NDVI_SCALE.min.toFixed(2)}</span><span>${((NDVI_SCALE.min + NDVI_SCALE.max) / 2).toFixed(2)}</span><span>≥ ${NDVI_SCALE.max.toFixed(2)}</span></div>
      <div class="note">${one ? `${esc(field)}: ${yearSpan(one.years)} (${one.years.length} season${one.years.length > 1 ? "s" : ""}, ${one.scenes} scenes), field mean ${one.mean.toFixed(2)}`
        : `All fields with ${esc(s.crop)} years (${s.count})`}. Pixel = 10 m Sentinel-2/Landsat seasonal composite averaged over the years this crop was grown (CDL). Colour scale shared by all fields on this map.</div>
      ${one ? `<button class="link" data-act="ndvi-all">Show for all fields</button>` : ""}<button class="link" data-act="ndvi-hide">Hide NDVI</button></section>`);
  }
  div.innerHTML = parts.join("");
  div.style.display = parts.length ? "" : "none";
}
renderLegend();

L.control.layers({ "Satellite (Esri)": imagery, "Streets (Esri)": streets },
  { "Place labels": labels, "SSURGO soils": soils, "Fields": fieldGroup },
  { collapsed: false }).addTo(map);
map.on("overlayadd overlayremove", e => {
  if (e.layer !== soils) return;
  soilsOn = e.type === "overlayadd";
  restyle();
  renderLegend();
});

const fitAll = () => map.fitBounds(fields.getBounds(), { padding: [30, 30] });
const updateLabels = () => document.getElementById("map").classList.toggle("labels-off", map.getZoom() < 15);
map.on("zoomend", updateLabels);
fitAll();
updateLabels();

function select(id, zoom = true) {
  document.querySelectorAll(".item").forEach(el => el.classList.toggle("active", el.dataset.id === id));
  const layer = layers[id];
  if (!layer) return;
  selectedId = id;
  restyle();
  if (zoom) {
    // Open after the zoom animation so auto-pan can fit the popup on screen.
    map.once("moveend", () => layer.openPopup());
    map.fitBounds(layer.getBounds(), { paddingTopLeft: [40, 260], paddingBottomRight: [40, 40], maxZoom: 18 });
  } else {
    layer.openPopup();
  }
  const el = document.querySelector(`.item[data-id="${CSS.escape(id)}"]`);
  if (el) el.scrollIntoView({ block: "nearest" });
}

const list = document.getElementById("list");
FARMS.forEach(farm => {
  const head = document.createElement("div");
  head.className = "farm";
  head.innerHTML = `<span class="swatch" style="background:${farm.color}"></span>${esc(farm.name)}`;
  list.appendChild(head);
  DATA.features.filter(f => f.properties.farm === farm.slug).forEach(f => {
    const item = document.createElement("div");
    item.className = "item";
    item.dataset.id = f.properties.id;
    item.dataset.search = f.properties.name.toLowerCase();
    item.innerHTML = `<span>${esc(f.properties.name)}</span><small>${f.properties.acres.toFixed(1)} ac</small>`;
    item.onclick = () => select(f.properties.id);
    list.appendChild(item);
  });
});
document.getElementById("fit").onclick = fitAll;
document.getElementById("filter").oninput = e => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll(".item").forEach(el => { el.style.display = el.dataset.search.includes(q) ? "" : "none"; });
};
</script>
</body>
</html>
"""


def _json_for_html(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False).replace("</", "<\\/")


def build_grower_map(grower: str, viewer_file: str | None = None) -> tuple[Path, dict]:
    grower_root = grower_dir(grower)
    grower_meta = _read_json(grower_root / "grower.json")
    grower_label = grower_meta.get("display_name") if grower_meta.get("display_name") != grower else None
    features: list[dict] = []
    farms: list[dict] = []
    soil_units: list[dict] = []
    ndvi: dict[str, list[dict]] = {}
    for farm_path in sorted((grower_root / "farms").iterdir(), key=lambda p: _natural_key(p.name)):
        if not farm_path.is_dir():
            continue
        farm_features, farm_info, farm_soils, farm_ndvi = _farm_features(grower, farm_path, grower_label)
        if not farm_features:
            continue
        farm_info["color"] = FARM_COLORS[len(farms) % len(FARM_COLORS)]
        farms.append(farm_info)
        features.extend(farm_features)
        soil_units.extend(farm_soils)
        ndvi.update(farm_ndvi)
    ndvi_min, ndvi_max = _ndvi_scale(ndvi)
    for layers in ndvi.values():
        for layer in layers:
            layer["png"] = _ndvi_png(layer.pop("_array"), ndvi_min, ndvi_max)
    legend = _soil_legend(soil_units)
    series_color = {item["name"]: item["color"] for item in legend["series"]}
    for unit in soil_units:
        props = unit["properties"]
        props["series_color"] = series_color[props["series"]]
        props["drainage_color"] = DRAINAGE_COLORS.get(props["drainage"], SOIL_OTHER_COLOR)
        props.pop("acres", None)
    if not features:
        raise RuntimeError(f"grower {grower} has no farm boundaries")

    grower_name = dict(features[0]["properties"]["info"]).get("Grower", grower)
    total_acres = sum(f["properties"]["acres"] for f in features)
    stats = (
        f"{len(farms)} farm{'s' if len(farms) != 1 else ''} · {len(features)} fields · "
        f"{total_acres:,.0f} acres"
    )
    page = (
        _TEMPLATE.replace("__LEAFLET__", LEAFLET_VERSION)
        .replace("__TITLE__", html.escape(f"{grower_name} — field map"))
        .replace("__HEADING__", html.escape(grower_name))
        .replace("__STATS__", html.escape(stats))
        .replace("__FARMS__", _json_for_html(farms))
        .replace("__SOILS__", _json_for_html({"type": "FeatureCollection", "features": soil_units}))
        .replace("__SOIL_LEGEND__", _json_for_html(legend))
        .replace("__NDVI__", _json_for_html(ndvi))
        .replace("__NDVI_SCALE__", _json_for_html({"min": ndvi_min, "max": ndvi_max, "colors": NDVI_GRADIENT}))
        .replace("__VIEWER_FILE__", _json_for_html(viewer_file or ""))
        .replace("__DATA__", _json_for_html({"type": "FeatureCollection", "features": features}))
    )
    target = grower_root / "derived" / "reports" / f"{grower}_web_map.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return target, {
        "grower": grower,
        "name": grower_name,
        "farms": len(farms),
        "fields": len(features),
        "acres": total_acres,
        "soil_units": len(soil_units),
        "ndvi_layers": sum(len(layers) for layers in ndvi.values()),
        "bytes": target.stat().st_size,
    }


def write_index(entries: list[dict]) -> Path:
    rows = "\n".join(
        f'<li><a href="{html.escape(e["grower"])}/derived/reports/{html.escape(e["grower"])}_web_map.html">'
        f'{html.escape(e["name"])}</a> — {e["farms"]} farm(s), {e["fields"]} fields, {e["acres"]:,.0f} acres'
        + (f' · <a href="{html.escape(e["grower"])}/derived/reports/{html.escape(e["viewer"])}">NDVI viewer</a>'
           if e.get("viewer") else "")
        + "</li>"
        for e in sorted(entries, key=lambda e: _natural_key(e["name"]))
    )
    page = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Grower web maps</title>'
        "<style>body{font:15px system-ui,sans-serif;margin:40px;max-width:720px}li{margin:8px 0}</style>"
        f"</head><body><h1>Grower web maps</h1><ul>\n{rows}\n</ul></body></html>\n"
    )
    target = GROWERS_ROOT / "web_maps_index.html"
    target.write_text(page, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--grower", action="append", help="Grower slug (repeatable)")
    group.add_argument("--all-growers", action="store_true", help="Every grower under growers/")
    parser.add_argument(
        "--no-ndvi-viewer",
        action="store_true",
        help="Skip the companion <grower>_ndvi_viewer.html (scene-by-scene NDVI detail viewer)",
    )
    args = parser.parse_args(argv)

    growers = (
        sorted(p.name for p in GROWERS_ROOT.iterdir() if (p / "farms").is_dir())
        if args.all_growers
        else args.grower
    )
    if not growers:
        sys.exit("No growers found under the runtime growers/ directory.")
    entries = []
    for grower in growers:
        viewer_file = None
        if not args.no_ndvi_viewer:
            from generate_grower_ndvi_viewer import build_ndvi_viewer  # lazy: avoids a circular import

            viewer_path, viewer = build_ndvi_viewer(grower, map_filename=f"{grower}_web_map.html")
            viewer_file = viewer_path.name
            print(
                f"ok    {grower:<22} NDVI viewer: {viewer['scenes']} scenes "
                f"({viewer['skipped']} cloudy skipped), {viewer['bytes'] / 1e6:.1f} MB  {viewer_path}"
            )
        target, entry = build_grower_map(grower, viewer_file)
        entry["viewer"] = viewer_file
        entries.append(entry)
        warn = "  WARNING: over 1 MB" if entry["bytes"] > SIZE_WARN_BYTES else ""
        print(
            f"ok    {grower:<22} {entry['farms']} farm(s), {entry['fields']:>3} fields, "
            f"{entry['soil_units']:>3} soil units, {entry['ndvi_layers']:>3} NDVI crop maps, "
            f"{entry['bytes'] / 1024:,.0f} KB  {target}{warn}"
        )
    if args.all_growers:
        print(f"index {write_index(entries)}")


if __name__ == "__main__":
    main()
