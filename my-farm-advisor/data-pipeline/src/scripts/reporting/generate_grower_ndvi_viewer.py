#!/usr/bin/env python3
# ruff: noqa: E402
# pyright: reportMissingImports=false
"""Generate a per-grower NDVI detail viewer (companion to the grower web map).

One self-contained HTML file per grower for flipping through every field's
satellite NDVI scenes: Sentinel-2 (10 m) and Landsat (30 m), all seasons in
the pipeline archive (default 2021-2025). Per field it shows each scene on an
ESRI satellite basemap with prev/next, a date slider, play, year tabs, a
sensor filter, a hide-partly-cloudy switch, per-scene stats (date, sensor,
cloud cover, clear share of the field, field-mean NDVI, CDL crop), and a
clickable NDVI-over-time chart. An "Averages" tab shows the average NDVI per
crop and each year's composite.

Inputs (pipeline outputs only): fields/<field>/satellite/{sentinel,landsat}/
manifest.json and their per-scene ndvi_tif files, derived/summaries/
ndvi_yearly_summary.json and the yearly composites. Scenes with less than 10 %
clear pixels inside the field are skipped. Images are field-clipped native-
resolution palette PNGs on a fixed red-yellow-green 0-0.9 scale so colours
stay comparable while flipping. This file is deliberately heavier than the
main map (typically 1-4 MB per grower); the main map stays lightweight and
links here.

Output: growers/<grower>/derived/reports/<grower>_ndvi_viewer.html
"""

from __future__ import annotations

import argparse
import base64
import io
import sys
import warnings
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap_runtime import ensure_runtime_environment

ensure_runtime_environment()

import geopandas as gpd
import html
import matplotlib
import numpy as np
import rasterio
import shapely
from PIL import Image
from rasterio.features import geometry_mask

from generate_grower_web_map import (
    EQUAL_AREA_CRS,
    FARM_COLORS,
    LEAFLET_VERSION,
    NDVI_COLORMAP,
    SIMPLIFY_M,
    SKIP_CROPS,
    _hex,
    _json_for_html,
    _natural_key,
    _ndvi_by_crop,
    _read_json,
)
from paths import (
    DATA_ROOT,
    GROWERS_ROOT,
    farm_boundary_path,
    farm_manifest_dir,
    field_satellite_dir,
    field_summaries_dir,
    grower_dir,
)
from reporting_bootstrap import field_slug_map_from_inventory

SCALE_MIN, SCALE_MAX = 0.0, 0.9
PALETTE_LEVELS = 64
MIN_CLEAR_PCT = 10.0
PARTLY_CLOUDY_PCT = 50.0
SIZE_WARN_BYTES = 8_000_000
SENSORS = (("sentinel", "S2", "Sentinel-2 · 10 m"), ("landsat", "L", "Landsat · 30 m"))
YEAR_COLORS = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#D55E00", "#56B4E9", "#F0E442"]


def _palette() -> list[int]:
    cmap = matplotlib.colormaps[NDVI_COLORMAP]
    flat = [0, 0, 0]  # index 0 = transparent
    for i in range(PALETTE_LEVELS):
        flat.extend(int(round(c * 255)) for c in cmap(i / (PALETTE_LEVELS - 1))[:3])
    return flat + [0] * (768 - len(flat))


_PALETTE = _palette()


def _png(values: np.ndarray) -> str:
    """Palette PNG on the fixed red-yellow-green scale (NaN = transparent) as a data URL."""
    scaled = np.clip((np.nan_to_num(values) - SCALE_MIN) / (SCALE_MAX - SCALE_MIN), 0, 1)
    index = (np.round(scaled * (PALETTE_LEVELS - 1)) + 1).astype("uint8")
    index[~np.isfinite(values)] = 0
    image = Image.fromarray(index, "P")
    image.putpalette(_PALETTE)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True, transparency=0)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _bounds(transform, shape, crs) -> list[list[float]]:
    west, north = transform * (0, 0)
    east, south = transform * (shape[1], shape[0])
    if crs and crs.to_epsg() != 4326:
        west, south, east, north = gpd.GeoSeries(
            [shapely.geometry.box(west, south, east, north)], crs=crs
        ).to_crs(4326).total_bounds
    return [[round(float(south), 7), round(float(west), 7)], [round(float(north), 7), round(float(east), 7)]]


def _clip(path: Path, field_geom) -> tuple[np.ndarray, list, float] | None:
    """Read an NDVI raster, keep pixels inside the field; return (values, bounds, clear %)."""
    try:
        with rasterio.open(path) as src:
            data = src.read(1, masked=True).astype("float64").filled(np.nan)
            transform, crs = src.transform, src.crs
    except Exception:
        return None
    geom = gpd.GeoSeries([field_geom], crs="EPSG:4326").to_crs(crs).iloc[0]
    inside = geometry_mask([geom], data.shape, transform, invert=True, all_touched=True)
    if not inside.any():
        return None
    values = np.where(inside, data, np.nan)
    clear = 100.0 * np.isfinite(values).sum() / inside.sum()
    return values, _bounds(transform, data.shape, crs), clear


def _resolve(path_text: str) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else DATA_ROOT / path


def _field_record(grower: str, farm: str, farm_name: str, field_slug: str, row, geom_simplified) -> dict:
    crops: dict[str, str] = {}
    summary = _read_json(field_summaries_dir(grower, farm, field_slug) / "ndvi_yearly_summary.json")
    for entry in summary.get("years", []):
        crop = str(entry.get("crop_name") or "").strip()
        crops[str(entry["year"])] = crop if crop.lower() not in SKIP_CROPS else "Unknown"

    scenes, bounds, skipped = [], {}, 0
    for folder, code, _ in SENSORS:
        manifest = _read_json(field_satellite_dir(grower, farm, field_slug) / folder / "manifest.json")
        for year_entry in manifest.get("years", []):
            for scene in year_entry.get("scenes", []):
                if not scene.get("ndvi_tif") or scene.get("status", "complete") != "complete":
                    continue
                clipped = _clip(_resolve(scene["ndvi_tif"]), row.geometry)
                if clipped is None:
                    continue
                values, scene_bounds, clear = clipped
                if clear < MIN_CLEAR_PCT:
                    skipped += 1
                    continue
                bounds.setdefault(code, scene_bounds)
                cloud = scene.get("cloud_cover")
                cloud = float(cloud) * (100 if cloud is not None and float(cloud) <= 1 else 1) if cloud is not None else None
                scenes.append(
                    {
                        "d": str(scene.get("scene_date"))[:10],
                        "s": code,
                        "cc": round(cloud, 1) if cloud is not None else None,
                        "clear": round(clear),
                        "mean": round(float(np.nanmean(values)), 3),
                        "png": _png(values),
                        "b": scene_bounds if scene_bounds != bounds[code] else None,
                    }
                )
    scenes.sort(key=lambda s: (s["d"], s["s"]))

    composites = []
    for entry in sorted(summary.get("years", []), key=lambda e: e["year"]):
        if not entry.get("composite_tif"):
            continue
        clipped = _clip(_resolve(entry["composite_tif"]), row.geometry)
        if clipped is None or clipped[2] == 0:
            continue
        values, comp_bounds, _ = clipped
        composites.append(
            {
                "label": f"{entry['year']} season composite",
                "year": int(entry["year"]),
                "crop": crops.get(str(entry["year"]), "Unknown"),
                "mean": round(float(np.nanmean(values)), 3),
                "png": _png(values),
                "b": comp_bounds,
            }
        )

    averages = []
    for layer in _ndvi_by_crop(grower, farm, field_slug, row.geometry):
        averages.append(
            {
                "label": f"Average {layer['crop']} NDVI",
                "crop": layer["crop"],
                "years": layer["years"],
                "mean": layer["mean"],
                "png": _png(layer.pop("_array")),
                "b": layer["bounds"],
            }
        )

    return {
        "id": f"{farm}/{field_slug}",
        "farm": farm,
        "farmName": farm_name,
        "name": str(row.get("field_name") or field_slug),
        "acres": round(float(row.get("area_acres") or 0), 1),
        "geom": shapely.geometry.mapping(shapely.set_precision(geom_simplified, 1e-6)),
        "crops": crops,
        "scenes": scenes,
        "bounds": bounds,
        "skipped": skipped,
        "composites": composites,
        "averages": averages,
    }


def _fields(grower: str) -> tuple[list[dict], list[dict], str]:
    fields, farms = [], []
    grower_name = grower
    for farm_path in sorted((grower_dir(grower) / "farms").iterdir(), key=lambda p: _natural_key(p.name)):
        farm = farm_path.name
        boundary = farm_boundary_path(grower, farm)
        if not boundary.exists():
            continue
        gdf = gpd.read_file(boundary).to_crs("EPSG:4326")
        if gdf.empty:
            continue
        farm_meta = _read_json(farm_path / "farm.json")
        slugs = field_slug_map_from_inventory(farm_manifest_dir(grower, farm) / "field-inventory.csv")
        simplified = gdf.to_crs(EQUAL_AREA_CRS).geometry.simplify(SIMPLIFY_M, preserve_topology=True).to_crs(4326)
        farm_name = str(gdf.iloc[0].get("farm_name") or farm_meta.get("display_name") or farm)
        grower_name = str(gdf.iloc[0].get("grower_name") or grower_name)
        farms.append({"slug": farm, "name": farm_name, "color": FARM_COLORS[len(farms) % len(FARM_COLORS)]})
        for idx, row in gdf.iterrows():
            field_id = str(row.get("field_id", idx))
            fields.append(_field_record(grower, farm, farm_name, slugs.get(field_id, field_id), row, simplified.loc[idx]))
    fields.sort(key=lambda f: (_natural_key(f["farm"]), _natural_key(f["name"])))
    return fields, farms, grower_name


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
  #side { width: 270px; display: flex; flex-direction: column; background: #12161c; color: #e8eaed; z-index: 1000; box-shadow: 2px 0 8px rgba(0,0,0,.4); }
  #side header { padding: 14px 16px 10px; border-bottom: 1px solid #2a313b; }
  #side h1 { font-size: 17px; margin: 0 0 2px; }
  #side .sub { color: #9aa4b2; font-size: 12.5px; }
  #side a { color: #8fb4ff; font-size: 12.5px; }
  #side input { width: 100%; box-sizing: border-box; margin-top: 10px; padding: 6px 8px; border-radius: 6px; border: 1px solid #333c48; background: #1b2129; color: #e8eaed; }
  #list { overflow-y: auto; flex: 1; padding: 4px 0 12px; }
  .farm { padding: 10px 16px 4px; font-size: 11.5px; letter-spacing: .06em; text-transform: uppercase; color: #9aa4b2; display: flex; gap: 8px; align-items: center; }
  .swatch { width: 11px; height: 11px; border-radius: 2px; border: 1px solid #fff; }
  .item { padding: 6px 16px 6px 34px; cursor: pointer; display: flex; justify-content: space-between; gap: 8px; }
  .item:hover, .item.active { background: #1f2a38; }
  .item small { color: #9aa4b2; }
  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  #bar { padding: 8px 12px; background: #f4f6f9; border-bottom: 1px solid #d9dee5; display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; }
  #bar .group { display: flex; gap: 4px; align-items: center; }
  #bar button, #bar select { padding: 4px 9px; border: 1px solid #9fb3d9; border-radius: 5px; background: #fff; cursor: pointer; font-size: 12.5px; }
  #bar button.on { background: #2f6fed; border-color: #2f6fed; color: #fff; }
  #bar button:disabled { opacity: .45; cursor: default; }
  #bar label { font-size: 12.5px; color: #333; display: flex; align-items: center; gap: 4px; }
  #slider { width: 220px; }
  #info { padding: 6px 12px; font-size: 13px; background: #fff; border-bottom: 1px solid #e3e7ec; min-height: 20px; }
  #info b { font-size: 14px; }
  #info .tag { display: inline-block; padding: 0 6px; border-radius: 9px; background: #eef1f5; margin-left: 6px; font-size: 12px; }
  #info .warn { background: #fff1d6; }
  #map { flex: 1; min-height: 200px; }
  #chartwrap { height: 190px; border-top: 1px solid #d9dee5; background: #fff; position: relative; }
  #chart { width: 100%; height: 100%; display: block; }
  #chart .pt { cursor: pointer; }
  #chart .pt:hover { stroke: #000; stroke-width: 2; }
  #avgpanel { padding: 8px 12px; display: none; flex-wrap: wrap; gap: 6px; background: #fff; border-bottom: 1px solid #e3e7ec; }
  #avgpanel button { padding: 5px 9px; border: 1px solid #2f6fed; border-radius: 5px; background: #eef3fe; color: #1a3f8f; cursor: pointer; font-size: 12.5px; }
  #avgpanel button.on { background: #2f6fed; color: #fff; }
  .legend { background: rgba(255,255,255,.95); padding: 7px 10px; border-radius: 6px; box-shadow: 0 1px 6px rgba(0,0,0,.35); font-size: 12px; width: 210px; }
  .legend .grad { height: 12px; border: 1px solid #333; border-radius: 2px; margin: 4px 0 2px; }
  .legend .ticks { display: flex; justify-content: space-between; color: #444; font-size: 11px; }
  .ndvi-img { image-rendering: pixelated; }
  .empty { padding: 30px; color: #666; }
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <header>
      <h1>__HEADING__</h1>
      <div class="sub">NDVI viewer · __STATS__</div>
      <a href="__MAP_FILE__">← Back to the field map</a>
      <input id="filter" placeholder="Filter fields…" aria-label="Filter fields">
    </header>
    <div id="list"></div>
  </aside>
  <div id="main">
    <div id="bar">
      <div class="group"><button id="tab-scenes" class="on">Scenes</button><button id="tab-avg">Averages</button></div>
      <div class="group" id="years"></div>
      <div class="group"><select id="sensor" aria-label="Sensor"><option value="both">Both sensors</option><option value="S2">Sentinel-2 (10 m)</option><option value="L">Landsat (30 m)</option></select></div>
      <label><input type="checkbox" id="hidecloudy"> Hide partly cloudy</label>
      <div class="group"><button id="prev" title="Previous scene (←)">◀</button><button id="play" title="Play / pause (space)">▶ Play</button><button id="next" title="Next scene (→)">▶</button></div>
      <input type="range" id="slider" min="0" max="0" value="0" aria-label="Scene">
      <label>Opacity <input type="range" id="opacity" min="20" max="100" value="100" style="width:80px"></label>
    </div>
    <div id="avgpanel"></div>
    <div id="info"></div>
    <div id="map"></div>
    <div id="chartwrap"><svg id="chart" role="img" aria-label="Field mean NDVI over time"></svg></div>
  </div>
</div>
<script>
const FIELDS = __FIELDS__;
const FARMS = __FARMS__;
const META = __META__;
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const SENSOR_NAME = { S2: "Sentinel-2 · 10 m", L: "Landsat · 30 m" };
const map = L.map("map", { zoomControl: true });
L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 20, maxNativeZoom: 19, attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics" }).addTo(map);
L.control.scale({ imperial: true, metric: true }).addTo(map);
map.createPane("ndvi").style.zIndex = 380;
const overlay = L.imageOverlay("data:image/gif;base64,R0lGODlhAQABAAAAACw=", [[0, 0], [0, 0]], { pane: "ndvi", className: "ndvi-img" }).addTo(map);
let outline = null;
const legend = L.control({ position: "bottomright" });
legend.onAdd = () => {
  const d = L.DomUtil.create("div", "legend");
  d.innerHTML = `<b>NDVI</b> (fixed scale for flipping)<div class="grad" style="background:linear-gradient(to right, ${META.colors.join(",")})"></div>
    <div class="ticks"><span>${META.min.toFixed(1)} low</span><span>${((META.min + META.max) / 2).toFixed(2)}</span><span>${META.max.toFixed(1)} high</span></div>`;
  return d;
};
legend.addTo(map);

const state = { field: null, tab: "scenes", year: "all", sensor: "both", hideCloudy: false, idx: 0, timer: null, avg: 0 };
const $ = id => document.getElementById(id);

function filtered() {
  const f = state.field;
  if (!f) return [];
  return f.scenes.filter(s => (state.year === "all" || s.d.startsWith(state.year))
    && (state.sensor === "both" || s.s === state.sensor) && (!state.hideCloudy || s.clear >= META.partly));
}
const boundsOf = (f, s) => s.b || f.bounds[s.s];
const doy = d => { const t = new Date(d + "T00:00:00Z"); return Math.round((t - Date.UTC(t.getUTCFullYear(), 0, 1)) / 864e5) + 1; };

function showImage(png, bounds) {
  overlay.setUrl(png);
  overlay.setBounds(bounds);
  overlay.setOpacity($("opacity").value / 100);
}

function render() {
  const f = state.field;
  if (!f) return;
  $("tab-scenes").classList.toggle("on", state.tab === "scenes");
  $("tab-avg").classList.toggle("on", state.tab === "avg");
  $("avgpanel").style.display = state.tab === "avg" ? "flex" : "none";
  ["years", "sensor", "hidecloudy", "prev", "play", "next", "slider"].forEach(id => {
    const el = $(id); el.closest("label") ? (el.closest("label").style.display = state.tab === "scenes" ? "" : "none")
      : (el.style.display = state.tab === "scenes" ? "" : "none");
  });
  if (state.tab === "avg") return renderAverages();
  const list = filtered();
  state.idx = Math.max(0, Math.min(state.idx, list.length - 1));
  $("slider").max = Math.max(0, list.length - 1);
  $("slider").value = state.idx;
  $("prev").disabled = $("next").disabled = $("play").disabled = list.length < 2;
  const s = list[state.idx];
  if (!s) {
    overlay.setUrl("data:image/gif;base64,R0lGODlhAQABAAAAACw=");
    $("info").innerHTML = `<b>${esc(f.name)}</b> — no scenes match these filters.`;
  } else {
    showImage(s.png, boundsOf(f, s));
    const crop = f.crops[s.d.slice(0, 4)] || "Unknown";
    $("info").innerHTML = `<b>${esc(f.name)}</b> · ${esc(s.d)} <span class="tag">${SENSOR_NAME[s.s]}</span>
      <span class="tag">field mean NDVI ${s.mean.toFixed(2)}</span><span class="tag">crop ${esc(crop)}</span>
      <span class="tag ${s.clear < META.partly ? "warn" : ""}">${s.clear}% of field clear${s.clear < META.partly ? " · partly cloudy" : ""}</span>
      ${s.cc != null ? `<span class="tag">scene cloud ${s.cc}%</span>` : ""}<span class="tag">${state.idx + 1} / ${list.length}</span>`;
  }
  drawChart(s);
}

function renderAverages() {
  const f = state.field;
  const items = [...f.averages, ...f.composites];
  $("avgpanel").innerHTML = items.length ? items.map((a, i) =>
    `<button data-i="${i}" class="${i === state.avg ? "on" : ""}">${esc(a.label)}${a.crop && !a.years ? ` · ${esc(a.crop)}` : ""} · ${a.mean.toFixed(2)}</button>`).join("")
    : `<span class="empty">No averages or composites for this field.</span>`;
  const a = items[state.avg];
  if (a) {
    showImage(a.png, a.b);
    $("info").innerHTML = `<b>${esc(f.name)}</b> · ${esc(a.label)} <span class="tag">field mean NDVI ${a.mean.toFixed(2)}</span>${
      a.years ? `<span class="tag">years ${a.years.join(", ")}</span>` : `<span class="tag">crop ${esc(a.crop)}</span>`}`;
  }
  drawChart(null);
}

function drawChart(current) {
  const svg = $("chart");
  const W = svg.clientWidth || 800, H = svg.clientHeight || 190, m = { l: 44, r: 160, t: 12, b: 28 };
  const x = d => m.l + (d - 60) / (335 - 60) * (W - m.l - m.r);
  const y = v => H - m.b - (v - META.min) / (META.max - META.min) * (H - m.t - m.b);
  const f = state.field;
  const pts = f.scenes.filter(s => (state.sensor === "both" || s.s === state.sensor) && (!state.hideCloudy || s.clear >= META.partly));
  const years = [...new Set(pts.map(s => s.d.slice(0, 4)))].sort();
  const months = [["Mar", 60], ["Apr", 91], ["May", 121], ["Jun", 152], ["Jul", 182], ["Aug", 213], ["Sep", 244], ["Oct", 274], ["Nov", 305]];
  let out = "";
  [0, 0.3, 0.6, 0.9].forEach(v => { out += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="#e6e9ee"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="#555">${v.toFixed(1)}</text>`; });
  months.forEach(([n, d]) => { out += `<text x="${x(d + 15)}" y="${H - 9}" text-anchor="middle" font-size="11" fill="#555">${n}</text>`; });
  out += `<text x="12" y="${(H - m.b + m.t) / 2}" font-size="11" fill="#555" transform="rotate(-90 12 ${(H - m.b + m.t) / 2})" text-anchor="middle">Field mean NDVI</text>`;
  years.forEach((yr, k) => {
    const c = META.yearColors[k % META.yearColors.length];
    const ys = pts.filter(s => s.d.startsWith(yr)).sort((a, b) => doy(a.d) - doy(b.d));
    const faded = state.year !== "all" && state.year !== yr;
    out += `<polyline fill="none" stroke="${c}" stroke-width="2" opacity="${faded ? 0.2 : 0.9}" points="${ys.map(s => `${x(doy(s.d))},${y(s.mean)}`).join(" ")}"/>`;
    ys.forEach(s => {
      const on = current && s === current;
      out += `<circle class="pt" data-d="${s.d}" data-s="${s.s}" cx="${x(doy(s.d))}" cy="${y(s.mean)}" r="${on ? 7 : 4}" fill="${s.s === "L" ? "#fff" : c}" stroke="${c}" stroke-width="${on ? 3 : 1.5}" opacity="${faded ? 0.25 : 1}"><title>${s.d} · ${SENSOR_NAME[s.s]} · NDVI ${s.mean.toFixed(2)} · ${s.clear}% clear</title></circle>`;
    });
    out += `<text x="${W - m.r + 12}" y="${m.t + 14 + k * 16}" font-size="12" fill="${c}" font-weight="600">● ${yr} ${esc(f.crops[yr] || "")}</text>`;
  });
  out += `<text x="${W - m.r + 12}" y="${H - m.b}" font-size="11" fill="#555">● S2  ○ Landsat</text>`;
  svg.innerHTML = out;
}

$("chart").addEventListener("click", e => {
  const pt = e.target.closest(".pt");
  if (!pt) return;
  state.tab = "scenes";
  if (state.year !== "all" && !pt.dataset.d.startsWith(state.year)) state.year = "all";
  renderYears();
  const i = filtered().findIndex(s => s.d === pt.dataset.d && s.s === pt.dataset.s);
  if (i >= 0) state.idx = i;
  render();
});

function renderYears() {
  const f = state.field;
  const years = f ? [...new Set(f.scenes.map(s => s.d.slice(0, 4)))].sort() : [];
  $("years").innerHTML = ["all", ...years].map(yr =>
    `<button data-year="${yr}" class="${state.year === yr ? "on" : ""}">${yr === "all" ? "All years" : yr}</button>`).join("");
}
$("years").addEventListener("click", e => {
  const b = e.target.closest("[data-year]");
  if (!b) return;
  state.year = b.dataset.year; state.idx = 0; renderYears(); render();
});
$("avgpanel").addEventListener("click", e => {
  const b = e.target.closest("[data-i]");
  if (b) { state.avg = +b.dataset.i; render(); }
});
$("tab-scenes").onclick = () => { state.tab = "scenes"; render(); };
$("tab-avg").onclick = () => { stop(); state.tab = "avg"; render(); };
$("sensor").onchange = e => { state.sensor = e.target.value; state.idx = 0; render(); };
$("hidecloudy").onchange = e => { state.hideCloudy = e.target.checked; state.idx = 0; render(); };
$("slider").oninput = e => { state.idx = +e.target.value; render(); };
$("opacity").oninput = e => overlay.setOpacity(e.target.value / 100);
const step = d => { const n = filtered().length; if (n) { state.idx = (state.idx + d + n) % n; render(); } };
$("prev").onclick = () => step(-1);
$("next").onclick = () => step(1);
function stop() { clearInterval(state.timer); state.timer = null; $("play").textContent = "▶ Play"; }
$("play").onclick = () => {
  if (state.timer) return stop();
  $("play").textContent = "⏸ Pause";
  state.timer = setInterval(() => step(1), 900);
};
document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT" && e.target.type === "text") return;
  if (e.key === "ArrowRight") { step(1); e.preventDefault(); }
  if (e.key === "ArrowLeft") { step(-1); e.preventDefault(); }
  if (e.key === " ") { $("play").click(); e.preventDefault(); }
});

function selectField(id) {
  const f = FIELDS.find(x => x.id === id) || FIELDS[0];
  if (!f) return;
  stop();
  state.field = f; state.idx = 0; state.avg = 0;
  if (state.year !== "all" && !f.scenes.some(s => s.d.startsWith(state.year))) state.year = "all";
  document.querySelectorAll(".item").forEach(el => el.classList.toggle("active", el.dataset.id === f.id));
  if (outline) outline.remove();
  outline = L.geoJSON(f.geom, { style: { color: "#111", weight: 5, fill: false, opacity: 0.9 } }).addTo(map);
  L.geoJSON(f.geom, { style: { color: (FARMS.find(x => x.slug === f.farm) || {}).color || "#E69F00", weight: 2.5, fill: false } }).addTo(outline);
  map.fitBounds(outline.getBounds(), { padding: [30, 30], maxZoom: 18 });
  history.replaceState(null, "", "#field=" + encodeURIComponent(f.id));
  renderYears();
  render();
}

const list = $("list");
FARMS.forEach(farm => {
  const head = document.createElement("div");
  head.className = "farm";
  head.innerHTML = `<span class="swatch" style="background:${farm.color}"></span>${esc(farm.name)}`;
  list.appendChild(head);
  FIELDS.filter(f => f.farm === farm.slug).forEach(f => {
    const el = document.createElement("div");
    el.className = "item";
    el.dataset.id = f.id;
    el.dataset.search = f.name.toLowerCase();
    el.innerHTML = `<span>${esc(f.name)}</span><small>${f.scenes.length} scenes</small>`;
    el.onclick = () => selectField(f.id);
    list.appendChild(el);
  });
});
$("filter").oninput = e => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll(".item").forEach(el => { el.style.display = el.dataset.search.includes(q) ? "" : "none"; });
};
window.addEventListener("resize", () => drawChart(state.tab === "scenes" ? filtered()[state.idx] : null));
const fromHash = decodeURIComponent((location.hash.match(/field=([^&]+)/) || [])[1] || "");
selectField(fromHash);
</script>
</body>
</html>
"""


def viewer_filename(grower: str) -> str:
    return f"{grower}_ndvi_viewer.html"


def build_ndvi_viewer(grower: str, map_filename: str | None = None) -> tuple[Path, dict]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        fields, farms, grower_name = _fields(grower)
    scenes = sum(len(f["scenes"]) for f in fields)
    stats = f"{len(fields)} fields · {scenes} scenes"
    meta = {
        "min": SCALE_MIN,
        "max": SCALE_MAX,
        "colors": [_hex(matplotlib.colormaps[NDVI_COLORMAP](i / 8)) for i in range(9)],
        "partly": PARTLY_CLOUDY_PCT,
        "yearColors": YEAR_COLORS,
    }
    page = (
        _TEMPLATE.replace("__LEAFLET__", LEAFLET_VERSION)
        .replace("__TITLE__", html.escape(f"{grower_name} — NDVI viewer"))
        .replace("__HEADING__", html.escape(grower_name))
        .replace("__STATS__", html.escape(stats))
        .replace("__MAP_FILE__", html.escape(map_filename or f"{grower}_web_map.html"))
        .replace("__FARMS__", _json_for_html(farms))
        .replace("__META__", _json_for_html(meta))
        .replace("__FIELDS__", _json_for_html(fields))
    )
    target = grower_dir(grower) / "derived" / "reports" / viewer_filename(grower)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return target, {
        "grower": grower,
        "fields": len(fields),
        "scenes": scenes,
        "skipped": sum(f["skipped"] for f in fields),
        "bytes": target.stat().st_size,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--grower", action="append", help="Grower slug (repeatable)")
    group.add_argument("--all-growers", action="store_true", help="Every grower under growers/")
    args = parser.parse_args(argv)
    growers = (
        sorted(p.name for p in GROWERS_ROOT.iterdir() if (p / "farms").is_dir())
        if args.all_growers
        else args.grower
    )
    for grower in growers:
        target, entry = build_ndvi_viewer(grower)
        warn = "  WARNING: over 8 MB" if entry["bytes"] > SIZE_WARN_BYTES else ""
        print(
            f"ok    {grower:<22} {entry['fields']:>3} fields, {entry['scenes']:>4} scenes "
            f"({entry['skipped']} cloudy skipped), {entry['bytes'] / 1e6:.1f} MB  {target}{warn}"
        )


if __name__ == "__main__":
    main()
